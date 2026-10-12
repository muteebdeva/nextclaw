"""Long-running background missions.

Extends the task/checkpoint model (tasks.db) with durable multi-step
missions that survive process restarts:

    CREATED -> PLANNING -> RUNNING -> VERIFYING -> COMPLETED
                          |          |
                          v          v
                       PAUSED   WAITING_FOR_APPROVAL
                          |
                          v
                    FAILED / CANCELLED

- Persistent mission state + step checkpoints in SQLite.
- Pause / resume / cancel; recovery replays only incomplete steps.
- Idempotency keys: completed actions are never repeated after recovery.
- Bounded retries, per-mission budgets (steps, tool calls, runtime, cost).
- Structured failure/blocker reporting; approval gating reuses the
  existing approval store.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

MISSION_STATES = (
    "created",
    "planning",
    "running",
    "verifying",
    "paused",
    "waiting_for_approval",
    "failed",
    "cancelled",
    "completed",
)
TERMINAL_STATES = ("failed", "cancelled", "completed")

STEP_STATUSES = ("pending", "running", "completed", "failed", "skipped")


class MissionError(Exception):
    """Mission-level failure."""


@dataclass
class MissionBudgets:
    max_steps: int = 50
    max_tool_calls: int = 200
    max_runtime_s: int = 3600
    max_retries_per_step: int = 3
    max_cost_usd: float = 0.0  # 0 = no cost cap enforced


class MissionStore:
    """SQLite persistence for missions (same data_dir as TaskStore)."""

    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "tasks.db")
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS missions (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'created',
                    plan_json TEXT NOT NULL DEFAULT '[]',
                    budgets_json TEXT NOT NULL DEFAULT '{}',
                    acceptance_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT DEFAULT '',
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    tool_calls INTEGER NOT NULL DEFAULT 0,
                    started_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mission_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT NOT NULL,
                    ord INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'tool',
                    tool TEXT NOT NULL DEFAULT '',
                    args_json TEXT NOT NULL DEFAULT '{}',
                    depends_on TEXT NOT NULL DEFAULT '[]',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error TEXT DEFAULT '',
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(mission_id) REFERENCES missions(id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_msteps_mission ON mission_steps(mission_id, ord)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mission_actions (
                    idempotency_key TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    completed_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mission_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(mission_id) REFERENCES missions(id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mevents_mission ON mission_events(mission_id, id)"
            )

    # -- missions ------------------------------------------------------
    def create_mission(
        self,
        goal: str,
        plan: Optional[List[Dict[str, Any]]] = None,
        budgets: Optional[Dict[str, Any]] = None,
        acceptance_criteria: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        mid = "m_" + uuid.uuid4().hex[:12]
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO missions (id, goal, state, plan_json, budgets_json,"
                " acceptance_json, created_at, updated_at)"
                " VALUES (?, ?, 'created', ?, ?, ?, ?, ?)",
                (
                    mid, goal, json.dumps(plan or []), json.dumps(budgets or {}),
                    json.dumps(acceptance_criteria or []), now, now,
                ),
            )
        self.add_event(mid, "created", {"goal": goal})
        if plan:
            self.add_steps(mid, plan)
        return mid

    def get_mission(self, mission_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM missions WHERE id = ?", (mission_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("plan_json", "budgets_json", "acceptance_json"):
            d[k.replace("_json", "")] = json.loads(d.pop(k) or ("[]" if "acceptance" in k or "plan" in k else "{}"))
        return d

    def set_state(self, mission_id: str, state: str, error: str = "") -> None:
        if state not in MISSION_STATES:
            raise ValueError(f"bad mission state: {state}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE missions SET state = ?, error = ?, updated_at = ? WHERE id = ?",
                (state, error, time.time(), mission_id),
            )
        self.add_event(mission_id, "state", {"state": state, "error": error})

    def request_pause(self, mission_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE missions SET pause_requested = 1, updated_at = ? WHERE id = ?",
                (time.time(), mission_id),
            )

    def request_cancel(self, mission_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE missions SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                (time.time(), mission_id),
            )

    def clear_pause(self, mission_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE missions SET pause_requested = 0, updated_at = ? WHERE id = ?",
                (time.time(), mission_id),
            )

    def bump_tool_calls(self, mission_id: str, n: int = 1) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE missions SET tool_calls = tool_calls + ?, updated_at = ? WHERE id = ?",
                (n, time.time(), mission_id),
            )

    def list_missions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, goal, state, error, created_at, updated_at FROM missions"
                " ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- steps ---------------------------------------------------------
    def add_steps(self, mission_id: str, steps: List[Dict[str, Any]]) -> None:
        now = time.time()
        with self._connect() as conn:
            base = conn.execute(
                "SELECT COALESCE(MAX(ord), -1) FROM mission_steps WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()[0]
            for i, s in enumerate(steps):
                conn.execute(
                    "INSERT INTO mission_steps (mission_id, ord, name, kind, tool,"
                    " args_json, depends_on, idempotency_key, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        mission_id, base + 1 + i, s.get("name", f"step-{i}"),
                        s.get("kind", "tool"), s.get("tool", ""),
                        json.dumps(s.get("args", {})),
                        json.dumps(s.get("depends_on", [])),
                        s.get("idempotency_key", "") or f"{mission_id}:{base + 1 + i}",
                        now,
                    ),
                )
        self.add_event(mission_id, "plan", {"steps": len(steps)})

    def get_steps(self, mission_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM mission_steps WHERE mission_id = ? ORDER BY ord",
                (mission_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["args"] = json.loads(d.pop("args_json") or "{}")
            d["depends_on"] = json.loads(d.pop("depends_on") or "[]")
            raw_result = d.pop("result_json", None)
            d["result"] = json.loads(raw_result) if raw_result else None
            out.append(d)
        return out

    def update_step(self, step_id: int, status: str, result=None, error: str = "") -> None:
        if status not in STEP_STATUSES:
            raise ValueError(f"bad step status: {status}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE mission_steps SET status = ?, result_json = ?, error = ?,"
                " attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (status, json.dumps(result) if result is not None else None,
                 error, time.time(), step_id),
            )

    def set_step_status(self, step_id: int, status: str) -> None:
        if status not in STEP_STATUSES:
            raise ValueError(f"bad step status: {status}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE mission_steps SET status = ?, updated_at = ? WHERE id = ?",
                (status, time.time(), step_id),
            )

    def reset_running_steps(self, mission_id: str) -> int:
        """After a crash/restart, steps stuck in 'running' become pending again.

        Completed steps are never reset: idempotency keys in mission_actions
        additionally guard against re-execution.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE mission_steps SET status = 'pending', updated_at = ?"
                " WHERE mission_id = ? AND status = 'running'",
                (time.time(), mission_id),
            )
            return cur.rowcount

    # -- idempotency ---------------------------------------------------
    def action_done(self, idempotency_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM mission_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return row is not None

    def record_action(self, mission_id: str, idempotency_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO mission_actions"
                " (idempotency_key, mission_id, completed_at) VALUES (?, ?, ?)",
                (idempotency_key, mission_id, time.time()),
            )

    # -- events --------------------------------------------------------
    def add_event(self, mission_id: str, kind: str,
                  data: Optional[Dict[str, Any]] = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO mission_events (mission_id, ts, kind, data_json)"
                " VALUES (?, ?, ?, ?)",
                (mission_id, time.time(), kind, json.dumps(data or {})),
            )

    def list_events(self, mission_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, kind, data_json FROM mission_events"
                " WHERE mission_id = ? ORDER BY id DESC LIMIT ?",
                (mission_id, limit),
            ).fetchall()
        return [
            {"ts": r[0], "kind": r[1], "data": json.loads(r[2] or "{}")} for r in rows
        ]


# ---------------------------------------------------------------------------
# Mission runner
# ---------------------------------------------------------------------------

def _budgets_from(mission: Dict[str, Any]) -> MissionBudgets:
    b = mission.get("budgets") or {}
    known = {f for f in MissionBudgets.__dataclass_fields__}
    return MissionBudgets(**{k: v for k, v in b.items() if k in known})


def _failure_report(mission_id: str, store: MissionStore, reason: str) -> Dict[str, Any]:
    steps = store.get_steps(mission_id)
    failed = [s for s in steps if s["status"] == "failed"]
    return {
        "mission_id": mission_id,
        "reason": reason,
        "failed_steps": [
            {"name": s["name"], "tool": s["tool"], "attempts": s["attempts"],
             "error": s["error"][:500]}
            for s in failed
        ],
        "completed_steps": sum(1 for s in steps if s["status"] == "completed"),
        "total_steps": len(steps),
    }


def run_mission(
    mission_id: str,
    cfg,
    approvals: Tuple[str, ...] = (),
    approver: Optional[Callable] = None,
    planner: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    verifier: Optional[Callable] = None,
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Execute (or resume) a mission. Returns the final mission record.

    - planner: optional callable(goal) -> steps; used when the mission has no
      plan yet (CREATED/PLANNING with zero steps).
    - verifier: optional callable(criteria, cfg) -> list of result dicts; used
      in the VERIFYING state.
    - Recovery: steps stuck in 'running' are reset to pending; completed
      actions (by idempotency key) are never re-executed.
    """
    from . import tools as tools_mod
    from .approvals import ApprovalStore

    store = MissionStore(cfg.data_dir)
    mission = store.get_mission(mission_id)
    if mission is None:
        raise MissionError(f"unknown mission '{mission_id}'")
    if mission["state"] in TERMINAL_STATES:
        return mission  # never re-execute a finished mission

    def emit(kind: str, data: Dict[str, Any]) -> None:
        store.add_event(mission_id, kind, data)
        if on_event:
            try:
                on_event(kind, data)
            except Exception:
                pass

    # -- planning ------------------------------------------------------
    steps = store.get_steps(mission_id)
    if not steps and mission["state"] in ("created", "planning"):
        store.set_state(mission_id, "planning")
        if planner is not None:
            plan = planner(mission["goal"]) or []
            if plan:
                store.add_steps(mission_id, plan)
                steps = store.get_steps(mission_id)
        if not store.get_steps(mission_id):
            # No planner (or empty plan): wait for steps to be added.
            emit("planning", {"note": "no plan yet; add steps then resume"})
            return store.get_mission(mission_id)

    # -- recovery ------------------------------------------------------
    reset = store.reset_running_steps(mission_id)
    if reset:
        emit("recovery", {"reset_running_steps": reset})

    budgets = _budgets_from(store.get_mission(mission_id))
    mission = store.get_mission(mission_id)
    if not mission.get("started_at"):
        with store._connect() as conn:
            conn.execute(
                "UPDATE missions SET started_at = ?, updated_at = ? WHERE id = ?",
                (time.time(), time.time(), mission_id),
            )
        mission = store.get_mission(mission_id)
    deadline = mission["started_at"] + budgets.max_runtime_s
    store.set_state(mission_id, "running")
    approval_store = ApprovalStore(cfg.data_dir)

    while True:
        mission = store.get_mission(mission_id)
        if mission["cancel_requested"]:
            store.set_state(mission_id, "cancelled", error="cancel requested")
            emit("cancelled", {})
            return store.get_mission(mission_id)
        if mission["pause_requested"]:
            store.set_state(mission_id, "paused")
            emit("paused", {})
            return store.get_mission(mission_id)
        if time.time() > deadline:
            report = _failure_report(mission_id, store, "runtime budget exceeded")
            store.set_state(mission_id, "failed", error="runtime budget exceeded")
            emit("failed", report)
            return store.get_mission(mission_id)
        if mission["tool_calls"] >= budgets.max_tool_calls:
            report = _failure_report(mission_id, store, "tool-call budget exceeded")
            store.set_state(mission_id, "failed", error="tool-call budget exceeded")
            emit("failed", report)
            return store.get_mission(mission_id)

        steps = store.get_steps(mission_id)
        if len(steps) > budgets.max_steps:
            report = _failure_report(mission_id, store, "step budget exceeded")
            store.set_state(mission_id, "failed", error="step budget exceeded")
            emit("failed", report)
            return store.get_mission(mission_id)

        done_names = {s["name"] for s in steps if s["status"] == "completed"}
        runnable = None
        for s in steps:
            if s["status"] == "pending" and all(
                d in done_names for d in s["depends_on"]
            ):
                runnable = s
                break

        if runnable is None:
            if steps and all(s["status"] == "completed" for s in steps):
                break  # -> verifying
            if any(s["status"] == "failed" for s in steps):
                report = _failure_report(mission_id, store, "a step failed")
                store.set_state(mission_id, "failed", error="a step failed")
                emit("failed", report)
                return store.get_mission(mission_id)
            report = _failure_report(
                mission_id, store, "no runnable step (dependency deadlock?)")
            store.set_state(mission_id, "failed", error="no runnable step")
            emit("failed", report)
            return store.get_mission(mission_id)

        # Idempotency: never re-execute a completed action.
        if store.action_done(runnable["idempotency_key"]):
            store.set_step_status(runnable["id"], "completed")
            emit("step", {"name": runnable["name"], "skipped": "idempotent"})
            continue

        store.set_step_status(runnable["id"], "running")
        emit("step_start", {"name": runnable["name"], "tool": runnable["tool"]})

        # -- execute ---------------------------------------------------
        try:
            tools_mod.check_permission(
                runnable["tool"], runnable["args"], cfg, approvals)
        except tools_mod.ApprovalRequired as exc:
            if approver is not None and approver(
                mission_id, runnable["tool"], runnable["args"], str(exc)
            ):
                approvals = approvals + (runnable["tool"],)
            elif approver is not None:
                store.update_step(runnable["id"], "failed",
                                  error="approval denied by user")
                report = _failure_report(mission_id, store, "approval denied")
                store.set_state(mission_id, "failed", error="approval denied")
                emit("failed", report)
                return store.get_mission(mission_id)
            else:
                approval_store.request(
                    mission_id, runnable["tool"], runnable["args"], str(exc))
                store.set_step_status(runnable["id"], "pending")
                store.set_state(mission_id, "waiting_for_approval",
                                error=str(exc))
                emit("waiting_for_approval",
                     {"tool": runnable["tool"], "step": runnable["name"]})
                return store.get_mission(mission_id)
        except tools_mod.ToolError as exc:
            # Denied by policy: do not retry, fail the mission honestly.
            store.update_step(runnable["id"], "failed",
                              error=f"denied by policy: {exc}")
            report = _failure_report(mission_id, store, "permission denied")
            store.set_state(mission_id, "failed",
                            error=f"permission denied: {exc}")
            emit("failed", report)
            return store.get_mission(mission_id)

        try:
            res = tools_mod.execute(
                runnable["tool"], runnable["args"], cfg, approvals)
        except Exception as exc:  # defensive: execute() normally returns ToolResult
            res = None
            exec_error = str(exc)
        else:
            exec_error = "" if res.ok else res.error

        store.bump_tool_calls(mission_id)
        if exec_error:
            store.update_step(runnable["id"], "failed", error=exec_error[:2000])
            attempts = runnable["attempts"] + 1
            if attempts < budgets.max_retries_per_step:
                store.set_step_status(runnable["id"], "pending")
                emit("step_retry", {"name": runnable["name"],
                                    "attempt": attempts, "error": exec_error[:300]})
                continue
            report = _failure_report(
                mission_id, store,
                f"step '{runnable['name']}' failed after {attempts} attempts")
            store.set_state(mission_id, "failed",
                            error=f"step '{runnable['name']}' failed")
            emit("failed", report)
            return store.get_mission(mission_id)

        store.record_action(mission_id, runnable["idempotency_key"])
        store.update_step(runnable["id"], "completed", result=res.output)
        emit("step", {"name": runnable["name"], "ok": True})

    # -- verifying ------------------------------------------------------
    store.set_state(mission_id, "verifying")
    criteria = (store.get_mission(mission_id) or {}).get("acceptance") or []
    if criteria and verifier is not None:
        results = verifier(criteria, cfg)
        failures = [r for r in results if r.get("status") == "fail"]
        inconclusive = [r for r in results if r.get("status") == "inconclusive"]
        emit("verification", {"results": results})
        if failures:
            store.set_state(
                mission_id, "failed",
                error="acceptance criteria failed: "
                + ", ".join(r.get("criterion", "?") for r in failures))
            return store.get_mission(mission_id)
        if inconclusive:
            # Inconclusive is not success: report honestly, do not auto-pass.
            store.set_state(
                mission_id, "failed",
                error="acceptance criteria inconclusive: "
                + ", ".join(r.get("criterion", "?") for r in inconclusive))
            return store.get_mission(mission_id)
    elif criteria:
        emit("verification", {"note": "criteria defined but no verifier wired; "
                                      "marked by step completion only"})

    store.set_state(mission_id, "completed")
    emit("completed", {"steps": len(store.get_steps(mission_id))})
    return store.get_mission(mission_id)


def pause_mission(mission_id: str, data_dir: str) -> None:
    MissionStore(data_dir).request_pause(mission_id)


def cancel_mission(mission_id: str, data_dir: str) -> None:
    MissionStore(data_dir).request_cancel(mission_id)


def resume_mission(mission_id: str, cfg, **kwargs) -> Dict[str, Any]:
    """Resume a paused / waiting mission. Clears the pause flag first."""
    store = MissionStore(cfg.data_dir)
    mission = store.get_mission(mission_id)
    if mission is None:
        raise MissionError(f"unknown mission '{mission_id}'")
    if mission["state"] not in ("paused", "waiting_for_approval", "failed"):
        raise MissionError(
            f"mission '{mission_id}' is {mission['state']}; nothing to resume")
    store.clear_pause(mission_id)
    if mission["state"] == "failed":
        # Resume-from-failure: reset failed steps to pending for a fresh attempt.
        with store._connect() as conn:
            conn.execute(
                "UPDATE mission_steps SET status = 'pending', error = '',"
                " updated_at = ? WHERE mission_id = ? AND status = 'failed'",
                (time.time(), mission_id),
            )
    return run_mission(mission_id, cfg, **kwargs)
