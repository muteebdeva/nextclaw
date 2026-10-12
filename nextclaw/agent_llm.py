"""LLM-driven agent loop: real model reasoning + permission-checked tools.

The model plans in its own words, selects tools, inspects real outputs, and
continues until done, blocked, or bounded out. Every turn and every tool call
is checkpointed to SQLite, so an interrupted run resumes from the persisted
conversation instead of starting over.

Safety invariants (unchanged from the MVP core):
- Tool calls go through the same permission policy (allowlist, workspace
  confinement, command denylist, approvals).
- Retries and turns are bounded; the loop cannot spin forever.
- A task that already completed/failed is never re-executed.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

from . import llm as llm_mod
from . import tools
from .config import Config, ensure_data_dir
from .memory import MemoryStore
from .tasks import TaskStore

SYSTEM_PROMPT = """You are NextClaw, a careful AI agent that accomplishes user goals with tools.

Rules:
- Work step by step. Call one or more tools per turn when it helps.
- Only call the tools listed for you. Never invent tool names or arguments.
- All file paths are relative to the task workspace root. Stay inside it.
- Prefer to verify your own work: after writing a file, read it back or run the verifier tool.
- If a tool call fails, read the error, adjust once or twice, then explain the blocker.
- IMPORTANT: if a verifier tool (e.g. verify.file_report) returns ok:true for the task, the goal is ACHIEVED. Stop calling tools immediately and reply with a short final summary.
- When the goal is fully achieved, reply with a short final summary and NO tool calls.
- Keep arguments small and valid. Do not dump huge blobs into arguments.
- Do not repeat a tool call that already succeeded with the result you need.
"""


def create_agent_task(
    goal: str,
    cfg: Config,
    verify: Dict[str, Any] | None = None,
) -> str:
    """Create a task driven by live model reasoning (task_type='agent')."""
    ensure_data_dir(cfg)
    store = TaskStore(cfg.data_dir)
    params: Dict[str, Any] = {"goal": goal}
    if verify:
        params["verify"] = verify
    task_id = store.create_task(goal, "agent", params)
    store.add_turn(task_id, "user", {"text": goal})
    return task_id


def _history_for_provider(turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for t in turns:
        role = t["role"]
        if role == "user":
            out.append({"role": "user", "text": t.get("text", "")})
        elif role == "assistant":
            out.append(
                {
                    "role": "assistant",
                    "text": t.get("text", ""),
                    "tool_calls": [
                    {
                        "name": c["name"],
                        "args": c.get("args", {}),
                        "thought_signature": c.get("thought_signature", ""),
                    }
                    for c in t.get("tool_calls", [])
                ],
                }
            )
        elif role == "tool":
            out.append({"role": "tool", "name": t.get("name", ""), "output": t.get("output")})
    return out


def run_llm_task(
    task_id: str,
    cfg: Config,
    approvals: Tuple[str, ...] = (),
    provider: llm_mod.LLMProvider | None = None,
    fail_after_turn: int | None = None,
) -> Dict[str, Any]:
    """Execute (or resume) an LLM-driven task. Returns the final task record."""
    ensure_data_dir(cfg)
    store = TaskStore(cfg.data_dir)
    task = store.get_task(task_id)
    if task is None:
        raise ValueError(f"unknown task '{task_id}'")
    if task["task_type"] != "agent":
        raise ValueError(f"task '{task_id}' is not an LLM agent task")
    if task["status"] in ("completed", "failed"):
        return task  # never re-execute a finished task

    store.set_task_status(task_id, "running")
    task = store.get_task(task_id)
    params = task["params"]

    if provider is None:
        try:
            provider = llm_mod.llm_provider(cfg)
        except llm_mod.ProviderError as exc:
            # Fail the task with a clear message instead of a traceback;
            # e.g. missing NEXTCLAW_GEMINI_API_KEY or offline mode.
            store.set_task_status(task_id, "failed", error=str(exc))
            return store.get_task(task_id)
    budget = llm_mod.RequestBudget(cfg.data_dir)
    declarations = llm_mod.build_declarations(cfg.allowed_tools)
    deadline = time.time() + cfg.task_timeout_s
    turn_index = store.count_turns(task_id)
    max_task_requests = int(getattr(cfg, "llm_max_requests_per_task", 40))

    for _ in range(cfg.max_turns):
        if time.time() > deadline:
            return _finish(task_id, store, cfg, "failed", error="task deadline exceeded")
        if store.count_turns(task_id, role="assistant") >= max_task_requests:
            return _finish(task_id, store, cfg, "failed",
                           error=f"task API budget exceeded ({max_task_requests} model calls)")

        turns = store.get_turns(task_id)
        history = _history_for_provider(turns)
        try:
            budget.check(cfg)  # fail fast: cooldown / daily cap, no wasted call
            resp = _chat_with_retry(provider, SYSTEM_PROMPT, history, declarations, cfg)
            budget.record()
        except llm_mod.QuotaExhausted as exc:
            budget.record(quota_exhausted=True)
            return _finish(task_id, store, cfg, "failed", error=f"model error: {exc}")
        except llm_mod.ProviderError as exc:
            return _finish(task_id, store, cfg, "failed", error=f"model error: {exc}")

        store.add_turn(
            task_id,
            "assistant",
            {
                "text": resp.text,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "args": c.args,
                     "thought_signature": c.thought_signature}
                    for c in resp.tool_calls
                ],
                "latency_ms": resp.latency_ms,
            },
        )
        turn_index += 1

        if not resp.tool_calls:
            return _verify_and_finish(task_id, store, cfg, params, resp.text)

        for tc in resp.tool_calls:
            outcome, output = _execute_tool_call(task_id, store, cfg, tc, approvals, turn_index)
            if outcome == "awaiting_approval":
                return store.get_task(task_id)
            # Explicit success criteria: if the task's verifier tool reports
            # ok:true with matching args, the goal is achieved - stop now
            # instead of letting the model second-guess a verified result.
            if _verify_spec_satisfied(params, tc, output):
                return _finish(
                    task_id, store, cfg, "completed",
                    result={
                        "final": f"Verified by {tc.name}: ok:true",
                        "verified": True,
                        "verify_output": output,
                    },
                )

        if fail_after_turn is not None and turn_index >= fail_after_turn:
            # Turns + tool results are already checkpointed above; resume is safe.
            raise RuntimeError(f"simulated crash after turn {turn_index}")

    return _finish(task_id, store, cfg, "failed", error=f"did not finish within {cfg.max_turns} turns")


def _chat_with_retry(provider, system, history, declarations, cfg, retries: int = 3):
    delay = 5
    for attempt in range(retries + 1):
        try:
            return provider.chat(system, history, declarations, timeout_s=cfg.llm_timeout_s)
        except llm_mod.QuotaExhausted:
            raise  # never retry quota exhaustion; the budget layer handles cooldown
        except llm_mod.RateLimited as exc:
            if attempt >= retries:
                raise
            # Respect the provider's Retry-After when given, bounded.
            time.sleep(exc.retry_after_s if exc.retry_after_s > 0 else delay)
            delay = min(delay * 2, 60)
    raise llm_mod.ProviderError("unreachable")


def _execute_tool_call(task_id, store, cfg, tc: llm_mod.ToolCall, approvals, turn_index):
    """Permission-check, execute, checkpoint.

    Returns (status, output): status is 'ok' or 'awaiting_approval'.
    """
    step_name = f"turn{turn_index}:{tc.name}"
    try:
        tools.check_permission(tc.name, tc.args, cfg, approvals)
    except tools.ApprovalRequired as exc:
        store.add_turn(task_id, "tool", {"name": tc.name, "output": {"error": str(exc)}})
        store.set_task_status(task_id, "awaiting_approval", error=str(exc))
        return "awaiting_approval", {"error": str(exc)}
    except tools.ToolError as exc:
        # Denied by policy: tell the model so it can adjust, don't crash.
        denied = {"error": f"denied: {exc}"}
        store.add_turn(task_id, "tool", {"name": tc.name, "output": denied})
        return "ok", denied

    res = tools.execute(tc.name, tc.args, cfg, approvals)
    output = res.output if res.ok else {"error": res.error}
    # Checkpoint the tool call as a step (audit) and as a turn (resume).
    store.add_steps(
        task_id,
        [{"name": step_name, "tool": tc.name, "args": tc.args, "verify": {"kind": "ok"}}],
    )
    steps = store.get_steps(task_id)
    step = next(s for s in steps if s["name"] == step_name)
    store.update_step(
        step["id"],
        status="done" if res.ok else "failed",
        attempts=1,
        result={"ok": res.ok, "output": output, "error": res.error, "duration_ms": res.duration_ms},
        error="" if res.ok else res.error,
    )
    store.add_turn(task_id, "tool", {"name": tc.name, "output": output})
    return "ok", output


def _verify_spec_satisfied(params: Dict[str, Any], tc: llm_mod.ToolCall, output: Any) -> bool:
    """True when the task's explicit verifier tool just reported ok:true."""
    verify = params.get("verify")
    if not verify or tc.name != verify.get("tool"):
        return False
    want = verify.get("args", {}) or {}
    if not all(k in tc.args and tc.args[k] == v for k, v in want.items()):
        return False
    return isinstance(output, dict) and output.get("ok") is True


