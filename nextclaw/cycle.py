"""Autonomous development cycle (MVP).

Each cycle:
1. Reads persistent project state (data/cycle.json).
2. Runs the test suite (evidence, not vibes).
3. Records results and selects the next roadmap item.

The cycle never invents results: test outcomes come from actually running
``python -m unittest``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROADMAP = [
    "Harden exec.run policy with more denylist cases + tests",
    "Add scheduler (cron-like) with persistent due tasks",
    "Add event triggers (file watcher) with persistent cursor",
    "Add multi-agent delegation (parent/child tasks)",
    "Add web dashboard (read-only task/memory views)",
    "Add Telegram adapter behind explicit pairing + approvals",
    "Add browser automation tool behind allowlisted domains",
    "Add cost/token accounting per task and provider",
]


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cycle(cfg) -> dict:
    from .config import ensure_data_dir

    data_dir = ensure_data_dir(cfg)
    state_path = os.path.join(data_dir, "cycle.json")
    state = {"runs": 0, "last": None, "next_task": ROADMAP[0]}
    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            state.update(json.load(f))

    root = _project_root()
    started = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    duration = round(time.time() - started, 2)
    passed = proc.returncode == 0

    # Select next task: first roadmap item not yet marked done.
    done = set(state.get("done", []))
    remaining = [t for t in ROADMAP if t not in done]
    next_task = remaining[0] if remaining else "roadmap complete - propose new items"

    report = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tests_passed": passed,
        "returncode": proc.returncode,
        "duration_s": duration,
        "tail": (proc.stderr or proc.stdout)[-2000:],
        "next_task": next_task,
    }
    state["runs"] = int(state.get("runs", 0)) + 1
    state["last"] = report
    state["next_task"] = next_task
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    return report
