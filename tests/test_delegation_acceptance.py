"""Acceptance demonstration: multi-agent bug-fix workflow.

User request: "Inspect this Python project, identify a bug, fix it,
add a regression test, and verify the result."

Expected execution:
1. Lead creates plan + acceptance criteria.
2. Researcher inspects the code.
3. Coder prepares a patch in an isolated workspace.
4. Tester independently runs the regression tests.
5. Reviewer examines patch + evidence.
6. Verification engine checks files, tests, criteria.
7. Structured final report with each worker's contribution.

Independent subtasks run concurrently; dependent work waits.
A failure-injection test proves retry/replacement without full restart.

Deterministic: scripted mock providers (no model API calls), real tools.
"""
import json
import os
import tempfile
import time
import unittest

from nextclaw import delegation as d
from nextclaw.delegation import Subtask, DelegationBudgets, WORKER_PROFILES
from nextclaw.config import Config, ensure_data_dir
from nextclaw import llm as llm_mod
from nextclaw import verify as verify_mod


def make_cfg(**kw):
    ws = tempfile.mkdtemp(prefix="nc-acc-ws-")
    data = tempfile.mkdtemp(prefix="nc-acc-data-")
    cfg = Config(data_dir=data, workspace_root=ws, **kw)
    ensure_data_dir(cfg)
    return cfg


def write_buggy_project(ws):
    with open(os.path.join(ws, "calc.py"), "w") as f:
        f.write('def add(a, b):\n    return a - b  # BUG: should be a + b\n')
    with open(os.path.join(ws, "test_calc.py"), "w") as f:
        f.write(
            "import unittest\nfrom calc import add\n\n"
            "class TestCalc(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n")


class ScriptedProvider(llm_mod.LLMProvider):
    """Mock provider playing a scripted tool-call sequence per worker.

    script: list of (text, [(tool_name, args), ...]). Verifies the worker's
    profile role is in the system prompt (profile isolation check).
    """
    name = "scripted"

    def __init__(self, script, expected_profile):
        self.script = list(script)
        self.expected_profile = expected_profile
        self.calls = 0

    def chat(self, system, history, declarations, timeout_s=60):
        self.calls += 1
        profile = WORKER_PROFILES[self.expected_profile]
        # The worker must run under its profile role (first 60 chars).
        assert profile.role[:60] in system, \
            f"worker missing profile role for {self.expected_profile}"
        # Tools offered must be subset of the profile's allowlist.
        offered = {decl["name"] for decl in declarations}
        assert offered <= set(profile.allowed_tools), \
            f"worker offered tools outside allowlist: {offered - set(profile.allowed_tools)}"
        if not self.script:
            return llm_mod.LLMResponse(text="{}", tool_calls=[])
        text, calls = self.script.pop(0)
        return llm_mod.LLMResponse(
            text=text,
            tool_calls=[llm_mod.ToolCall(id=f"c{i}", name=n, args=a)
                        for i, (n, a) in enumerate(calls)],
            latency_ms=1)


def researcher_script():
    return [
        ("inspecting", [("fs.read", {"path": "calc.py"})]),
        (json.dumps({
            "findings": [
                {"file": "calc.py", "line": 2,
                 "issue": "add() subtracts instead of adding",
                 "evidence": "return a - b"}],
            "sources": ["calc.py:2"],
            "open_questions": []}), []),
    ]


def coder_script():
    return [
        ("fixing", [("fs.edit", {
            "path": "calc.py",
            "old_string": "return a - b  # BUG: should be a + b",
            "new_string": "return a + b"})]),
        ("adding regression test", [("fs.write", {
            "path": "test_regression.py",
            "content": "import unittest\nfrom calc import add\n\n"
                       "class TestRegression(unittest.TestCase):\n"
                       "    def test_add_regression(self):\n"
                       "        self.assertEqual(add(2, 3), 5)\n"
                       "        self.assertEqual(add(-1, 1), 0)\n"})]),
        ("running tests", [("exec.run", {
            "command": "python3 -m unittest test_calc test_regression -v"})]),
        (json.dumps({
            "summary": "Fixed add() to use +; added regression test",
            "files_changed": ["calc.py", "test_regression.py"],
            "tests_run": [{"command": "python3 -m unittest test_calc test_regression",
                           "exit": 0}],
            "artifacts": [{"name": "patch", "path": "calc.py"},
                          {"name": "regression-test",
                           "path": "test_regression.py"}],
            "notes": "all green"}), []),
    ]


