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

import hmac
import json
import mimetypes
import os
import secrets
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
        # Migrate old llm_provider/llm_model/env settings once, idempotently.
        try:
            seed_from_legacy_config(self.provider_store, cfg)
        except Exception:
            pass
        self.runner = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nc-task")
        self.running: Dict[str, Any] = {}  # task_id -> Future
        self.rate = RateLimiter()
        self.token = self._load_token(cfg.data_dir)

    # -- auth -----------------------------------------------------------
    @staticmethod
    def _load_token(data_dir: str) -> str:
        env_token = os.environ.get("NEXTCLAW_API_TOKEN", "").strip()
        if env_token:
            return env_token
        path = os.path.join(data_dir, "api_token")
        if os.path.exists(path):
            return open(path).read().strip()
        token = secrets.token_urlsafe(32)
        with open(path, "w") as f:
            f.write(token)
        os.chmod(path, 0o600)
        return token

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
                if mode == "agent":
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
        # Auth: Bearer header normally; the SSE endpoint also accepts ?token=
        # because EventSource cannot set request headers.
        if "/events" in path and (qs.get("token") or [""])[0]:
            if not hmac.compare_digest((qs.get("token") or [""])[0], self.ctx.token):
                raise ApiError(401, "invalid token")
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
        }
        handler = routes.get((method, path))
        if handler:
            return handler(qs)

        # Parameterized routes.
        if path.startswith("/api/tasks/"):
            return self._route_task(method, path, qs)
        if path.startswith("/api/approvals/"):
            return self._route_approval(method, path)
        if path.startswith("/api/scheduler/jobs/"):
            return self._route_sched_job(method, path)
        if path.startswith("/api/providers/"):
            return self._route_providers(method, path)
        self._error(404, "not found")

    # -- static dashboard -----------------------------------------------------
    def _serve_static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        if ".." in rel or rel.startswith("/"):
            return self._error(400, "bad path")
        full = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(full):
            full = os.path.join(WEB_DIR, "index.html")  # SPA fallback
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
        })

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
        if mode == "agent":
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
