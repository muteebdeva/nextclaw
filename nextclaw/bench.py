"""Reproducible benchmark suite (MVP).

Measures, on the local machine:
- end-to-end task success rate (file_report)
- recovery after an interrupted execution
- permission/security denials
- memory round-trip

All results are produced by actually running the code; nothing is simulated
except the injected crash used to test recovery.
"""
from __future__ import annotations

import os
import tempfile
import time

from . import agent, tools
from .config import Config, ensure_data_dir
from .memory import MemoryStore


def _temp_cfg():
    ws = tempfile.mkdtemp(prefix="nextclaw-bench-ws-")
    data = tempfile.mkdtemp(prefix="nextclaw-bench-data-")
    cfg = Config(data_dir=data, workspace_root=ws)
    ensure_data_dir(cfg)
    return cfg, ws, data


def _write(ws: str, rel: str, content: str) -> None:
    real = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "w", encoding="utf-8") as f:
        f.write(content)


def bench_file_report():
    cfg, ws, _ = _temp_cfg()
    _write(ws, "fixture/a.py", "x = 1\n" * 10)
    _write(ws, "fixture/b.py", "y = 2\n" * 5)
    started = time.time()
    task_id = agent.create_task(
        "Benchmark: count python files and write verified report",
        "file_report",
        {"root": "fixture", "pattern": "**/*.py", "output": "reports/bench.md"},
        cfg,
    )
    task = agent.run_task(task_id, cfg)
    duration = round(time.time() - started, 3)
    ok = task["status"] == "completed" and bool((task["result"] or {}).get("verified"))
    detail = task.get("result") or {"error": task.get("error")}
    # correctness: we know the fixture (10 + 5 lines)
    correct = detail.get("total_files") == 2 and detail.get("total_lines") == 15
    return {
        "name": "file_report_success",
        "passed": bool(ok and correct),
        "duration_s": duration,
        "detail": detail,
    }


def bench_crash_recovery():
    cfg, ws, _ = _temp_cfg()
    _write(ws, "fixture/a.txt", "hello\n" * 3)
    task_id = agent.create_task(
        "Benchmark: recover after interruption",
        "file_report",
        {"root": "fixture", "pattern": "**/*.txt", "output": "reports/recover.md"},
        cfg,
    )
    started = time.time()
    try:
        agent.run_task(task_id, cfg, fail_after_step=0)
        crashed = False
    except RuntimeError:
        crashed = True
    task = agent.run_task(task_id, cfg)  # resume
    duration = round(time.time() - started, 3)
    ok = crashed and task["status"] == "completed" and bool((task.get("result") or {}).get("verified"))
    return {"name": "crash_recovery", "passed": ok, "duration_s": duration,
            "detail": task.get("result") or {"error": task.get("error")}}


def bench_permissions():
    cfg, ws, _ = _temp_cfg()
    checks = []

    def expect(name, fn, exc):
        try:
            fn()
        except exc:
            checks.append({"name": name, "passed": True})
        except Exception as e:  # noqa: BLE001
            checks.append({"name": name, "passed": False, "detail": f"wrong exception: {e!r}"})
        else:
            checks.append({"name": name, "passed": False, "detail": "no exception raised"})

    expect(
        "path_traversal_denied",
        lambda: tools.execute("fs.write", {"path": "../evil.txt", "content": "x"}, cfg),
        tools.PermissionDenied,
    )
    expect(
        "absolute_escape_denied",
        lambda: tools.execute("fs.read", {"path": "/etc/passwd"}, cfg),
        tools.PermissionDenied,
    )
    expect(
        "destructive_command_denied",
        lambda: tools.execute("exec.run", {"command": "rm -rf /"}, cfg, approvals=("exec.run",)),
        tools.PermissionDenied,
    )
    expect(
        "high_risk_needs_approval",
        lambda: tools.execute("exec.run", {"command": "echo hi"}, cfg),
        tools.ApprovalRequired,
    )
    passed = all(c["passed"] for c in checks)
    return {"name": "permission_denials", "passed": passed, "duration_s": 0.0, "detail": checks}


def bench_memory():
    cfg, ws, _ = _temp_cfg()
    started = time.time()
    mem = MemoryStore(cfg.data_dir)
    eid = mem.remember("bench fact: nextclaw mvp", tags=["bench"], source="bench")
    found = mem.recall("nextclaw mvp", limit=5)
    ok_recall = any(e.id == eid for e in found)
    ok_forget = mem.forget(eid) and mem.get(eid) is None
    duration = round(time.time() - started, 3)
    return {"name": "memory_roundtrip", "passed": bool(ok_recall and ok_forget),
            "duration_s": duration, "detail": {"recall": ok_recall, "forget": ok_forget}}


def run_benchmarks(cfg: Config | None = None):
    benches = [bench_file_report, bench_crash_recovery, bench_permissions, bench_memory]
    results = []
    for b in benches:
        try:
            results.append(b())
        except Exception as exc:  # noqa: BLE001 - a bench must never kill the suite
            results.append({"name": b.__name__, "passed": False, "duration_s": 0.0,
                            "detail": f"bench crashed: {exc!r}"})
    passed = sum(1 for r in results if r["passed"])
    return {
        "results": results,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "success_rate": round(passed / len(results), 3) if results else 0.0,
        },
    }
