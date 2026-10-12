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
    from .approvals import preapproved_approver
    params = _parse_params(args.param)
    task_id = agent.create_task(args.goal, args.type, params, cfg)
    print(f"task: {task_id}")
    approver = preapproved_approver(args.approve) if args.approve else None
    try:
        task = agent.run_task(task_id, cfg, approvals=tuple(args.approve or ()), approver=approver)
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
    from .approvals import preapproved_approver

    ensure_data_dir(cfg)
    approver = preapproved_approver(args.approve) if args.approve else None
    task = _TS(cfg.data_dir).get_task(args.task_id)
    if task and task["task_type"] == "agent":
        runner = lambda: agent_llm.run_llm_task(args.task_id, cfg, approvals=tuple(args.approve or ()), approver=approver)
    else:
        runner = lambda: agent.run_task(args.task_id, cfg, approvals=tuple(args.approve or ()), approver=approver)
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


def cmd_providers(args, cfg: Config) -> int:
    from .providers import ProviderStore, build_provider, seed_from_legacy_config
    from .config import ensure_data_dir
    ensure_data_dir(cfg)
    store = ProviderStore(cfg.data_dir)
    seed_from_legacy_config(store, cfg)
    action = args.provider_action

    def _find():
        want = args.id or args.name or ""
        for c in store.list():
            if c["id"] == want or c["name"] == want:
                return c
        return None

    if action == "presets":
        from .providers import PRESETS
        for p in PRESETS:
            print(f"{p['id']:15} {p['display']:28} {p['adapter']:14} {p.get('default_model', '')}")
        return 0
    if action == "list":
        default_id = store.get_setting("default_id")
        fallbacks = store.get_fallbacks()
        for c in store.list():
            flags = []
            if c["id"] == default_id:
                flags.append("default")
            if c["id"] in fallbacks:
                flags.append(f"fallback#{fallbacks.index(c['id']) + 1}")
            if not c["enabled"]:
                flags.append("disabled")
            print(f"{c['id']}  {c['name']:20} {c['adapter']:14} "
                  f"{c['model'] or '-':30} {c['api_key_masked'] or '-':16} "
                  f"{' '.join(flags)}")
        return 0
    if action == "set-default":
        row = _find()
        if not row:
            print("error: provide --id or --name of an existing provider")
            return 1
        try:
            store.set_default(row["id"])
        except Exception as exc:
            print(f"error: {exc}")
            return 1
        print(f"default -> {row['name']}")
        return 0
    if action == "test":
        row = _find()
        if not row:
            print("error: provide --id or --name of an existing provider")
            return 1
        full = store.get(row["id"], include_secret=True)
        try:
            provider = build_provider(full)
        except Exception as exc:
            print(f"NOT CONFIGURED: {exc}")
            return 1
        result = provider.test_connection(timeout_s=15)
        store.record_test(row["id"], bool(result.get("ok")), result.get("error", ""))
        if result.get("ok"):
            print(f"OK: {row['name']}")
            models = result.get("models") or result.get("models_sample") or []
            if models:
                print("models: " + ", ".join(models[:10]))
            if result.get("note"):
                print(result["note"])
            return 0
        print(f"FAILED: {result.get('error')}")
        return 1
    raise SystemExit(f"unknown provider action '{action}'")


def cmd_serve(args, cfg: Config) -> int:
    from .server import serve
    return serve(cfg, host=args.host, port=args.port)


