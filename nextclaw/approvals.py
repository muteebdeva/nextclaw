"""Human approval gate + audit log for high-risk tool actions.

An *approver* is a callable ``(task_id, tool_name, args, reason) -> bool``.
Agent loops consult it when a tool requires approval; the server wires an
ApprovalGate that blocks until a human decides via the API/UI, while the CLI
wires a pre-approval set from ``--approve``.

A model's request is never permission by itself: without an explicit
approval the action is denied and the denial is audited.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

APPROVAL_STATUSES = {"pending", "approved", "rejected", "expired"}

Approver = Callable[[str, str, Dict[str, Any], str], bool]


class ApprovalStore:
    """SQLite-backed approval requests and audit events (shares tasks.db)."""

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
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    args_json TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    decided_at REAL,
                    decided_by TEXT DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    task_id TEXT DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts)")

    # -- approvals ------------------------------------------------------
    def request(self, task_id: str, tool: str, args: Dict[str, Any], reason: str = "") -> str:
        approval_id = uuid.uuid4().hex[:12]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO approvals (id, task_id, tool, args_json, reason, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (approval_id, task_id, tool, json.dumps(args), reason, time.time()),
            )
        return approval_id

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            return self._row(row)

    def list(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [self._row(r) for r in rows]

    def pending_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE task_id = ? AND status = 'pending'"
                " ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
            return [self._row(r) for r in rows]

    def decide(self, approval_id: str, decision: str, decided_by: str = "user") -> Optional[Dict[str, Any]]:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be 'approved' or 'rejected'")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?"
                " WHERE id = ? AND status = 'pending'",
                (decision, time.time(), decided_by, approval_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            return self._row(row)

    def expire_stale(self, older_than_s: float = 3600) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
                (time.time() - older_than_s,),
            )
            return cur.rowcount

    @staticmethod
    def _row(row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        d = dict(row)
        d["args"] = json.loads(d["args_json"])
        return d

    # -- audit ----------------------------------------------------------
    def audit(self, actor: str, action: str, task_id: str = "", detail: Optional[Dict[str, Any]] = None) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO audit_events (ts, actor, action, task_id, detail_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (time.time(), actor, action, task_id, json.dumps(detail or {})),
            )
            return int(cur.lastrowid)

    def list_audit(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, actor, action, task_id, detail_json FROM audit_events"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"id": r[0], "ts": r[1], "actor": r[2], "action": r[3],
                 "task_id": r[4], "detail": json.loads(r[5])}
                for r in rows
            ]


class ApprovalGate:
    """Blocking approver: pauses the agent until a human decides.

    Used by the API server. ``decide()`` on the store wakes the waiter;
    ``cancel()`` wakes it with denial (used on task cancellation).
    """

    def __init__(self, store: ApprovalStore, task_store=None, timeout_s: float = 1800):
        self.store = store
        self.task_store = task_store
        self.timeout_s = timeout_s
        self._events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def __call__(self, task_id: str, tool: str, args: Dict[str, Any], reason: str) -> bool:
        approval_id = self.store.request(task_id, tool, args, reason)
        self.store.audit("system", "approval.requested", task_id,
                         {"approval_id": approval_id, "tool": tool})
        if self.task_store is not None:
            self.task_store.add_event(task_id, "approval_requested",
                                      {"approval_id": approval_id, "tool": tool, "reason": reason})
            try:
                self.task_store.set_task_status(task_id, "awaiting_approval")
            except Exception:
                pass
        ev = threading.Event()
        with self._lock:
            self._events[approval_id] = ev
        # Poll the DB (not just the event) so decisions made by other
        # processes — and task cancellation — are honored promptly.
        deadline = time.time() + self.timeout_s
        outcome: Optional[bool] = None
        try:
            while time.time() < deadline:
                rec = self.store.get(approval_id)
                if rec is not None and rec["status"] != "pending":
                    outcome = rec["status"] == "approved"
                    break
                if self.task_store is not None and self.task_store.cancel_requested(task_id):
                    self.store.decide(approval_id, "rejected", decided_by="system-cancel")
                    outcome = False
                    break
                ev.wait(2.0)
        finally:
            with self._lock:
                self._events.pop(approval_id, None)
        rec = self.store.get(approval_id)
        if outcome is None:
            if rec is not None and rec["status"] == "pending":
                self.store.decide(approval_id, "rejected", decided_by="system-timeout")
            self.store.audit("system", "approval.timeout", task_id, {"approval_id": approval_id})
            return False
        self.store.audit((rec or {}).get("decided_by") or "user",
                         f"approval.{(rec or {}).get('status', 'unknown')}", task_id,
                         {"approval_id": approval_id, "tool": tool})
        return outcome

    def wake(self, approval_id: str) -> None:
        """Wake a waiter after an external decision (API/UI/CLI)."""
        with self._lock:
            ev = self._events.get(approval_id)
        if ev is not None:
            ev.set()

    def cancel_task(self, task_id: str) -> None:
        """Wake all waiters for a task with denial (task cancelled)."""
        for rec in self.store.pending_for_task(task_id):
            self.store.decide(rec["id"], "rejected", decided_by="system-cancel")
            self.wake(rec["id"])


def preapproved_approver(approved_tools) -> Approver:
    """Approver for the CLI ``--approve`` path: only listed tools pass."""
    allowed = set(approved_tools or ())

    def _approve(task_id: str, tool: str, args: Dict[str, Any], reason: str) -> bool:
        return tool in allowed

    return _approve
