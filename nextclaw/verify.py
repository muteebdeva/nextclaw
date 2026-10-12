"""Independent verification engine.

Determines whether a requested outcome was actually achieved, using
deterministic checks — never the model's word alone.

Each acceptance criterion declares a verification *method*:

  {"criterion": "all unit tests pass",
   "method": "tests", "command": "python3 -m unittest discover -s tests"}

  {"criterion": "report exists with totals",
   "method": "files",
   "files": [{"path": "report.md", "contains": "Total lines:"}]}

  {"criterion": "CLI prints version",
   "method": "command", "command": "python3 -m nextclaw.cli --version",
   "expect_exit": 0, "expect_output": "nextclaw"}

  {"criterion": "dashboard shows login",
   "method": "browser", "url": "http://127.0.0.1:8765",
   "expect_text": "NextClaw"}

Produces a structured result per criterion:
  criterion / method / status (pass|fail|inconclusive) / evidence /
  errors / uncertainty.

Rules:
- A criterion that cannot be checked is INCONCLUSIVE, never passed.
- Verification runs through the same sandboxed tool policy as everything
  else (workspace-restricted exec, no shell).
- Model-based review may add analysis, but model agreement alone is not proof.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"


@dataclass
class CriterionResult:
    criterion: str
    method: str
    status: str  # pass | fail | inconclusive
    evidence: str = ""
    errors: str = ""
    uncertainty: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "criterion": self.criterion,
            "method": self.method,
            "status": self.status,
            "evidence": self.evidence,
            "errors": self.errors,
            "uncertainty": self.uncertainty,
        }


def _truncate(s: str, n: int = 1500) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "\n…[truncated]"


def _verify_tests(criterion: Dict[str, Any], cfg) -> CriterionResult:
    from . import tools as tools_mod

    command = str(criterion.get("command", "")).strip()
    if not command:
        return CriterionResult(
            criterion.get("criterion", ""), "tests", INCONCLUSIVE,
            uncertainty="no test command specified")
    try:
        res = tools_mod.execute(
            "exec.run",
            {"command": command,
             "cwd": str(criterion.get("cwd", ".")),
             "timeout": int(criterion.get("timeout", 300))},
            cfg, approvals=("exec.run",),
        )
    except tools_mod.ToolError as exc:
        return CriterionResult(
            criterion.get("criterion", ""), "tests", INCONCLUSIVE,
            errors=str(exc),
            uncertainty="could not execute the test command under policy")
    if not res.ok:
        return CriterionResult(
            criterion.get("criterion", ""), "tests", FAIL,
            evidence=_truncate((res.output or {}).get("stdout", "")),
            errors=res.error)
    out = res.output or {}
    passed = out.get("returncode") == 0
    tail = _truncate((out.get("stdout", "") + "\n" + out.get("stderr", ""))[-1200:])
    return CriterionResult(
        criterion.get("criterion", ""), "tests",
        PASS if passed else FAIL,
        evidence=f"exit={out.get('returncode')}\n{tail}",
        errors="" if passed else "test command exited non-zero")


def _verify_files(criterion: Dict[str, Any], cfg) -> CriterionResult:
    from . import tools as tools_mod

    files = criterion.get("files") or []
    if not files:
        return CriterionResult(
            criterion.get("criterion", ""), "files", INCONCLUSIVE,
            uncertainty="no files listed to check")
    problems, evidence = [], []
    for spec in files:
        path = str(spec.get("path", ""))
        try:
            text = tools_mod.execute("fs.read", {"path": path}, cfg).output
        except tools_mod.ToolError as exc:
            problems.append(f"{path}: unreadable ({exc})")
            continue
        if text is None:
            problems.append(f"{path}: missing")
            continue
        want = spec.get("contains")
        if want and want not in str(text):
            problems.append(f"{path}: does not contain {want!r}")
        else:
            evidence.append(f"{path}: present"
                            + (f", contains {want!r}" if want else ""))
    if problems:
        return CriterionResult(
            criterion.get("criterion", ""), "files", FAIL,
            evidence="\n".join(evidence), errors="\n".join(problems))
    return CriterionResult(
        criterion.get("criterion", ""), "files", PASS,
        evidence="\n".join(evidence))


def _verify_command(criterion: Dict[str, Any], cfg) -> CriterionResult:
    from . import tools as tools_mod

    command = str(criterion.get("command", "")).strip()
    if not command:
        return CriterionResult(
            criterion.get("criterion", ""), "command", INCONCLUSIVE,
            uncertainty="no command specified")
    try:
        res = tools_mod.execute(
            "exec.run",
            {"command": command,
             "cwd": str(criterion.get("cwd", ".")),
             "timeout": int(criterion.get("timeout", 120))},
            cfg, approvals=("exec.run",),
        )
    except tools_mod.ToolError as exc:
        return CriterionResult(
            criterion.get("criterion", ""), "command", INCONCLUSIVE,
            errors=str(exc),
            uncertainty="could not execute under policy")
    if not res.ok:
        return CriterionResult(
            criterion.get("criterion", ""), "command", FAIL,
            errors=res.error)
    out = res.output or {}
    expect_exit = criterion.get("expect_exit", 0)
    expect_output = criterion.get("expect_output")
    combined = (out.get("stdout", "") or "") + (out.get("stderr", "") or "")
    problems = []
    if expect_exit is not None and out.get("returncode") != expect_exit:
        problems.append(
            f"exit {out.get('returncode')} != expected {expect_exit}")
    if expect_output and expect_output not in combined:
        problems.append(f"output missing {expect_output!r}")
    if problems:
        return CriterionResult(
            criterion.get("criterion", ""), "command", FAIL,
            evidence=_truncate(combined), errors="; ".join(problems))
    return CriterionResult(
        criterion.get("criterion", ""), "command", PASS,
        evidence=_truncate(combined))


def _verify_browser(criterion: Dict[str, Any], cfg) -> CriterionResult:
    from . import browsing
    from . import tools as tools_mod

    url = str(criterion.get("url", "")).strip()
    if not url:
        return CriterionResult(
            criterion.get("criterion", ""), "browser", INCONCLUSIVE,
            uncertainty="no URL specified")
    try:
        tools_mod.execute("browser.open", {"url": url}, cfg)
        snap = tools_mod.execute("browser.snapshot", {}, cfg).output or {}
    except tools_mod.ToolError as exc:
        return CriterionResult(
            criterion.get("criterion", ""), "browser", INCONCLUSIVE,
            errors=str(exc),
            uncertainty="browser unavailable or navigation blocked")
    except browsing.BrowserError as exc:
        return CriterionResult(
            criterion.get("criterion", ""), "browser", INCONCLUSIVE,
            errors=str(exc), uncertainty="browser automation failed")
    text = snap.get("snapshot", "") if isinstance(snap, dict) else ""
    expect_text = criterion.get("expect_text")
    problems = []
    if expect_text and expect_text not in text:
        problems.append(f"page missing {expect_text!r}")
    if problems:
        return CriterionResult(
            criterion.get("criterion", ""), "browser", FAIL,
            evidence=_truncate(text, 800), errors="; ".join(problems))
    return CriterionResult(
        criterion.get("criterion", ""), "browser", PASS,
        evidence=f"opened {url}; " + _truncate(text, 600))


_VERIFIERS = {
    "tests": _verify_tests,
    "files": _verify_files,
    "command": _verify_command,
    "browser": _verify_browser,
}


def verify_criteria(criteria: List[Dict[str, Any]], cfg) -> List[Dict[str, Any]]:
    """Run every acceptance criterion through its deterministic verifier.

    Returns a list of result dicts (criterion/method/status/evidence/
    errors/uncertainty). Unknown methods -> inconclusive, never pass.
    """
    results: List[CriterionResult] = []
    for c in criteria:
        method = str(c.get("method", "")).strip()
        fn = _VERIFIERS.get(method)
        if fn is None:
            results.append(CriterionResult(
                c.get("criterion", ""), method or "unknown", INCONCLUSIVE,
                uncertainty=f"unknown verification method {method!r}"))
            continue
        try:
            results.append(fn(c, cfg))
        except Exception as exc:  # a verifier must never crash the run
            results.append(CriterionResult(
                c.get("criterion", ""), method, INCONCLUSIVE,
                errors=f"verifier crashed: {exc}",
                uncertainty="verification could not complete"))
    return [r.as_dict() for r in results]


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {PASS: 0, FAIL: 0, INCONCLUSIVE: 0}
    for r in results:
        counts[r.get("status", INCONCLUSIVE)] = counts.get(r.get("status"), 0) + 1
    return {
        "total": len(results),
        "passed": counts[PASS],
        "failed": counts[FAIL],
        "inconclusive": counts[INCONCLUSIVE],
        "verdict": "pass" if counts[FAIL] == 0 and counts[INCONCLUSIVE] == 0 and results else "fail",
    }
