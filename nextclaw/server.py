"""NextClaw web dashboard + JSON API server (Python standard library only).

``python3 -m nextclaw.cli serve`` starts:
  - the dashboard UI at http://127.0.0.1:8765/
  - a JSON API under /api/ (Bearer token auth, see module docstring)

Security model: the server binds loopback by default. The API token is read
from NEXTCLAW_API_TOKEN or auto-generated into <data_dir>/api_token (0600)
and printed once at startup. The dashboard keeps the token in the browser's
localStorage after the user pastes it on the unlock screen; it is never
logged, never embedded in served pages, and never sent to analytics.
Secrets (API keys) stay server-side and are never returned by any endpoint.
"""
from __future__ import annotations

import dataclasses
import hmac
import json
import mimetypes
import os
import secrets
import uuid
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple

VERSION = "0.2.0-dev"
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
OVERRIDES_FILE = "server_overrides.json"

# Config keys the API is allowed to change at runtime. API keys are
# deliberately excluded: they come from the environment only.
MUTABLE_CONFIG = {
    "llm_provider", "llm_model", "ollama_url", "ollama_model",
    "llm_timeout_s", "max_turns", "max_step_attempts", "task_timeout_s",
    "workspace_root", "llm_max_requests_per_task",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class RateLimiter:
    """Tiny in-memory per-IP rate limiter for sensitive endpoints."""

    def __init__(self, max_hits: int = 30, window_s: float = 60.0):
        self.max_hits = max_hits
        self.window_s = window_s
        self._hits: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def check(self, ip: str, key: str) -> None:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key + ip, []) if now - t < self.window_s]
            if len(hits) >= self.max_hits:
                raise ApiError(429, "rate limit exceeded, try again shortly")
            hits.append(now)
            self._hits[key + ip] = hits


class ServerContext:
    def __init__(self, cfg):
        from .approvals import ApprovalGate, ApprovalStore
        from .config import ensure_data_dir
        from .providers import ProviderStore, seed_from_legacy_config
        from .tasks import TaskStore

        ensure_data_dir(cfg)
        # Runtime config overrides persisted by POST /api/providers/config.
        overrides_path = os.path.join(cfg.data_dir, OVERRIDES_FILE)
        if os.path.exists(overrides_path):
            try:
                saved = json.load(open(overrides_path))
                for k, v in saved.items():
                    if k in MUTABLE_CONFIG and hasattr(cfg, k):
                        setattr(cfg, k, v)
            except Exception:
                pass
        self.cfg = cfg
        self.tasks = TaskStore(cfg.data_dir)
        self.approvals = ApprovalStore(cfg.data_dir)
        self.gate = ApprovalGate(self.approvals, self.tasks)
        self.provider_store = ProviderStore(cfg.data_dir)
        from .delegation import DelegationStore
        self.delegations = DelegationStore(cfg.data_dir)
        # Migrate old llm_provider/llm_model/env settings once, idempotently.
        try:
            seed_from_legacy_config(self.provider_store, cfg)
        except Exception:
            pass
        self.runner = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nc-task")
        self.running: Dict[str, Any] = {}  # task_id -> Future
        self.rate = RateLimiter()
        self.token, self.token_source = self._load_token(cfg.data_dir)
        # Short-lived single-use SSE tickets: ticket -> expiry timestamp.
        # Issued only to Bearer-authenticated callers; redeemed (consumed) on
        # first SSE connect. Never put the long-lived dashboard token in a URL.
        self._sse_tickets: Dict[str, float] = {}
        self._sse_lock = threading.Lock()
        self._token_lock = threading.Lock()

    SSE_TICKET_TTL_S = 60.0

    def issue_sse_ticket(self) -> Dict[str, Any]:
        """Mint a single-use SSE ticket (callers must already be authed)."""
        ticket = secrets.token_urlsafe(24)
        expires_at = time.time() + self.SSE_TICKET_TTL_S
        with self._sse_lock:
            # Prune expired tickets so the dict cannot grow unboundedly.
            now = time.time()
            for t in [t for t, exp in self._sse_tickets.items() if exp <= now]:
                del self._sse_tickets[t]
            self._sse_tickets[ticket] = expires_at
        return {"ticket": ticket, "expires_in": int(self.SSE_TICKET_TTL_S)}

    def redeem_sse_ticket(self, ticket: str) -> bool:
        """Consume a ticket. Single-use: replay after first use fails."""
        if not ticket:
            return False
        with self._sse_lock:
            exp = self._sse_tickets.pop(ticket, None)
        return exp is not None and exp > time.time()

    # -- auth -----------------------------------------------------------
    @staticmethod
    def _load_token(data_dir: str) -> tuple:
        """Returns (token, source) where source is 'env' or 'file'."""
        env_token = os.environ.get("NEXTCLAW_API_TOKEN", "").strip()
        if env_token:
            return env_token, "env"
        path = os.path.join(data_dir, "api_token")
        if os.path.exists(path):
            return open(path).read().strip(), "file"
        token = secrets.token_urlsafe(32)
        with open(path, "w") as f:
            f.write(token)
        os.chmod(path, 0o600)
        return token, "file"

    def rotate_token(self, presented: str) -> str:
        """Atomically swap the dashboard token. Returns the new token.

        Only valid when the token is file-managed; env-managed tokens are
        refused (the operator must change the env var). Re-validates the
        presented credential under the lock so concurrent rotations cannot
        silently double-rotate: the loser gets an error, not a dead token.
        """
        if self.token_source != "file":
            raise ApiError(409, "token is managed via NEXTCLAW_API_TOKEN; "
                                "rotation is not applicable")
        with self._token_lock:
            if not hmac.compare_digest(presented.strip(), self.token):
                raise ApiError(409, "token was already rotated; "
                                    "re-authenticate with the current token")
            new_token = secrets.token_urlsafe(32)
            path = os.path.join(self.cfg.data_dir, "api_token")
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(new_token)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)  # atomic swap on POSIX
            self.token = new_token
            return new_token

    def check_auth(self, headers) -> None:
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise ApiError(401, "missing bearer token")
        if not hmac.compare_digest(auth[7:].strip(), self.token):
            raise ApiError(401, "invalid token")

    # -- task execution ---------------------------------------------------
    def submit_task(self, task_id: str, mode: str) -> None:
        from . import agent, agent_llm

        if task_id in self.running and not self.running[task_id].done():
            return  # already running; never double-execute
        self.tasks.add_event(task_id, "status", {"status": "queued"})

        def _run():
            try:
                if mode == "run-agent":
                    agent_llm.run_llm_task(task_id, self.cfg, approver=self.gate)
                else:
                    agent.run_task(task_id, self.cfg, approver=self.gate)
            except Exception as exc:  # never leave a task stuck
                try:
                    t = self.tasks.get_task(task_id)
                    if t and t["status"] not in ("completed", "failed", "cancelled"):
                        self.tasks.set_task_status(task_id, "failed", error=f"runner error: {exc}")
                        self.tasks.add_event(task_id, "status", {"status": "failed"})
                except Exception:
                    pass
            finally:
                self.running.pop(task_id, None)
                try:
                    self.approvals.audit("system", "task.finished", task_id, {})
                except Exception:
                    pass

        self.running[task_id] = self.runner.submit(_run)

    def submit_delegation(self, mission_id: str) -> None:
        """Run a delegation mission in the background; the delegation engine
        persists status/results to the DelegationStore itself."""
        from . import delegation as deleg_mod

        if mission_id in self.running and not self.running[mission_id].done():
            return  # already running; never double-execute

        def _run():
            try:
                rec = self.delegations.get(mission_id)
                if rec is None:
                    return
                plan_raw = json.loads(rec["plan"] or "[]")
                plan = [deleg_mod.Subtask(**{
                    k: v for k, v in s.items()
                    if k in deleg_mod.Subtask.__dataclass_fields__})
                    for s in plan_raw]
                budgets = deleg_mod.DelegationBudgets(**{
                    k: v for k, v in json.loads(rec["budgets"] or "{}").items()
                    if k in deleg_mod.DelegationBudgets.__dataclass_fields__})
                deleg_mod.run_delegation(
                    self.cfg, rec["goal"], plan, mission_id=mission_id,
                    budgets=budgets, store=self.delegations)
            except Exception as exc:  # never leave a mission stuck
                try:
                    rec = self.delegations.get(mission_id)
                    if rec and rec["status"] not in (
                            "completed", "failed", "cancelled"):
                        self.delegations.update(
                            mission_id, status="failed",
                            error=f"runner error: {exc}")
                except Exception:
                    pass
            finally:
                self.running.pop(mission_id, None)

        self.running[mission_id] = self.runner.submit(_run)


