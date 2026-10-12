"""Persistent scheduler for autonomous development cycles.

Design:
- Jobs and runs live in SQLite (``scheduler.db``), so state survives restarts.
- Overlap prevention: a job is claimed with an atomic
  ``UPDATE ... WHERE status='idle'``; only one claimant wins, even across
  processes.
- Restart recovery: ``recover()`` marks stale ``running`` runs as
  ``interrupted`` and resets stuck jobs.
- Missed schedules: a job overdue by less than an hour gets one catch-up run;
  longer outages just reschedule (hourly fallback, no stampede).
- Timeouts: each run executes in a subprocess with a hard timeout.
- The dev-cycle job never calls the LLM (quota protection); it runs tests and
  records state.

"Machine shutdown" vs "process restart": a process restart leaves the DB and
pidfile behind, so ``recover()`` can distinguish stale runs. A full machine
shutdown is the same as a crash from our perspective - on next start,
``recover()`` runs before any tick, so interrupted work is marked, never
silently dropped or duplicated.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional

DEV_CYCLE_JOB = "dev-cycle"
DEFAULT_INTERVAL_S = 40 * 60  # 40 minutes
CATCHUP_MAX_LAG_S = 3600  # hourly fallback: don't catch up after longer outages


class SchedulerStore:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "scheduler.db")
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    interval_s INTEGER NOT NULL,
                    handler TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'idle',
                    last_run_at REAL,
                    next_run_at REAL NOT NULL,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    pid INTEGER,
                    started_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    finished_at REAL,
                    summary_json TEXT,
                    error TEXT DEFAULT '',
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id, id)")

    # -- jobs ----------------------------------------------------------
    def ensure_job(self, job_id: str, name: str, interval_s: int, handler: str) -> None:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE jobs SET name=?, interval_s=?, handler=?, updated_at=? WHERE id=?",
                    (name, interval_s, handler, now, job_id),
                )
            else:
                conn.execute(
                    "INSERT INTO jobs (id, name, interval_s, handler, enabled, status,"
                    " last_run_at, next_run_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 1, 'idle', NULL, ?, ?, ?)",
                    (job_id, name, interval_s, handler, now + interval_s, now, now),
                )

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")]

    def set_enabled(self, job_id: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET enabled=?, updated_at=? WHERE id = ?",
                (1 if enabled else 0, time.time(), job_id),
            )

    def claim(self, job_id: str) -> bool:
        """Atomically claim an idle job. Returns True iff this caller won."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='running', updated_at=? WHERE id=? AND status='idle'",
                (time.time(), job_id),
            )
            return cur.rowcount == 1

    def release(self, job_id: str, next_run_at: float, failed: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='idle', next_run_at=?,"
                " consecutive_failures = consecutive_failures + ?,"
                " last_run_at=?, updated_at=? WHERE id=?",
                (next_run_at, 1 if failed else 0, time.time(), time.time(), job_id),
            )
            if not failed:
                conn.execute(
                    "UPDATE jobs SET consecutive_failures=0 WHERE id=?", (job_id,)
                )

    def reset_stuck_job(self, job_id: str, next_run_at: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='idle', next_run_at=?, updated_at=? WHERE id=?",
                (next_run_at, time.time(), job_id),
            )

    # -- runs ----------------------------------------------------------
    def start_run(self, job_id: str, trigger: str) -> int:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (job_id, trigger, status, pid, started_at, heartbeat_at)"
                " VALUES (?, ?, 'running', ?, ?, ?)",
                (job_id, trigger, os.getpid(), now, now),
            )
            return int(cur.lastrowid)

    def heartbeat(self, run_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET heartbeat_at=? WHERE id=?", (time.time(), run_id)
            )

    def finish_run(
        self, run_id: int, status: str, summary: Optional[Dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status=?, finished_at=?, heartbeat_at=?,"
                " summary_json=?, error=? WHERE id=?",
                (status, time.time(), time.time(),
                 json.dumps(summary or {}), error, run_id),
            )

    def list_runs(self, job_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["summary"] = json.loads(d["summary_json"]) if d["summary_json"] else {}
                out.append(d)
            return out

    def stale_running_runs(self, stale_after_s: float) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status='running' AND heartbeat_at < ?",
                (time.time() - stale_after_s,),
            ).fetchall()
            return [dict(r) for r in rows]


def _run_cycle_subprocess(cfg, timeout_s: int) -> Dict[str, Any]:
    """Execute one dev cycle in an isolated subprocess with a hard timeout."""
    from .config import ensure_data_dir

    ensure_data_dir(cfg)
    proc = subprocess.Popen(
        [sys.executable, "-m", "nextclaw.cli",
         "--data-dir", cfg.data_dir, "--workspace", cfg.workspace_root,
         "cycle"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        timed_out = True
    tail = (out or "")[-4000:]
    report: Dict[str, Any] = {}
    if not timed_out:
        try:
            # cli cycle prints the report JSON last
            start = tail.rfind("{")
            report = json.loads(tail[start:])
        except Exception:
            report = {}
    return {
        "timed_out": timed_out,
        "returncode": proc.returncode,
        "tests_passed": report.get("tests_passed"),
        "next_task": report.get("next_task"),
        "tail": tail[-1500:],
    }


class Scheduler:
    def __init__(self, cfg, handlers: Optional[Dict[str, Callable]] = None):
        self.cfg = cfg
        self.store = SchedulerStore(cfg.data_dir)
        self.handlers = handlers or {"cycle": _run_cycle_subprocess}
        self.job_timeout_s = int(getattr(cfg, "scheduler_job_timeout_s", 900))
        self.stale_after_s = int(getattr(cfg, "scheduler_stale_after_s", 1800))
        self.tick_s = int(getattr(cfg, "scheduler_tick_s", 30))

    # -- lifecycle ------------------------------------------------------
    def ensure_defaults(self) -> None:
        self.store.ensure_job(DEV_CYCLE_JOB, "Autonomous dev cycle",
                              DEFAULT_INTERVAL_S, "cycle")

    def recover(self) -> List[str]:
        """Mark stale runs interrupted; reset stuck jobs. Returns actions taken."""
        actions = []
        for run in self.store.stale_running_runs(self.stale_after_s):
            self.store.finish_run(run["id"], "interrupted",
                                  error="stale running run found at scheduler start")
            actions.append(f"run {run['id']} ({run['job_id']}) marked interrupted")
        for job in self.store.list_jobs():
            if job["status"] == "running":
                still_running = any(
                    r["status"] == "running"
                    for r in self.store.list_runs(job["id"], limit=3)
                )
                if not still_running:
                    self.store.reset_stuck_job(job["id"], time.time() + 60)
                    actions.append(f"job {job['id']} reset to idle")
        return actions

    def tick(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Run every due job once. Returns per-job run summaries."""
        now = now if now is not None else time.time()
        results = []
        for job in self.store.list_jobs():
            if not job["enabled"]:
                continue
            if job["next_run_at"] > now:
                continue
            lag = now - job["next_run_at"]
            if lag > CATCHUP_MAX_LAG_S:
                # Hourly fallback: outage too long, just reschedule, no stampede.
                self.store.reset_stuck_job(job["id"], now + job["interval_s"])
                results.append({"job_id": job["id"], "trigger": "skipped",
                                "detail": f"missed by {int(lag)}s, rescheduled"})
                continue
            trigger = "catchup" if lag > job["interval_s"] else "schedule"
            results.append(self.run_job(job["id"], trigger, now))
        return results

    def run_job(self, job_id: str, trigger: str, now: Optional[float] = None) -> Dict[str, Any]:
        now = now if now is not None else time.time()
        job = self.store.get_job(job_id)
        if job is None:
            return {"job_id": job_id, "trigger": trigger, "status": "no-such-job"}
        if not self.store.claim(job_id):
            return {"job_id": job_id, "trigger": trigger, "status": "already-running"}
        run_id = self.store.start_run(job_id, trigger)
        handler = self.handlers.get(job["handler"])
        summary: Dict[str, Any] = {}
        try:
            if handler is None:
                raise RuntimeError(f"unknown handler '{job['handler']}'")
            summary = handler(self.cfg, self.job_timeout_s)
            self.store.heartbeat(run_id)
            if summary.get("timed_out"):
                self.store.finish_run(run_id, "timed_out", summary, "job exceeded timeout")
                status = "timed_out"
            elif summary.get("returncode", 0) != 0 or summary.get("tests_passed") is False:
                self.store.finish_run(run_id, "failed", summary, "handler reported failure")
                status = "failed"
            else:
                self.store.finish_run(run_id, "completed", summary)
                status = "completed"
        except Exception as exc:  # noqa: BLE001 - record, don't crash the scheduler
            self.store.finish_run(run_id, "failed", summary, str(exc)[:500])
            status = "failed"
        failed = status != "completed"
        self.store.release(job_id, now + job["interval_s"], failed)
        return {"job_id": job_id, "trigger": trigger, "status": status,
                "run_id": run_id, "summary": summary}

    # -- daemon ----------------------------------------------------------
    def _pidfile(self) -> str:
        return os.path.join(self.cfg.data_dir, "scheduler.pid")

    def daemon_alive(self) -> Optional[int]:
        try:
            with open(self._pidfile()) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return None
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            return None

    def start_daemon(self) -> int:
        existing = self.daemon_alive()
        if existing:
            raise RuntimeError(f"scheduler already running (pid {existing})")
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        log = open(os.path.join(self.cfg.data_dir, "scheduler.log"), "a")
        proc = subprocess.Popen(
            [sys.executable, "-m", "nextclaw.cli",
             "--data-dir", self.cfg.data_dir, "--workspace", self.cfg.workspace_root,
             "scheduler", "daemon"],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        time.sleep(1.0)
        if proc.poll() is not None:
            raise RuntimeError("scheduler daemon exited immediately; see scheduler.log")
        # daemon writes its own pidfile on boot
        for _ in range(20):
            pid = self.daemon_alive()
            if pid:
                return pid
            time.sleep(0.5)
        raise RuntimeError("scheduler daemon did not write pidfile")

    def stop_daemon(self) -> bool:
        pid = self.daemon_alive()
        if not pid:
            try:
                os.remove(self._pidfile())
            except OSError:
                pass
            return False
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.5)
        try:
            os.remove(self._pidfile())
        except OSError:
            pass
        return True

    def daemon_loop(self) -> None:
        """Foreground loop for the daemon process. Exits cleanly on SIGTERM."""
        stop = False

        def _term(signum, frame):
            nonlocal stop
            stop = True

        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
        with open(self._pidfile(), "w") as f:
            f.write(str(os.getpid()))
        self.ensure_defaults()
        self.recover()
        while not stop:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - scheduler must survive job errors
                print(f"[scheduler] tick error: {exc}", flush=True)
            for _ in range(self.tick_s * 2):
                if stop:
                    break
                time.sleep(0.5)
        try:
            os.remove(self._pidfile())
        except OSError:
            pass
