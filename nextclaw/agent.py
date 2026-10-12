"""Agent runtime: plan -> permission-checked tools -> verified result.

Reliability rules (MVP):
- Every step has explicit success criteria (``verify``); a step is only
  "done" when its verifier passes on real tool output.
- State is checkpointed to SQLite after every step; ``run_task`` resets
  orphaned ``running`` steps so interrupted runs resume safely.
- Retries are bounded (``max_step_attempts``); repeated failures fail the
  task with a diagnosis instead of looping forever.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from . import models, tools
from .config import Config, ensure_data_dir
from .memory import MemoryStore
from .tasks import TaskStore


class VerificationFailed(Exception):
    pass


def render_file_report(
    goal: str, params: Dict[str, Any], files: list, measure: Dict[str, Any]
) -> str:
    lines = [
        "# File report",
        "",
        f"Goal: {goal}",
        f"Root: {params['root']}",
        f"Pattern: {params['pattern']}",
        "",
        f"Files: {measure['total_files']}",
        f"Total lines: {measure['total_lines']}",
        f"Total bytes: {measure['total_bytes']}",
        "",
        "## Files",
    ]
    for rel in measure["files"]:
        info = measure["per_file"][rel]
        lines.append(f"- {rel}: {info['lines']} lines, {info['bytes']} bytes")
    lines += ["", "Generated and verified by NextClaw (MVP)."]
    return "\n".join(lines) + "\n"


def _substitute(args: Dict[str, Any], context: Dict[str, Any], goal: str, params: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and v == "{{file_report}}":
            files = context.get("collect_files") or []
            measure = context.get("measure_files") or {}
            out[k] = render_file_report(goal, params, files, measure)
        else:
            out[k] = v
    return out


def _verify_step(step: Dict[str, Any], result: Dict[str, Any], context: Dict[str, Any], cfg: Config) -> None:
    verify = step.get("verify", {}) or {}
    kind = verify.get("kind", "ok")
    output = result.get("output")

    if kind == "ok":
        if not result.get("ok"):
            raise VerificationFailed(f"tool reported failure: {result.get('error')}")
        return
    if kind == "is_list":
        if not (result.get("ok") and isinstance(output, list)):
            raise VerificationFailed("expected a list of files from fs.list")
        return
    if kind == "totals_match":
        ref = context.get(verify.get("ref_step", ""))
        if not (result.get("ok") and isinstance(output, dict)):
            raise VerificationFailed("expected measurements dict from fs.measure")
        if isinstance(ref, list) and output.get("total_files") != len(ref):
            raise VerificationFailed(
                f"measure total_files={output.get('total_files')} != listed {len(ref)}"
            )
        return
    if kind == "file_exists":
        if not result.get("ok"):
            raise VerificationFailed(f"fs.write failed: {result.get('error')}")
        return
    if kind == "verify_ok":
        if not (result.get("ok") and isinstance(output, dict) and output.get("ok") is True):
            detail = output.get("detail") if isinstance(output, dict) else result.get("error")
            raise VerificationFailed(f"report verification failed: {detail}")
        return
    raise VerificationFailed(f"unknown verify kind '{kind}'")


def create_task(goal: str, task_type: str, params: Dict[str, Any], cfg: Config) -> str:
    ensure_data_dir(cfg)
    provider = models.route(task_type, goal, cfg)
    plan = provider.generate_plan(goal, task_type, params)
    store = TaskStore(cfg.data_dir)
    task_id = store.create_task(goal, task_type, params)
    store.add_steps(task_id, plan)
    return task_id


def run_task(
    task_id: str,
    cfg: Config,
    approvals: Tuple[str, ...] = (),
    fail_after_step: int | None = None,
) -> Dict[str, Any]:
    """Execute (or resume) a task. Returns the final task record."""
    ensure_data_dir(cfg)
    store = TaskStore(cfg.data_dir)
    task = store.get_task(task_id)
    if task is None:
        raise ValueError(f"unknown task '{task_id}'")
    if task["status"] in ("completed", "failed"):
        return task

    # Crash recovery: orphaned running steps become pending again.
    store.reset_running_steps(task_id)
    store.set_task_status(task_id, "running")
    task = store.get_task(task_id)
    params = task["params"]
    goal = task["goal"]

    steps = store.get_steps(task_id)
    context: Dict[str, Any] = {}
    for s in steps:
        if s["status"] == "done" and s["result"] and s["result"].get("ok"):
            context[s["name"]] = s["result"]["output"]

    for step in steps:
        if step["status"] == "done":
            continue
        attempts = step["attempts"] or 0
        last_error = step.get("error") or ""
        while attempts < cfg.max_step_attempts:
            attempts += 1
            store.update_step(step["id"], status="running", attempts=attempts)
            try:
                args = _substitute(step["args"], context, goal, params)
                res = tools.execute(step["tool"], args, cfg, approvals)
                res_dict = {
                    "ok": res.ok,
                    "output": res.output,
                    "error": res.error,
                    "duration_ms": res.duration_ms,
                }
                if not res.ok:
                    raise VerificationFailed(f"tool error: {res.error}")
                _verify_step(step, res_dict, context, cfg)
                store.update_step(step["id"], status="done", result=res_dict, error="")
                context[step["name"]] = res.output
                last_error = ""
                break
            except tools.ApprovalRequired as exc:
                store.update_step(step["id"], status="awaiting_approval", error=str(exc))
                store.set_task_status(task_id, "awaiting_approval", error=str(exc))
                return store.get_task(task_id)
            except Exception as exc:  # noqa: BLE001 - bounded retry, then fail
                last_error = str(exc)
                if attempts >= cfg.max_step_attempts:
                    store.update_step(step["id"], status="failed", error=last_error)
                    store.set_task_status(
                        task_id,
                        "failed",
                        error=f"step '{step['name']}' failed after {attempts} attempts: {last_error}",
                    )
                    return store.get_task(task_id)
                # otherwise retry (bounded)
        else:
            store.set_task_status(task_id, "failed", error=f"step '{step['name']}' exhausted retries")
            return store.get_task(task_id)

        # Checkpoint is already durable (step marked done above). Optional
        # crash injection for recovery tests:
        if fail_after_step is not None and step["ord"] == fail_after_step:
            raise RuntimeError(f"simulated crash after step {step['ord']} ({step['name']})")

    result = _build_result(task_id, cfg, context, params)
    store.set_task_status(task_id, "completed", result=result)
    _remember_completion(cfg, task, result)
    return store.get_task(task_id)


def _build_result(
    task_id: str, cfg: Config, context: Dict[str, Any], params: Dict[str, Any]
) -> Dict[str, Any]:
    if "verify_report" in context:
        report_path = params.get("output", "")
        h = tools.execute("fs.hash", {"path": report_path}, cfg)
        measure = context.get("measure_files", {})
        return {
            "task_id": task_id,
            "report_path": report_path,
            "sha256": h.output.get("digest") if h.ok else "",
            "total_files": measure.get("total_files"),
            "total_lines": measure.get("total_lines"),
            "total_bytes": measure.get("total_bytes"),
            "verified": True,
        }
    return {"task_id": task_id, "verified": False}


def _remember_completion(cfg: Config, task: Dict[str, Any], result: Dict[str, Any]) -> None:
    try:
        mem = MemoryStore(cfg.data_dir)
        if result.get("verified"):
            text = (
                f"Completed task {task['id']} ({task['task_type']}): {task['goal']} -> "
                f"{result.get('report_path')} "
                f"({result.get('total_files')} files, {result.get('total_lines')} lines, "
                f"sha256 {str(result.get('sha256'))[:12]}…). Verified by recomputation."
            )
        else:
            text = f"Completed task {task['id']} ({task['task_type']}): {task['goal']}"
        mem.remember(text, tags=["task", task["task_type"], "completed"], source="nextclaw-agent")
    except Exception:
        # Memory must never break task completion in the MVP.
        pass
