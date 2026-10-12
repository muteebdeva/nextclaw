"""Command-line interface for NextClaw (MVP).

Examples:
    python3 -m nextclaw.cli init
    python3 -m nextclaw.cli run --goal "Count py files" --type file_report \\
        --param root=examples/demo --param pattern='**/*.py' --param output=reports/demo.md
    python3 -m nextclaw.cli status --task-id <id>
    python3 -m nextclaw.cli resume --task-id <id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import agent
from .config import Config, ensure_data_dir, load_config
from .memory import MemoryStore
from .tasks import TaskStore


def _parse_params(items) -> dict:
    params: dict = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--param must be key=value, got {item!r}")
        k, v = item.split("=", 1)
        params[k.strip()] = v.strip()
    return params


def cmd_init(cfg: Config) -> int:
    ensure_data_dir(cfg)
    path = os.path.join(cfg.data_dir, "nextclaw.json")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "data_dir": cfg.data_dir,
                    "workspace_root": cfg.workspace_root,
                    "model_provider": cfg.model_provider,
                    "max_step_attempts": cfg.max_step_attempts,
                },
                f,
                indent=2,
            )
    print(f"initialized: data_dir={cfg.data_dir}")
    print(f"workspace_root={cfg.workspace_root} provider={cfg.model_provider}")
    return 0


def cmd_run(args, cfg: Config) -> int:
    params = _parse_params(args.param)
    task_id = agent.create_task(args.goal, args.type, params, cfg)
    print(f"task: {task_id}")
    try:
        task = agent.run_task(task_id, cfg, approvals=tuple(args.approve or ()))
    except RuntimeError as exc:
        print(f"interrupted: {exc}")
        print(f"resume with: python3 -m nextclaw.cli resume --task-id {task_id}")
        return 2
    print(f"status: {task['status']}")
    if task["status"] == "awaiting_approval":
        print(f"blocked: {task['error']}")
        print("re-run resume with --approve <tool-name> after human approval")
        return 3
    if task["status"] == "failed":
        print(f"error: {task['error']}")
        return 1
    print("result:")
    print(json.dumps(task["result"], indent=2))
    return 0


def cmd_resume(args, cfg: Config) -> int:
    from . import agent_llm
    from .tasks import TaskStore as _TS

    ensure_data_dir(cfg)
    task = _TS(cfg.data_dir).get_task(args.task_id)
    if task and task["task_type"] == "agent":
        runner = lambda: agent_llm.run_llm_task(args.task_id, cfg, approvals=tuple(args.approve or ()))
    else:
        runner = lambda: agent.run_task(args.task_id, cfg, approvals=tuple(args.approve or ()))
    try:
        task = runner()
    except RuntimeError as exc:
        print(f"interrupted: {exc}")
        return 2
    print(f"status: {task['status']}")
    if task["status"] == "failed":
        print(f"error: {task['error']}")
        return 1
    if task["status"] == "awaiting_approval":
        print(f"blocked: {task['error']}")
        return 3
    print(json.dumps(task.get("result"), indent=2))
    return 0


def cmd_status(args, cfg: Config) -> int:
    ensure_data_dir(cfg)
    store = TaskStore(cfg.data_dir)
    task = store.get_task(args.task_id)
    if not task:
        print(f"unknown task {args.task_id}")
        return 1
    print(f"id: {task['id']} status: {task['status']} type: {task['task_type']}")
    print(f"goal: {task['goal']}")
    for s in store.get_steps(args.task_id):
        print(f"  [{s['status']}] {s['ord']}: {s['name']} ({s['tool']}) attempts={s['attempts']}")
        if s["error"]:
            print(f"      error: {s['error'][:200]}")
    if task.get("result"):
        print("result:", json.dumps(task["result"], indent=2))
    return 0


def cmd_remember(args, cfg: Config) -> int:
    ensure_data_dir(cfg)
    mem = MemoryStore(cfg.data_dir)
    entry_id = mem.remember(args.text, tags=args.tag or [], source="cli")
    print(f"remembered id={entry_id}")
    return 0


def cmd_recall(args, cfg: Config) -> int:
    ensure_data_dir(cfg)
    mem = MemoryStore(cfg.data_dir)
    for e in mem.recall(args.query, limit=args.limit):
        print(f"[{e.id}] ({e.source}) tags={','.join(e.tags)}")
        print(f"  {e.text[:300]}")
    return 0


def cmd_forget(args, cfg: Config) -> int:
    ensure_data_dir(cfg)
    mem = MemoryStore(cfg.data_dir)
    print("deleted" if mem.forget(args.id) else "not found")
    return 0


def cmd_bench(args, cfg: Config) -> int:
    from .bench import run_benchmarks

    results = run_benchmarks(cfg)
    path = os.path.join(ensure_data_dir(cfg), "bench_results.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(json.dumps(results["summary"], indent=2))
    print(f"saved: {path}")
    return 0 if results["summary"]["failed"] == 0 else 1


def cmd_cycle(args, cfg: Config) -> int:
    from .cycle import run_cycle

    report = run_cycle(cfg)
    print(json.dumps(report, indent=2))
    return 0


def cmd_run_agent(args, cfg: Config) -> int:
    from . import agent_llm

    verify = None
    if args.verify_tool:
        verify = {"tool": args.verify_tool, "args": _parse_params(args.verify_arg)}
    task_id = agent_llm.create_agent_task(args.goal, cfg, verify=verify)
    print(f"task: {task_id} (llm provider: {cfg.llm_provider}/{cfg.llm_model})")
    try:
        task = agent_llm.run_llm_task(task_id, cfg, approvals=tuple(args.approve or ()))
    except RuntimeError as exc:
        print(f"interrupted: {exc}")
        print(f"resume with: python3 -m nextclaw.cli resume --task-id {task_id}")
        return 2
    print(f"status: {task['status']}")
    if task["status"] == "awaiting_approval":
        print(f"blocked: {task['error']}")
        print("re-run resume with --approve <tool-name> after human approval")
        return 3
    if task["status"] == "failed":
        print(f"error: {task['error']}")
        return 1
    print("result:")
    print(json.dumps(task["result"], indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="nextclaw", description="NextClaw MVP agent platform")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--workspace", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    p = sub.add_parser("run")
    p.add_argument("--goal", required=True)
    p.add_argument("--type", default="file_report")
    p.add_argument("--param", action="append", default=[])
    p.add_argument("--approve", action="append", default=[])

    p = sub.add_parser("run-agent", help="Run a goal with live model reasoning + tools")
    p.add_argument("--goal", required=True)
    p.add_argument("--verify-tool", default=None)
    p.add_argument("--verify-arg", action="append", default=[])
    p.add_argument("--approve", action="append", default=[])
    p.add_argument("--provider", default=None)

    p = sub.add_parser("resume")
    p.add_argument("--task-id", required=True)
    p.add_argument("--approve", action="append", default=[])

    p = sub.add_parser("status")
    p.add_argument("--task-id", required=True)

    p = sub.add_parser("remember")
    p.add_argument("--text", required=True)
    p.add_argument("--tag", action="append", default=[])

    p = sub.add_parser("recall")
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=5)

    p = sub.add_parser("forget")
    p.add_argument("--id", type=int, required=True)

    sub.add_parser("bench")
    sub.add_parser("cycle")

    args = parser.parse_args(argv)
    overrides = {}
    if args.data_dir:
        overrides["data_dir"] = args.data_dir
    if args.workspace:
        overrides["workspace_root"] = args.workspace
    if getattr(args, "provider", None):
        overrides["llm_provider"] = args.provider
    cfg = load_config(overrides=overrides)

    handlers = {
        "init": lambda: cmd_init(cfg),
        "run": lambda: cmd_run(args, cfg),
        "run-agent": lambda: cmd_run_agent(args, cfg),
        "resume": lambda: cmd_resume(args, cfg),
        "status": lambda: cmd_status(args, cfg),
        "remember": lambda: cmd_remember(args, cfg),
        "recall": lambda: cmd_recall(args, cfg),
        "forget": lambda: cmd_forget(args, cfg),
        "bench": lambda: cmd_bench(args, cfg),
        "cycle": lambda: cmd_cycle(args, cfg),
    }
    return handlers[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