def tester_script():
    return [
        ("running suite", [("exec.run", {
            "command": "python3 -m unittest test_calc test_regression -v"})]),
        (json.dumps({
            "summary": "All 4 tests pass",
            "tests": [{"command": "python3 -m unittest test_calc test_regression",
                       "exit": 0, "output_tail": "OK"}],
            "verdict": "pass"}), []),
    ]


def reviewer_script():
    return [
        ("checking diff", [("git.diff", {"stat_only": "false"})]),
        ("checking test file", [("fs.read", {"path": "test_regression.py"})]),
        (json.dumps({
            "verdict": "approve",
            "criteria": [
                {"criterion": "bug fixed", "status": "pass",
                 "evidence": "calc.py uses return a + b"},
                {"criterion": "regression test exists", "status": "pass",
                 "evidence": "test_regression.py asserts add(2,3)==5"}],
            "required_changes": []}), []),
    ]


class DelegationAcceptanceTests(unittest.TestCase):
    def test_full_bugfix_workflow(self):
        cfg = make_cfg()
        write_buggy_project(cfg.workspace_root)

        # The lead's plan: research first; coder+tester depend on research;
        # reviewer depends on both. Coder and tester are independent of each
        # other after research (tester will re-run everything anyway).
        # For true parallelism demo: researcher and a parallel "audit" run
        # concurrently.
        providers = {
            "research": ScriptedProvider(researcher_script(), "researcher"),
            "audit": ScriptedProvider(
                [("listing", [("fs.list", {"path": ".", "pattern": "*.py"})]),
                 (json.dumps({"findings": [{"file": "test_calc.py",
                                            "issue": "existing tests cover add()",
                                            "evidence": "test_add"}],
                              "sources": ["test_calc.py"],
                              "open_questions": []}), [])],
                "researcher"),
            "coder": ScriptedProvider(coder_script(), "coder"),
            "tester": ScriptedProvider(tester_script(), "tester"),
            "reviewer": ScriptedProvider(reviewer_script(), "reviewer"),
        }

        # Stage 1: research + audit in parallel (with scripted providers).
        stage1 = [
            Subtask(id="research", name="Analyze code", kind="agent",
                    profile="researcher",
                    goal="Inspect calc.py, identify the bug, cite sources."),
            Subtask(id="audit", name="Audit tests", kind="agent",
                    profile="researcher",
                    goal="List Python files and summarize test coverage."),
        ]

        class Router(llm_mod.LLMProvider):
            name = "router"

            def chat(self, system, history, declarations, timeout_s=60):
                # Route by profile role marker in the system prompt.
                for pname, prov in providers.items():
                    if WORKER_PROFILES[prov.expected_profile].role[:40] in system:
                        return prov.chat(system, history, declarations,
                                         timeout_s)
                raise AssertionError("no scripted provider matched")

        router = Router()
        t0 = time.time()
        r1 = d.run_delegation(
            cfg, "Inspect project", stage1,
            budgets=DelegationBudgets(max_concurrent=2),
            provider=router)
        self.assertEqual(r1["status"], "completed", r1.get("error"))
        self.assertEqual(r1["results"]["research"]["status"], "completed")
        self.assertEqual(r1["results"]["audit"]["status"], "completed")

        # Stage 2: coder -> tester -> reviewer with dependencies.
        stage2 = [
            Subtask(id="coder", name="Fix bug", kind="agent", profile="coder",
                    goal="Fix the add() bug in calc.py and add a regression test.",
                    depends_on=[],
                    acceptance_criteria=[
                        {"criterion": "calc.py fixed", "method": "files",
                         "files": [{"path": "calc.py", "contains": "return a + b"}]},
                    ]),
            Subtask(id="tester", name="Test fix", kind="agent", profile="tester",
                    goal="Independently run the test suite and report results.",
                    depends_on=["coder"]),
            Subtask(id="reviewer", name="Review", kind="agent",
                    profile="reviewer",
                    goal="Review the patch and test evidence.",
                    depends_on=["coder", "tester"]),
        ]
        r2 = d.run_delegation(
            cfg, "Fix and verify", stage2,
            budgets=DelegationBudgets(max_concurrent=2),
            approvals=("exec.run",),
            provider=router)
        elapsed = time.time() - t0
        self.assertEqual(r2["status"], "completed", r2.get("error"))

        # 7. Independent verification of the final deliverable.
        # The coder worked in an isolated workspace; its outputs were
        # promoted to artifacts/<worker_id>/. Verify the promoted copies.
        coder_artifacts = r2["artifacts"]
        self.assertTrue(coder_artifacts, "coder should have registered artifacts")
        promoted = {a["name"]: a["path"] for a in coder_artifacts}
        self.assertIn("patch", promoted)
        self.assertIn("regression-test", promoted)
        # Verify content of promoted artifacts directly.
        with open(promoted["patch"]) as f:
            self.assertIn("return a + b", f.read())
        with open(promoted["regression-test"]) as f:
            self.assertIn("test_add_regression", f.read())
        # And run the promoted test files for real.
        import shutil
        verify_dir = os.path.join(cfg.workspace_root, "verify_final")
        os.makedirs(verify_dir, exist_ok=True)
        shutil.copy(promoted["patch"], os.path.join(verify_dir, "calc.py"))
        shutil.copy(promoted["regression-test"],
                    os.path.join(verify_dir, "test_regression.py"))
        criteria = [
            {"criterion": "full suite green on delivered artifacts",
             "method": "tests",
             "command": "python3 -m unittest test_regression -v",
             "cwd": "verify_final"},
        ]
        results = verify_mod.verify_criteria(criteria, cfg)
        summary = verify_mod.summarize(results)

        # 8. Structured final report.
        report = {
            "goal": "Inspect, fix, test, verify",
            "workers": {
                sid: {"profile": r["profile"], "status": r["status"],
                      "summary": r["summary"][:200]}
                for sid, r in r2["results"].items()
            },
            "verification": summary,
            "elapsed_s": round(elapsed, 1),
        }
        self.assertEqual(report["verification"]["verdict"], "pass")
        self.assertEqual(
            {w["profile"] for w in report["workers"].values()},
            {"coder", "tester", "reviewer"})
        print("\nFINAL REPORT:\n" + json.dumps(report, indent=1)[:1500])

    def test_failure_injection_retry(self):
        """A failed worker is retried without restarting the mission."""
        cfg = make_cfg()
        attempts = {"n": 0}
        orig = d._run_tool_worker

        def flaky(subtask, worker_id, cfg_, ctx, approvals):
            if subtask.id == "flaky" and attempts["n"] == 0:
                attempts["n"] += 1
                from nextclaw.delegation import WorkerResult
                return WorkerResult(
                    task_id="", worker_id=worker_id, profile="tool",
                    subtask_id="flaky", status="failed",
                    summary="injected failure",
                    errors=["timeout talking to flaky service"],
                    usage={})
            return orig(subtask, worker_id, cfg_, ctx, approvals)

        d._run_tool_worker = flaky
        try:
            report = d.run_delegation(cfg, "test", [
                Subtask(id="flaky", name="flaky", goal="g", kind="tool",
                        tool="fs.list", args={"path": "."}, max_retries=2),
                Subtask(id="after", name="after", goal="g", kind="tool",
                        tool="fs.list", args={"path": "."},
                        depends_on=["flaky"]),
            ])
        finally:
            d._run_tool_worker = orig
        # First attempt failed with a timeout-like error -> retried -> ok.
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["results"]["flaky"]["status"], "completed")
        self.assertEqual(report["results"]["after"]["status"], "completed")
        self.assertEqual(attempts["n"], 1)


if __name__ == "__main__":
    unittest.main()