def cmd_scheduler(args, cfg: Config) -> int:
    from .scheduler import Scheduler, DEV_CYCLE_JOB

    ensure_data_dir(cfg)
    sch = Scheduler(cfg)
    action = args.scheduler_action

    if action == "ensure":
        sch.ensure_defaults()
        print(f"ensured job '{DEV_CYCLE_JOB}' every 40 minutes")
        return 0
    if action == "daemon":
        sch.daemon_loop()
        return 0
    if action == "start":
        sch.ensure_defaults()
        pid = sch.start_daemon()
        print(f"scheduler started (pid {pid})")
        return 0
    if action == "stop":
        stopped = sch.stop_daemon()
        print("scheduler stopped" if stopped else "scheduler was not running")
        return 0
    if action == "status":
        pid = sch.daemon_alive()
        print(f"daemon: {'running (pid %d)' % pid if pid else 'not running'}")
        for job in sch.store.list_jobs():
            runs = sch.store.list_runs(job["id"], limit=3)
            last = runs[0] if runs else None
            print(f"job {job['id']}: enabled={bool(job['enabled'])} status={job['status']} "
                  f"interval={job['interval_s']}s next_run_in={int(job['next_run_at'] - __import__('time').time())}s "
                  f"failures={job['consecutive_failures']}")
            if last:
                print(f"  last run #{last['id']}: {last['status']} trigger={last['trigger']} "
                      f"error={last['error'][:120]}")
        return 0
    if action == "run-now":
        sch.ensure_defaults()
        job_id = args.job or DEV_CYCLE_JOB
        result = sch.run_job(job_id, "manual")
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") == "completed" else 1
    if action == "recover":
        actions = sch.recover()
        print("\n".join(actions) if actions else "nothing to recover")
        return 0
    raise SystemExit(f"unknown scheduler action {action!r}")


def cmd_run_agent(args, cfg: Config) -> int:
    from . import agent_llm
    from .approvals import preapproved_approver

    verify = None
    if args.verify_tool:
        verify = {"tool": args.verify_tool, "args": _parse_params(args.verify_arg)}
    task_id = agent_llm.create_agent_task(args.goal, cfg, verify=verify)
    print(f"task: {task_id} (llm provider: {cfg.llm_provider}/{cfg.llm_model})")
    approver = preapproved_approver(args.approve) if args.approve else None
    try:
        task = agent_llm.run_llm_task(task_id, cfg, approvals=tuple(args.approve or ()), approver=approver)
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


def cmd_cancel(args, cfg: Config) -> int:
    from .approvals import ApprovalStore
    ensure_data_dir(cfg)
    store = TaskStore(cfg.data_dir)
    task = store.get_task(args.task_id)
    if not task:
        print(f"unknown task {args.task_id}")
        return 1
    if task["status"] in ("completed", "failed", "cancelled"):
        print(f"task {args.task_id} already {task['status']}")
        return 0
    store.request_cancel(args.task_id)
    # Deny any pending approvals so a blocked runner wakes promptly.
    ap = ApprovalStore(cfg.data_dir)
    for rec in ap.pending_for_task(args.task_id):
        ap.decide(rec["id"], "rejected", decided_by="system-cancel")
    ap.audit("user", "task.cancel", args.task_id, {})
    print(f"cancel requested for task {args.task_id}")
    return 0


def cmd_channels(args, cfg: Config) -> int:
    if args.channel_action == "telegram":
        from .channels import run_telegram
        return run_telegram(cfg)
    raise SystemExit(f"unknown channel {args.channel_action!r}")


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

    p = sub.add_parser("cancel", help="Request cancellation of a running task")
    p.add_argument("--task-id", required=True)

    p = sub.add_parser("serve", help="Start the NextClaw web dashboard + API server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    p = sub.add_parser("channels", help="Messaging channel integrations")
    p.add_argument("channel_action", choices=["telegram"])

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

    p = sub.add_parser("scheduler", help="Persistent 40-minute dev-cycle scheduler")
    p.add_argument("scheduler_action",
                   choices=["ensure", "start", "stop", "status", "run-now", "recover", "daemon"])
    p.add_argument("--job", default=None)

    p = sub.add_parser("providers", help="Manage AI provider configurations")
    p.add_argument("provider_action",
                   choices=["list", "test", "set-default", "presets"])
    p.add_argument("--id", default=None, help="Provider config id")
    p.add_argument("--name", default=None, help="Provider config name")

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
        "cancel": lambda: cmd_cancel(args, cfg),
        "serve": lambda: cmd_serve(args, cfg),
        "channels": lambda: cmd_channels(args, cfg),
        "remember": lambda: cmd_remember(args, cfg),
        "recall": lambda: cmd_recall(args, cfg),
        "forget": lambda: cmd_forget(args, cfg),
        "bench": lambda: cmd_bench(args, cfg),
        "cycle": lambda: cmd_cycle(args, cfg),
        "scheduler": lambda: cmd_scheduler(args, cfg),
        "providers": lambda: cmd_providers(args, cfg),
    }
    return handlers[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