def _verify_and_finish(task_id, store, cfg, params, final_text) -> Dict[str, Any]:
    verify = params.get("verify")
    evidence: Dict[str, Any] = {"final": final_text[:2000], "verified": False}
    if verify:
        try:
            res = tools.execute(verify["tool"], verify.get("args", {}), cfg)
        except tools.ToolError as exc:
            return _finish(task_id, store, cfg, "failed",
                           error=f"verification blocked: {exc}", result=evidence)
        out = res.output if res.ok else {}
        ok = bool(res.ok and isinstance(out, dict) and out.get("ok") is True)
        evidence.update({"verified": ok, "verify_output": out if isinstance(out, dict) else str(out)[:1000]})
        if not ok:
            detail = out.get("detail") if isinstance(out, dict) else res.error
            return _finish(task_id, store, cfg, "failed",
                           error=f"model finished but verification failed: {detail}", result=evidence)
    else:
        evidence["verified"] = None  # no verifier configured; model-declared done
    return _finish(task_id, store, cfg, "completed", result=evidence)


def _finish(task_id, store, cfg, status, result=None, error="") -> Dict[str, Any]:
    store.set_task_status(task_id, status, result=result, error=error)
    task = store.get_task(task_id)
    if status == "completed":
        try:
            mem = MemoryStore(cfg.data_dir)
            mem.remember(
                f"Completed LLM task {task_id}: {task['goal'][:160]}",
                tags=["task", "agent", "completed"],
                source="nextclaw-llm",
            )
        except Exception:
            pass
    return task