def _json_body(handler: BaseHTTPRequestHandler, max_bytes: int = 1_000_000) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length > max_bytes:
        raise ApiError(413, "request body too large")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ApiError(400, "invalid JSON body")
    if not isinstance(data, dict):
        raise ApiError(400, "JSON body must be an object")
    return data


class Handler(BaseHTTPRequestHandler):
    ctx: ServerContext  # set by serve()
    server_version = "NextClaw/0.2"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        # Never log Authorization headers or bodies that may hold secrets.
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.command} {self.path.split('?')[0]} -> {fmt % args}\n")

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _route(self) -> Tuple[str, str, Dict[str, List[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        return self.command, parsed.path, urllib.parse.parse_qs(parsed.query)

    # -- HTTP verbs ---------------------------------------------------------
    def do_GET(self):
        try:
            self._handle()
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except BrokenPipeError:
            pass
        except Exception:  # never leak internals
            self._error(500, "internal error")

    def do_POST(self):
        self.do_GET()  # _handle dispatches on method

    def do_PUT(self):
        self.do_GET()  # _handle dispatches on method

    def do_DELETE(self):
        self.do_GET()  # _handle dispatches on method

    def _handle(self) -> None:
        method, path, qs = self._route()
        if path == "/api/health":
            return self._send_json({"ok": True, "version": VERSION, "time": time.time()})
        if path == "/" or not path.startswith("/api/"):
            return self._serve_static(path)
        # Auth: Bearer header normally. The SSE endpoint cannot take headers
        # (EventSource), so it accepts a short-lived single-use ?ticket=
        # minted via POST /api/sse/ticket by an authenticated caller.
        # The long-lived dashboard token is never placed in a URL.
        # Note: log_message() strips query strings, so tickets never hit logs;
        # a ticket is single-use with a 60s TTL, bounding replay/abuse if a
        # proxy or infrastructure log does capture the URL.
        if "/events" in path and (qs.get("ticket") or [""])[0]:
            if not self.ctx.redeem_sse_ticket((qs.get("ticket") or [""])[0]):
                raise ApiError(401, "invalid or expired ticket")
        else:
            self.ctx.check_auth(self.headers)

        routes = {
            ("GET", "/api/status"): self._api_status,
            ("GET", "/api/tasks"): self._api_list_tasks,
            ("POST", "/api/tasks"): self._api_create_task,
            ("GET", "/api/approvals"): self._api_list_approvals,
            ("GET", "/api/policy"): self._api_policy,
            ("GET", "/api/providers"): self._api_providers,
            ("POST", "/api/providers/test"): self._api_test_provider,
            ("POST", "/api/providers/config"): self._api_config_provider,
            ("GET", "/api/scheduler/jobs"): self._api_sched_jobs,
            ("GET", "/api/scheduler/runs"): self._api_sched_runs,
            ("GET", "/api/quota"): self._api_quota,
            ("GET", "/api/audit"): self._api_audit,
            ("GET", "/api/memory"): self._api_memory,
            ("GET", "/api/delegations"): self._api_list_delegations,
            ("POST", "/api/delegations"): self._api_create_delegation,
            ("GET", "/api/delegations/profiles"): self._api_delegation_profiles,
            ("GET", "/api/delegations/settings"): self._api_delegation_settings_get,
            ("PUT", "/api/delegations/settings"): self._api_delegation_settings_put,
            ("POST", "/api/sse/ticket"): self._api_sse_ticket,
            ("POST", "/api/auth/rotate"): self._api_rotate_token,
        }
        handler = routes.get((method, path))
        if handler:
            return handler(qs)

        # Parameterized routes.
        if path.startswith("/api/tasks/"):
            return self._route_task(method, path, qs)
        if path.startswith("/api/delegations/"):
            return self._route_delegation(method, path, qs)
        if path.startswith("/api/approvals/"):
            return self._route_approval(method, path)
        if path.startswith("/api/scheduler/jobs/"):
            return self._route_sched_job(method, path)
        if path.startswith("/api/providers/"):
            return self._route_providers(method, path)
        self._error(404, "not found")

    # -- static dashboard -----------------------------------------------------
    # Extensions treated as static assets: a missing asset is a 404, never
    # the SPA fallback (which would serve index.html with 200 and confuse
    # caches, service workers, and error detection).
    _STATIC_EXTS = frozenset({
        ".js", ".mjs", ".css", ".map", ".json", ".webmanifest",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".avif",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".mp4", ".webm", ".mp3", ".wav", ".txt", ".xml",
    })

    def _serve_static(self, path: str) -> None:
        rel = urllib.parse.unquote(path.lstrip("/")) or "index.html"
        if "\\" in rel:
            return self._error(400, "bad path")
        # Contain every request inside WEB_DIR: normalize, resolve symlinks,
        # and reject anything that escapes (blocks ".." and encoded variants).
        web_root = os.path.realpath(WEB_DIR)
        full = os.path.realpath(os.path.join(web_root, os.path.normpath(rel)))
        if os.path.commonpath([full, web_root]) != web_root:
            return self._error(400, "bad path")
        if not os.path.isfile(full):
            _, ext = os.path.splitext(full)
            if ext.lower() in self._STATIC_EXTS:
                return self._error(404, "not found")
            full = os.path.join(web_root, "index.html")  # SPA fallback
        ctype, _ = mimetypes.guess_type(full)
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- API: status ------------------------------------------------------------
    def _api_status(self, qs) -> None:
        from . import providers as providers_mod
        from .browsertools import check_chromium
        cfg = self.ctx.cfg
        store = self.ctx.provider_store
        providers_mod.seed_from_legacy_config(store, cfg)
        default_id = store.get_setting("default_id")
        default = store.get(default_id) if default_id else None
        self._send_json({
            "version": VERSION,
            "workspace_root": cfg.workspace_root,
            "data_dir": cfg.data_dir,
            "providers": {
                "default_id": default_id,
                "default_name": (default or {}).get("name", ""),
                "default_model": (default or {}).get("model", ""),
                "count": len(store.list()),
                "fallback_ids": store.get_fallbacks(),
            },
            "browser": check_chromium(),
            "offline": os.environ.get("NEXTCLAW_OFFLINE") == "1",
            "token_source": self.ctx.token_source,  # 'env' or 'file'; rotation needs 'file'
            "health": self._health_snapshot(),
        })

    def _health_snapshot(self) -> Dict[str, Any]:
        """Real component health probes for the dashboard System Health panel."""
        snap: Dict[str, Any] = {}
        # Database: can we actually read tasks.db?
        try:
            import sqlite3
            conn = sqlite3.connect(os.path.join(self.ctx.cfg.data_dir, "tasks.db"), timeout=5.0)
            try:
                conn.execute("SELECT 1").fetchone()
                snap["database"] = {"ok": True, "label": "Online"}
            finally:
                conn.close()
        except Exception as exc:
            snap["database"] = {"ok": False, "label": "Unreachable", "error": str(exc)[:120]}
        # Task runner: in-process executor; reachable iff the API answered.
        snap["task_runner"] = {"ok": True, "label": "Online"}
        # Scheduler: report stored job counts (scheduler runs via CLI, not in-process).
        try:
            from .scheduler import SchedulerStore
            jobs = SchedulerStore(self.ctx.cfg.data_dir).list_jobs()
            enabled = sum(1 for j in jobs if j.get("enabled"))
            snap["scheduler"] = {"ok": True, "label": f"{len(jobs)} jobs ({enabled} enabled)"}
        except Exception as exc:
            snap["scheduler"] = {"ok": False, "label": "Error", "error": str(exc)[:120]}
        # Telegram: opt-in channel; report configured/not configured (never probe the network here).
        snap["telegram"] = (
            {"ok": True, "label": "Configured"}
            if os.environ.get("NEXTCLAW_TELEGRAM_BOT_TOKEN") else
            {"ok": None, "label": "Not configured"}
        )
        return snap

    def _api_quota(self, qs) -> None:
        from . import llm as llm_mod
        b = llm_mod.RequestBudget(self.ctx.cfg.data_dir)
        self._send_json(b.status(self.ctx.cfg))

    # -- API: tasks ---------------------------------------------------------------
    def _api_list_tasks(self, qs) -> None:
        status = (qs.get("status") or [None])[0]
        q = (qs.get("q") or [None])[0]
        limit = min(int((qs.get("limit") or ["50"])[0]), 200)
        tasks = self.ctx.tasks.list_tasks(limit=limit * 2)
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        if q:
            ql = q.lower()
            tasks = [t for t in tasks if ql in t["goal"].lower() or ql in t["id"]]
        out = []
        for t in tasks[:limit]:
            out.append({
                "id": t["id"], "goal": t["goal"], "task_type": t["task_type"],
                "status": t["status"], "error": t.get("error") or "",
                "created_at": t["created_at"], "updated_at": t["updated_at"],
            })
        self._send_json({"tasks": out})

    def _api_create_task(self, qs) -> None:
        from . import agent, agent_llm
        self.ctx.rate.check(self.client_address[0], "create_task")
        body = _json_body(self)
        goal = (body.get("goal") or "").strip()
        if not goal:
            raise ApiError(400, "goal is required")
        mode = body.get("mode") or "run"
        if mode not in ("run", "run-agent"):
            raise ApiError(400, "mode must be 'run' or 'run-agent'")
        cfg = self.ctx.cfg
        if mode == "run-agent":
            verify = body.get("verify")
            task_id = agent_llm.create_agent_task(goal, cfg, verify=verify)
        else:
            task_type = body.get("task_type") or "file_report"
            params = body.get("params") or {}
            if not isinstance(params, dict):
                raise ApiError(400, "params must be an object")
            try:
                task_id = agent.create_task(goal, task_type, params, cfg)
            except ValueError as exc:
                raise ApiError(400, str(exc))
            except Exception as exc:
                # generate_plan raises UnsupportedTask (a ProviderError, not a
                # ValueError) for bad params — that's a client error, not a 500.
                from .models import ProviderError
                if isinstance(exc, ProviderError):
                    raise ApiError(400, str(exc))
                raise
        self.ctx.approvals.audit("user", "task.created", task_id, {"mode": mode, "goal": goal[:200]})
        self.ctx.submit_task(task_id, mode)
        self._send_json({"task_id": task_id, "status": "queued"}, status=201)

    def _route_task(self, method: str, path: str, qs) -> None:
        parts = path[len("/api/tasks/"):].split("/")
        task_id, action = parts[0], (parts[1] if len(parts) > 1 else "")
        task = self.ctx.tasks.get_task(task_id)
        if not task:
            return self._error(404, "unknown task")
        if method == "GET" and action == "":
            detail = {
                "id": task["id"], "goal": task["goal"], "task_type": task["task_type"],
                "status": task["status"], "error": task.get("error") or "",
                "result": task.get("result"),
                "created_at": task["created_at"], "updated_at": task["updated_at"],
                "steps": self.ctx.tasks.get_steps(task_id),
                "events": self.ctx.tasks.list_events(task_id),
            }
            if task["task_type"] == "agent":
                turns = self.ctx.tasks.get_turns(task_id)
                detail["turns"] = turns[-40:]  # bounded; full history stays in DB
            return self._send_json(detail)
        if method == "GET" and action == "events":
            return self._sse_events(task_id, qs)
        if method == "POST" and action == "resume":
            if task["status"] in ("completed", "failed", "cancelled"):
                # Resume re-runs from checkpoints; finished tasks are left alone.
                return self._error(409, f"task already {task['status']}")
            mode = "agent" if task["task_type"] == "agent" else "run"
            self.ctx.approvals.audit("user", "task.resumed", task_id, {})
            self.ctx.submit_task(task_id, mode)
            return self._send_json({"task_id": task_id, "status": "queued"})
        if method == "POST" and action == "cancel":
            self.ctx.rate.check(self.client_address[0], "cancel")
            if task["status"] in ("completed", "failed", "cancelled"):
                return self._send_json({"task_id": task_id, "status": task["status"]})
            self.ctx.tasks.request_cancel(task_id)
            for rec in self.ctx.approvals.pending_for_task(task_id):
                self.ctx.approvals.decide(rec["id"], "rejected", decided_by="system-cancel")
                self.ctx.gate.wake(rec["id"])
            self.ctx.approvals.audit("user", "task.cancel", task_id, {})
            return self._send_json({"task_id": task_id, "status": "cancelling"})
        self._error(404, "not found")

    # -- multi-agent delegations ---------------------------------------------
    # Bounds for dashboard-configurable budgets. Only settings the delegation
    # engine actually implements are exposed.
    _BUDGET_BOUNDS = {
        "max_workers": (1, 32, int),
        "max_concurrent": (1, 8, int),
        "max_retries_per_worker": (0, 5, int),
        "max_model_calls": (1, 500, int),
        "max_runtime_s": (60, 7200, (int, float)),
        "max_depth": (1, 3, int),
    }
    _DELEGATION_TERMINAL = ("completed", "failed", "cancelled")

    def _delegation_enabled(self) -> bool:
        return self.ctx.delegations.kv_get(
            "delegation.enabled", "true") != "false"

    def _api_delegation_profiles(self, qs) -> None:
        from .delegation import list_profiles
        self._send_json({"profiles": list_profiles()})

    def _api_delegation_settings_get(self, qs) -> None:
        self._send_json({"enabled": self._delegation_enabled()})

    def _api_delegation_settings_put(self, qs) -> None:
        body = _json_body(self)
        if "enabled" not in body or not isinstance(body["enabled"], bool):
            raise ApiError(400, "body needs boolean 'enabled'")
        self.ctx.delegations.kv_set(
            "delegation.enabled", "true" if body["enabled"] else "false")
        try:
            self.ctx.approvals.audit(
                "dashboard", "delegation.settings",
                "enabled" if body["enabled"] else "disabled", {})
        except Exception:
            pass
        self._send_json({"enabled": body["enabled"]})

    def _api_list_delegations(self, qs) -> None:
        rows = self.ctx.delegations.list(limit=50)
        self._send_json({"delegations": [self._delegation_summary(r)
                                         for r in rows]})

    def _delegation_summary(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        results = {}
        try:
            results = json.loads(rec.get("results") or "{}")
        except Exception:
            pass
        counts = {"pending": 0, "running": 0, "completed": 0,
                  "failed": 0, "cancelled": 0}
        for r in results.values():
            st = r.get("status", "pending")
            counts[st] = counts.get(st, 0) + 1
        return {
            "mission_id": rec["mission_id"],
            "goal": rec["goal"],
            "status": rec["status"],
            "subtask_counts": counts,
            "subtasks_total": len(results),
            "model_calls": rec.get("model_calls") or 0,
            "workers_launched": rec.get("workers_launched") or 0,
            "runtime_seconds": rec.get("runtime_seconds") or 0,
            "error": rec.get("error") or "",
            "retry_of": rec.get("retry_of") or "",
            "created_at": rec["created_at"],
            "updated_at": rec["updated_at"],
        }

    def _parse_delegation_body(self, body: Dict[str, Any]):
        from . import delegation as deleg_mod
        if not self._delegation_enabled():
            raise ApiError(403, "multi-agent delegation is disabled")
        goal = str(body.get("goal", "")).strip()
        if not goal or len(goal) > 5000:
            raise ApiError(400, "goal is required (1-5000 chars)")
        raw_subtasks = body.get("subtasks", [])
        if not isinstance(raw_subtasks, list) or not raw_subtasks:
            raise ApiError(400, "subtasks must be a non-empty list")
        if len(raw_subtasks) > 50:
            raise ApiError(400, "at most 50 subtasks per mission")
        subtasks = []
        for i, raw in enumerate(raw_subtasks):
            if not isinstance(raw, dict):
                raise ApiError(400, f"subtask {i} must be an object")
            sid = str(raw.get("id", f"s{i}")).strip()
            if not sid or len(sid) > 64:
                raise ApiError(400, f"subtask {i} needs an id (1-64 chars)")
            kind = str(raw.get("kind", "agent"))
            if kind not in ("agent", "tool"):
                raise ApiError(400, f"subtask '{sid}': kind must be agent|tool")
            profile = str(raw.get("profile", "researcher"))
            if kind == "agent" and profile not in deleg_mod.WORKER_PROFILES:
                raise ApiError(
                    400, f"subtask '{sid}': unknown profile '{profile}'")
            tool = str(raw.get("tool", ""))
            if kind == "tool" and not tool:
                raise ApiError(400, f"subtask '{sid}': tool subtask needs a tool")
            sgoal = str(raw.get("goal", "")).strip()
            if kind == "agent" and (not sgoal or len(sgoal) > 5000):
                raise ApiError(
                    400, f"subtask '{sid}': agent subtask needs a goal")
            depends = raw.get("depends_on", [])
            if not isinstance(depends, list) or any(
                    not isinstance(d, str) for d in depends):
                raise ApiError(400, f"subtask '{sid}': depends_on must be a list of ids")
            criteria = raw.get("acceptance_criteria", [])
            if not isinstance(criteria, list):
                raise ApiError(400,
                               f"subtask '{sid}': acceptance_criteria must be a list")
            max_retries = raw.get("max_retries", 2)
            if not isinstance(max_retries, int) or not 0 <= max_retries <= 5:
                raise ApiError(400,
                               f"subtask '{sid}': max_retries must be 0-5")
            timeout_s = raw.get("timeout_s", 600)
            if not isinstance(timeout_s, (int, float)) or not 30 <= timeout_s <= 3600:
                raise ApiError(400,
                               f"subtask '{sid}': timeout_s must be 30-3600")
            subtasks.append(deleg_mod.Subtask(
                id=sid, name=str(raw.get("name", sid))[:200], goal=sgoal,
                kind=kind, profile=profile, tool=tool,
                args=dict(raw.get("args", {}) or {}),
                depends_on=list(depends), acceptance_criteria=list(criteria),
                max_retries=max_retries, timeout_s=int(timeout_s)))
        # DAG validation: duplicate ids, unknown deps, cycles.
        try:
            subtasks = deleg_mod.validate_plan(subtasks)
        except deleg_mod.DelegationError as exc:
            raise ApiError(400, f"invalid plan: {exc}")
        budgets_raw = body.get("budgets", {}) or {}
        if not isinstance(budgets_raw, dict):
            raise ApiError(400, "budgets must be an object")
        budgets = {}
        for key, (lo, hi, types) in self._BUDGET_BOUNDS.items():
            if key in budgets_raw:
                val = budgets_raw[key]
                if not isinstance(val, types) or isinstance(val, bool) \
                        or not lo <= val <= hi:
                    raise ApiError(400, f"budgets.{key} must be {lo}-{hi}")
                budgets[key] = val
        return goal, subtasks, budgets

    def _api_create_delegation(self, qs) -> None:
        from . import delegation as deleg_mod
        body = _json_body(self)
        goal, subtasks, budgets = self._parse_delegation_body(body)
        mission_id = "d_" + uuid.uuid4().hex[:12]
        plan = [dataclasses.asdict(s) for s in subtasks]
        self.ctx.delegations.create(mission_id, goal, plan, budgets,
                                    retry_of=body.get("retry_of") or None)
        self.ctx.approvals.audit("dashboard", "delegation.create", mission_id,
                                 {"goal": goal[:200], "subtasks": len(plan)})
        self.ctx.submit_delegation(mission_id)
        self._send_json({"mission_id": mission_id, "status": "created"}, 202)

    def _route_delegation(self, method: str, path: str, qs) -> None:
        # /api/delegations/<id>[/cancel|/retry]
        rest = path[len("/api/delegations/"):].split("/")
        mission_id = rest[0]
        if not mission_id:
            return self._error(400, "mission id required")
        action = rest[1] if len(rest) > 1 else ""
        if action == "cancel" and method == "POST":
            return self._api_cancel_delegation(mission_id)
        if action == "retry" and method == "POST":
            return self._api_retry_delegation(mission_id)
        if action == "" and method == "GET":
            return self._api_delegation_detail(mission_id)
        return self._error(405, "method not allowed")

    def _api_delegation_detail(self, mission_id: str) -> None:
        from .delegation import list_artifacts
        rec = self.ctx.delegations.get(mission_id)
        if rec is None:
            return self._error(404, "delegation not found")
        plan, results, budgets = [], {}, {}
        try:
            plan = json.loads(rec.get("plan") or "[]")
            results = json.loads(rec.get("results") or "{}")
            budgets = json.loads(rec.get("budgets") or "{}")
        except Exception:
            pass
        # Worker task records (latest per subtask; retries create new rows).
        tasks_by_sub: Dict[str, Dict[str, Any]] = {}
        for t in self.ctx.tasks.list_tasks(limit=500):
            params = t.get("params") or {}
            if params.get("mission_id") == mission_id and params.get("subtask_id"):
                sid = params["subtask_id"]
                prev = tasks_by_sub.get(sid)
                if prev is None or t.get("updated_at", 0) > prev.get("updated_at", 0):
                    tasks_by_sub[sid] = t
        workers = []
        for s in plan:
            sid = s.get("id")
            t = tasks_by_sub.get(sid) or {}
            r = results.get(sid) or {}
            status = t.get("status") or r.get("status") or "pending"
            workers.append({
                "subtask_id": sid,
                "name": s.get("name", sid),
                "goal": s.get("goal", ""),
                "kind": s.get("kind", "agent"),
                "profile": s.get("profile", ""),
                "tool": s.get("tool", ""),
                "depends_on": s.get("depends_on", []),
                "acceptance_criteria": s.get("acceptance_criteria", []),
                "status": status,
                "attempts": r.get("attempts", 1 if status != "pending" else 0),
                "task_id": t.get("id", ""),
                "worker_id": (t.get("params") or {}).get("worker_id", ""),
                "summary": r.get("summary", ""),
                "errors": r.get("errors", []),
                "error": t.get("error") or "",
                "artifacts": [a.get("name", "") for a in r.get("artifacts", [])],
                "updated_at": t.get("updated_at") or rec["updated_at"],
            })
        self._send_json({
            "mission_id": mission_id,
            "goal": rec["goal"],
            "status": rec["status"],
            "plan": plan,
            "budgets": budgets,
            "workers": workers,
            "results": results,
            "verification": self._safe_json(rec.get("verification")),
            "usage": {
                "model_calls": rec.get("model_calls") or 0,
                "runtime_seconds": rec.get("runtime_seconds") or 0,
                "workers_launched": rec.get("workers_launched") or 0,
            },
            "error": rec.get("error") or "",
            "retry_of": rec.get("retry_of") or "",
            "created_at": rec["created_at"],
            "updated_at": rec["updated_at"],
            "artifacts": list_artifacts(self.ctx.cfg.data_dir, mission_id),
        })

    def _safe_json(self, raw: Any) -> Any:
        if not raw:
            return None
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _api_cancel_delegation(self, mission_id: str) -> None:
        from . import delegation as deleg_mod
        rec = self.ctx.delegations.get(mission_id)
        if rec is None:
            return self._error(404, "delegation not found")
        if rec["status"] in self._DELEGATION_TERMINAL:
            return self._error(409, f"mission already {rec['status']}")
        live = deleg_mod.cancel_delegation_run(mission_id, self.ctx.cfg.data_dir)
        self.ctx.delegations.update(mission_id, status="cancelled",
                                    error="cancelled by user")
        self.ctx.approvals.audit("dashboard", "delegation.cancel", mission_id,
                                 {"live": live})
        self._send_json({"mission_id": mission_id, "status": "cancelled",
                         "live": live})

    def _api_retry_delegation(self, mission_id: str) -> None:
        from . import delegation as deleg_mod
        rec = self.ctx.delegations.get(mission_id)
        if rec is None:
            return self._error(404, "delegation not found")
        if rec["status"] not in ("completed", "failed"):
            return self._error(409, "only finished missions can be retried")
        if not self._delegation_enabled():
            return self._error(403, "multi-agent delegation is disabled")
        try:
            plan = json.loads(rec.get("plan") or "[]")
            results = json.loads(rec.get("results") or "{}")
            budgets = json.loads(rec.get("budgets") or "{}")
        except Exception:
            return self._error(500, "stored mission data is corrupt")
        failed = [sid for sid, r in results.items()
                  if r.get("status") != "completed"]
        if not failed:
            return self._error(409, "no failed subtasks to retry")
        failed_set = set(failed)
        retry_plan = []
        for s in plan:
            if s.get("id") in failed_set:
                s = dict(s)
                # Drop deps on subtasks outside the retry run; their
                # artifacts remain in the registry for reuse.
                s["depends_on"] = [d for d in s.get("depends_on", [])
                                   if d in failed_set]
                retry_plan.append(s)
        subtasks = [deleg_mod.Subtask(**{
            k: v for k, v in s.items()
            if k in deleg_mod.Subtask.__dataclass_fields__})
            for s in retry_plan]
        try:
            subtasks = deleg_mod.validate_plan(subtasks)
        except deleg_mod.DelegationError as exc:
            return self._error(400, f"invalid retry plan: {exc}")
        new_id = "d_" + uuid.uuid4().hex[:12]
        self.ctx.delegations.create(
            new_id, f"Retry of failed subtasks from {mission_id}",
            [dataclasses.asdict(s) for s in subtasks], budgets, retry_of=mission_id)
        self.ctx.approvals.audit("dashboard", "delegation.retry", new_id,
                                 {"from": mission_id, "subtasks": failed})
        self.ctx.submit_delegation(new_id)
        self._send_json({"mission_id": new_id, "status": "created",
                         "retry_of": mission_id, "subtasks": failed}, 202)

    def _api_sse_ticket(self, qs) -> None:
        # Issued only after Bearer auth (checked in _handle). Short-lived,
        # single-use, and never the long-lived dashboard token.
        self.ctx.rate.check(self.client_address[0], "sse_ticket")
        self._send_json(self.ctx.issue_sse_ticket())

    def _api_rotate_token(self, qs) -> None:
        # Authenticated + rate-limited in _handle/above. Requires explicit
        # confirmation in the body. Returns the new token ONCE; the caller
        # must store it — it is never logged or returned again.
        self.ctx.rate.check(self.client_address[0], "rotate_token")
        body = _json_body(self)
        if body.get("confirm") is not True:
            raise ApiError(400, "rotation requires explicit confirmation")
        auth = self.headers.get("Authorization", "")
        presented = auth[7:].strip() if auth.startswith("Bearer ") else ""
        new_token = self.ctx.rotate_token(presented)
        self._send_json({"token": new_token,
                         "message": "token rotated; the previous token is now invalid"})

    def _sse_events(self, task_id: str, qs) -> None:
        since = int((qs.get("since") or ["0"])[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            # Replay missed events, then stream new ones for up to 60s.
            deadline = time.time() + 60
            while time.time() < deadline:
                events = self.ctx.tasks.list_events(task_id, since_id=since)
                for ev in events:
                    since = ev["id"]
                    payload = "data: " + json.dumps(ev) + "\n\n"
                    self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
                task = self.ctx.tasks.get_task(task_id)
                if task and task["status"] in ("completed", "failed", "cancelled"):
                    self.wfile.write(b"event: done\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- API: approvals -------------------------------------------------------------
    def _api_list_approvals(self, qs) -> None:
        status = (qs.get("status") or [None])[0]
        recs = self.ctx.approvals.list(status=status)
        self._send_json({"approvals": recs})

    def _route_approval(self, method: str, path: str) -> None:
        approval_id = path[len("/api/approvals/"):].split("/")[0]
        if method != "POST":
            return self._error(404, "not found")
        self.ctx.rate.check(self.client_address[0], "approval_decide")
        body = _json_body(self)
        decision = body.get("decision")
        if decision not in ("approved", "rejected"):
            return self._error(400, "decision must be 'approved' or 'rejected'")
        rec = self.ctx.approvals.decide(approval_id, decision, decided_by="user")
        if rec is None:
            return self._error(409, "approval not found or already decided")
        self.ctx.gate.wake(approval_id)
        self._send_json({"approval": rec})

    def _api_policy(self, qs) -> None:
        from . import tools
        cfg = self.ctx.cfg
        info = []
        for t in tools.list_tools():
            info.append({
                "name": t.name, "description": t.description,
                "risk": t.risk, "requires_approval": t.requires_approval,
            })
        self._send_json({
            "workspace_root": cfg.workspace_root,
            "allowed_tools": cfg.allowed_tools,
            "max_step_attempts": cfg.max_step_attempts,
            "max_turns": cfg.max_turns,
            "task_timeout_s": cfg.task_timeout_s,
            "llm_timeout_s": cfg.llm_timeout_s,
            "tools": info,
        })

    # -- API: providers ---------------------------------------------------------------
    def _api_providers(self, qs) -> None:
        """Legacy summary endpoint; now backed by the provider store."""
        from . import providers as providers_mod
        store = self.ctx.provider_store
        providers_mod.seed_from_legacy_config(store, self.ctx.cfg)
        configs = store.list()
        default_id = store.get_setting("default_id")
        out = {}
        for c in configs:
            out[c["id"]] = {
                "name": c["name"], "adapter": c["adapter"],
                "display_name": c["display_name"], "model": c["model"],
                "base_url": c["base_url"], "enabled": c["enabled"],
                "is_default": c["id"] == default_id,
                "api_key_masked": c["api_key_masked"],
                "last_test_ok": c.get("last_test_ok"),
                "last_test_error": c.get("last_test_error", ""),
            }
        # Backwards-compatible gemini/ollama keys for old dashboard code.
        legacy = {}
        for c in configs:
            if c["adapter"] in ("gemini", "ollama") and c["adapter"] not in legacy:
                legacy[c["adapter"]] = {
                    "configured": c["enabled"] and bool(c["api_key_masked"] or c["adapter"] == "ollama"),
                    "model": c["model"], "url": c.get("base_url", ""),
                }
        out.update(legacy)
        self._send_json({"providers": out, "active": default_id,
                         "default_id": default_id,
                         "fallback_ids": store.get_fallbacks()})

    def _api_test_provider(self, qs) -> None:
        """Legacy: {"provider": "<config id or adapter name>"}."""
        from . import providers as providers_mod
        self.ctx.rate.check(self.client_address[0], "provider_test")
        body = _json_body(self)
        want = (body.get("provider") or "").strip()
        store = self.ctx.provider_store
        target = None
        for c in store.list():
            if c["id"] == want or c["adapter"] == want or c["name"] == want:
                target = c
                break
        if target is None:
            # Fall back to the old env-based checks for unmigrated setups.
            if want in ("gemini", ""):
                return self._send_json(_test_gemini())
            if want == "ollama":
                return self._send_json(_test_ollama(self.ctx.cfg))
            self._error(400, "unknown provider")
            return
        result = self._test_config(target["id"])
        self._send_json(result)

    def _test_config(self, pid: str) -> Dict[str, Any]:
        from . import providers as providers_mod
        store = self.ctx.provider_store
        row = store.get(pid, include_secret=True)
        if not row:
            return {"ok": False, "error": "unknown provider"}
        try:
            provider = providers_mod.build_provider(row)
        except providers_mod.ProviderError as exc:
            store.record_test(pid, False, str(exc))
            return {"ok": False, "error": str(exc)}
        try:
            result = provider.test_connection(timeout_s=15)
        except providers_mod.ProviderError as exc:
            result = {"ok": False, "error": str(exc)}
        except Exception as exc:  # never leak a traceback to the dashboard
            result = {"ok": False, "error": f"test failed: {exc}"}
        # Redact any secret that slipped into the message.
        secrets = [v for v in (row.get("api_key"), row.get("aws_secret_key")) if v]
        if not result.get("ok"):
            result["error"] = providers_mod.redact_secrets(result.get("error", ""), secrets)
        store.record_test(pid, bool(result.get("ok")), result.get("error", ""))
        return result

    # -- API: provider configs (CRUD) -------------------------------------------------
    def _route_providers(self, method: str, path: str) -> None:
        from . import providers as providers_mod
        rest = path[len("/api/providers/"):]
        parts = [p for p in rest.split("/") if p]
        store = self.ctx.provider_store

        if parts == ["presets"] and method == "GET":
            presets = []
            for p in providers_mod.PRESETS:
                presets.append({k: v for k, v in p.items()})
            return self._send_json({"presets": presets})

        if parts == ["routing"] and method == "GET":
            return self._send_json({
                "default_id": store.get_setting("default_id"),
                "fallback_ids": store.get_fallbacks()})

        if parts == ["routing"] and method == "POST":
            body = _json_body(self)
            try:
                if "default_id" in body and body["default_id"]:
                    store.set_default(body["default_id"])
                if "fallback_ids" in body:
                    ids = body["fallback_ids"]
                    if not isinstance(ids, list):
                        raise providers_mod.ProviderError("fallback_ids must be a list")
                    store.set_fallbacks([str(i) for i in ids])
            except providers_mod.ProviderError as exc:
                raise ApiError(400, str(exc))
            self.ctx.approvals.audit("user", "providers.routing", "",
                                     {"default": store.get_setting("default_id"),
                                      "fallbacks": store.get_fallbacks()})
            return self._send_json({"default_id": store.get_setting("default_id"),
                                    "fallback_ids": store.get_fallbacks()})

        if parts == ["configs"] and method == "GET":
            configs = store.list()
            for c in configs:
                c["capabilities"] = providers_mod.adapter_capabilities(
                    c["adapter"], c.get("capabilities_override"))
            return self._send_json({"configs": configs,
                                    "default_id": store.get_setting("default_id"),
                                    "fallback_ids": store.get_fallbacks()})

        if parts == ["configs"] and method == "POST":
            body = _json_body(self)
            try:
                cfg_row = store.create(body)
            except providers_mod.ProviderError as exc:
                raise ApiError(400, str(exc))
            cfg_row["capabilities"] = providers_mod.adapter_capabilities(
                cfg_row["adapter"], cfg_row.get("capabilities_override"))
            self.ctx.approvals.audit("user", "providers.created", cfg_row["id"],
                                     {"name": cfg_row["name"], "adapter": cfg_row["adapter"]})
            return self._send_json(cfg_row, status=201)

        if len(parts) >= 2 and parts[0] == "configs":
            pid = parts[1]
            if len(parts) == 2 and method == "GET":
                row = store.get(pid)
                if not row:
                    return self._error(404, "unknown provider")
                row["capabilities"] = providers_mod.adapter_capabilities(
                    row["adapter"], row.get("capabilities_override"))
                return self._send_json(row)
            if len(parts) == 2 and method == "PUT":
                body = _json_body(self)
                try:
                    row = store.update(pid, body)
                except providers_mod.ProviderError as exc:
                    raise ApiError(400, str(exc))
                row["capabilities"] = providers_mod.adapter_capabilities(
                    row["adapter"], row.get("capabilities_override"))
                self.ctx.approvals.audit("user", "providers.updated", pid, {})
                return self._send_json(row)
            if len(parts) == 2 and method == "DELETE":
                try:
                    store.delete(pid)
                except providers_mod.ProviderError as exc:
                    raise ApiError(400, str(exc))
                self.ctx.approvals.audit("user", "providers.deleted", pid, {})
                return self._send_json({"deleted": pid})
            if len(parts) == 3 and parts[2] == "test" and method == "POST":
                self.ctx.rate.check(self.client_address[0], "provider_test")
                return self._send_json(self._test_config(pid))
            if len(parts) == 3 and parts[2] == "models" and method == "GET":
                row = store.get(pid, include_secret=True)
                if not row:
                    return self._error(404, "unknown provider")
                try:
                    provider = providers_mod.build_provider(row)
                    models = provider.list_models(timeout_s=15)
                except providers_mod.ProviderError as exc:
                    return self._send_json({"models": [], "error": str(exc)})
                except Exception as exc:
                    return self._send_json({"models": [], "error": f"discovery failed: {exc}"})
                return self._send_json({"models": models})
            if len(parts) == 3 and parts[2] == "chat" and method == "POST":
                return self._provider_chat(pid)
        self._error(404, "not found")

    def _provider_chat(self, pid: str) -> None:
        """Send a user test prompt to one provider; return text + usage."""
        from . import providers as providers_mod
        self.ctx.rate.check(self.client_address[0], "provider_chat")
        body = _json_body(self)
        prompt = (body.get("prompt") or "").strip()
        if not prompt or len(prompt) > 4000:
            raise ApiError(400, "prompt must be 1..4000 chars")
        store = self.ctx.provider_store
        row = store.get(pid, include_secret=True)
        if not row:
            return self._error(404, "unknown provider")
        try:
            provider = providers_mod.build_provider(row)
        except providers_mod.ProviderError as exc:
            raise ApiError(400, str(exc))
        timeout_s = min(int(body.get("timeout_s", row.get("timeout_s", 60))), 120)
        try:
            resp = provider.chat("You are a helpful assistant. Answer briefly.",
                                 [{"role": "user", "text": prompt}], [],
                                 timeout_s=timeout_s)
        except providers_mod.ProviderError as exc:
            secrets = [v for v in (row.get("api_key"), row.get("aws_secret_key")) if v]
            raise ApiError(502, providers_mod.redact_secrets(str(exc), secrets))
        self._send_json({
            "text": resp.text[:4000], "finish_reason": resp.finish_reason,
            "latency_ms": resp.latency_ms, "adapter": row["adapter"],
            "model": row["model"],
        })

    def _api_config_provider(self, qs) -> None:
        body = _json_body(self)
        cfg = self.ctx.cfg
        changed = {}
        for k, v in body.items():
            if k not in MUTABLE_CONFIG or not hasattr(cfg, k):
                raise ApiError(400, f"config key not allowed: {k}")
            setattr(cfg, k, v)
            changed[k] = v
        path = os.path.join(cfg.data_dir, OVERRIDES_FILE)
        try:
            saved = json.load(open(path)) if os.path.exists(path) else {}
        except Exception:
            saved = {}
        saved.update(changed)
        with open(path, "w") as f:
            json.dump(saved, f, indent=2)
        self.ctx.approvals.audit("user", "config.updated", "", {"keys": sorted(changed)})
        self._send_json({"updated": changed})

    # -- API: scheduler -----------------------------------------------------------------
    def _api_sched_jobs(self, qs) -> None:
        from .scheduler import SchedulerStore
        store = SchedulerStore(self.ctx.cfg.data_dir)
        jobs = store.list_jobs()
        for j in jobs:
            runs = store.list_runs(j["id"], limit=1)
            j["last_run"] = dict(runs[0]) if runs else None
        self._send_json({"jobs": jobs})

    def _api_sched_runs(self, qs) -> None:
        from .scheduler import SchedulerStore
        job = (qs.get("job") or [None])[0]
        limit = min(int((qs.get("limit") or ["20"])[0]), 100)
        store = SchedulerStore(self.ctx.cfg.data_dir)
        if job:
            runs = [dict(r) for r in store.list_runs(job, limit=limit)]
        else:
            runs = []
            for j in store.list_jobs():
                runs.extend(dict(r) for r in store.list_runs(j["id"], limit=5))
            runs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
            runs = runs[:limit]
        self._send_json({"runs": runs})

    def _route_sched_job(self, method: str, path: str) -> None:
        from .scheduler import Scheduler, SchedulerStore
        rest = path[len("/api/scheduler/jobs/"):].split("/")
        job_id, action = rest[0], (rest[1] if len(rest) > 1 else "")
        store = SchedulerStore(self.ctx.cfg.data_dir)
        job = store.get_job(job_id)
        if not job:
            return self._error(404, "unknown job")
        if method == "POST" and action in ("enable", "disable"):
            store.set_enabled(job_id, action == "enable")
            self.ctx.approvals.audit("user", f"scheduler.{action}", "", {"job": job_id})
            return self._send_json({"job_id": job_id, "enabled": action == "enable"})
        if method == "POST" and action == "run-now":
            self.ctx.rate.check(self.client_address[0], "sched_runnow")
            # Run the job inline in a background thread — same code path as
            # the daemon and the CLI. The atomic claim prevents duplicates.
            sch = Scheduler(self.ctx.cfg)

            def _run():
                try:
                    sch.run_job(job_id, "api")
                except Exception as exc:
                    sys.stderr.write(f"scheduler run-now failed: {exc}\n")
            threading.Thread(target=_run, daemon=True).start()
            self.ctx.approvals.audit("user", "scheduler.run-now", "", {"job": job_id})
            return self._send_json({"job_id": job_id, "status": "triggered"}, status=202)
        self._error(404, "not found")

    # -- API: audit + memory ---------------------------------------------------------------
    def _api_audit(self, qs) -> None:
        limit = min(int((qs.get("limit") or ["100"])[0]), 500)
        self._send_json({"events": self.ctx.approvals.list_audit(limit=limit)})

    def _api_memory(self, qs) -> None:
        from .memory import MemoryStore
        query = (qs.get("query") or [""])[0]
        limit = min(int((qs.get("limit") or ["10"])[0]), 50)
        mem = MemoryStore(self.ctx.cfg.data_dir)
        entries = mem.recall(query, limit=limit) if query else mem.list_entries(limit=limit)
        self._send_json({"entries": [
            {"id": e.id, "text": e.text, "tags": e.tags, "source": e.source,
             "created_at": e.created_at, "updated_at": e.updated_at}
            for e in entries
        ]})


def _test_gemini() -> Dict[str, Any]:
    """Validate the Gemini key with a models.list call (no generation quota)."""
    key = os.environ.get("NEXTCLAW_GEMINI_API_KEY", "")
    if not key:
        return {"ok": False, "error": "NEXTCLAW_GEMINI_API_KEY is not set"}
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={urllib.parse.quote(key)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nextclaw/0.2"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "").split("/")[-1] for m in data.get("models", [])][:5]
        return {"ok": True, "models_sample": models}
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            return {"ok": False, "error": "key rejected by Google (invalid key)"}
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error": f"connection failed: {exc}"}


def _test_ollama(cfg) -> Dict[str, Any]:
    url = (cfg.ollama_url or "").rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nextclaw/0.2"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name") for m in data.get("models", [])]
        want = cfg.ollama_model
        ok = (not want) or any((want in (m or "")) for m in models)
        return {"ok": ok, "models": models,
                "error": "" if ok else f"model '{want}' not found locally"}
    except Exception as exc:
        return {"ok": False, "error": f"cannot reach Ollama at {cfg.ollama_url}: {exc}"}


def serve(cfg, host: str = "127.0.0.1", port: int = 8765) -> int:
    host = os.environ.get("NEXTCLAW_API_HOST", host)
    port = int(os.environ.get("NEXTCLAW_API_PORT", port))
    ctx = ServerContext(cfg)
    Handler.ctx = ctx
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"NextClaw dashboard: http://{host}:{port}/")
    print(f"API token: {ctx.token}")
    print("Keep this token secret — it controls your agents.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctx.runner.shutdown(wait=False)
    return 0
