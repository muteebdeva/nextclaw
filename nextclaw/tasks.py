"""Persistent task queue and workflow state machine (SQLite).

Every task has explicit steps. Step state is checkpointed after each tool call,
so a crashed run can resume without repeating completed work.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional


TASK_STATUSES = {"queued", "running", "awaiting_approval", "failed", "completed"}
STEP_STATUSES = {"pending", "running", "awaiting_approval", "failed", "done"}


class TaskStore:
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
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'queued',
                    result_json TEXT,
                    error TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    ord INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    args_json TEXT NOT NULL DEFAULT '{}',
                    verify_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error TEXT DEFAULT '',
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_task ON steps(task_id, ord)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    ord INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_task ON turns(task_id, ord)")

    # -- tasks -----------------------------------------------------------
    def create_task(self, goal: str, task_type: str, params: Dict[str, Any]) -> str:
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks (id, goal, task_type, params_json, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (task_id, goal, task_type, json.dumps(params), now, now),
            )
        return task_id

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return None
            task = dict(row)
            task["params"] = json.loads(task["params_json"])
            task["result"] = json.loads(task["result_json"]) if task["result_json"] else None
            return task

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            out = []
            for r in rows:
                t = dict(r)
                t["params"] = json.loads(t["params_json"])
                out.append(t)
            return out

    def set_task_status(
        self,
        task_id: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        if status not in TASK_STATUSES:
            raise ValueError(f"bad task status: {status}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, result_json = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, json.dumps(result) if result is not None else None, error, time.time(), task_id),
            )

    # -- steps -----------------------------------------------------------
    def add_steps(self, task_id: str, steps: List[Dict[str, Any]]) -> None:
        now = time.time()
        with self._connect() as conn:
            for i, s in enumerate(steps):
                conn.execute(
                    "INSERT INTO steps (task_id, ord, name, tool, args_json, verify_json, status, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        task_id,
                        i,
                        s["name"],
                        s["tool"],
                        json.dumps(s.get("args", {})),
                        json.dumps(s.get("verify", {})),
                        now,
                    ),
                )

    def get_steps(self, task_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM steps WHERE task_id = ? ORDER BY ord", (task_id,)
            ).fetchall()
            out = []
            for r in rows:
                s = dict(r)
                s["args"] = json.loads(s["args_json"])
                s["verify"] = json.loads(s["verify_json"])
                s["result"] = json.loads(s["result_json"]) if s["result_json"] else None
                out.append(s)
            return out

    def update_step(
        self,
        step_id: int,
        status: Optional[str] = None,
        attempts: Optional[int] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        sets = ["updated_at = ?"]
        values: list = [time.time()]
        if status is not None:
            if status not in STEP_STATUSES:
                raise ValueError(f"bad step status: {status}")
            sets.append("status = ?")
            values.append(status)
        if attempts is not None:
            sets.append("attempts = ?")
            values.append(attempts)
        if result is not None:
            sets.append("result_json = ?")
            values.append(json.dumps(result))
        if error is not None:
            sets.append("error = ?")
            values.append(error)
        values.append(step_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE steps SET {', '.join(sets)} WHERE id = ?", values)

    def reset_running_steps(self, task_id: str) -> int:
        """Crash recovery: steps left in 'running' become 'pending' again."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE steps SET status = 'pending', updated_at = ? WHERE task_id = ? AND status = 'running'",
                (time.time(), task_id),
            )
            return cur.rowcount

    # -- turns (LLM conversation persistence) ---------------------------
    def add_turn(self, task_id: str, role: str, content: Dict[str, Any]) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(ord), -1) FROM turns WHERE task_id = ?", (task_id,)
            ).fetchone()
            nxt = int(row[0]) + 1
            cur = conn.execute(
                "INSERT INTO turns (task_id, ord, role, content_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (task_id, nxt, role, json.dumps(content), time.time()),
            )
            return int(cur.lastrowid)

    def get_turns(self, task_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content_json FROM turns WHERE task_id = ? ORDER BY ord",
                (task_id,),
            ).fetchall()
            return [
                {"role": r["role"], **json.loads(r["content_json"])} for r in rows
            ]

    def count_turns(self, task_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE task_id = ?", (task_id,)
            ).fetchone()
            return int(row[0])
