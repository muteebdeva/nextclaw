"""Tests for the autonomous execution upgrade:
fs.edit + git tools, exec failures/timeouts, browser adapter,
missions (pause/resume/recovery/idempotency/budgets), verification engine,
and acceptance-criteria integration in the agent loop.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest

from nextclaw import tools
from nextclaw.config import Config, ensure_data_dir
from nextclaw import missions
from nextclaw.missions import MissionStore
from nextclaw import verify as verify_mod


def make_cfg(**kw):
    ws = tempfile.mkdtemp(prefix="nc-auto-ws-")
    data = tempfile.mkdtemp(prefix="nc-auto-data-")
    cfg = Config(data_dir=data, workspace_root=ws, **kw)
    ensure_data_dir(cfg)
    return cfg


class FsEditTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_edit_replaces_once(self):
        tools.execute("fs.write", {"path": "a.py", "content": "x = 1\ny = 2\n"}, self.cfg)
        r = tools.execute(
            "fs.edit", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 42"},
            self.cfg)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.output["replaced"], 1)
        r2 = tools.execute("fs.read", {"path": "a.py"}, self.cfg)
        self.assertIn("x = 42", r2.output)

    def test_edit_missing_old_string_fails(self):
        tools.execute("fs.write", {"path": "a.py", "content": "x = 1\n"}, self.cfg)
        r = tools.execute(
            "fs.edit", {"path": "a.py", "old_string": "zzz", "new_string": "q"},
            self.cfg)
        self.assertFalse(r.ok)
        self.assertIn("not found", r.error)

    def test_edit_ambiguous_requires_count_all(self):
        tools.execute("fs.write", {"path": "a.py", "content": "x=1\nx=1\n"}, self.cfg)
        r = tools.execute(
            "fs.edit", {"path": "a.py", "old_string": "x=1", "new_string": "x=2"},
            self.cfg)
        self.assertFalse(r.ok)
        self.assertIn("occurs 2 times", r.error)
        r2 = tools.execute(
            "fs.edit", {"path": "a.py", "old_string": "x=1", "new_string": "x=2",
                        "count": "all"},
            self.cfg)
        self.assertTrue(r2.ok, r2.error)
        self.assertEqual(r2.output["replaced"], 2)

    def test_edit_traversal_blocked(self):
        with self.assertRaises(tools.PermissionDenied):
            tools.execute(
                "fs.edit", {"path": "../evil", "old_string": "a", "new_string": "b"},
                self.cfg)


class GitToolTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        subprocess.run(["git", "init", "-q"], cwd=self.cfg.workspace_root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.cfg.workspace_root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.cfg.workspace_root, check=True)
        tools.execute("fs.write", {"path": "f.txt", "content": "v1"}, self.cfg)

    def test_status_and_diff(self):
        r = tools.execute("git.status", {}, self.cfg)
        self.assertTrue(r.ok, r.error)
        self.assertIn("f.txt", r.output)
        r2 = tools.execute("git.diff", {}, self.cfg)
        self.assertTrue(r2.ok, r2.error)

    def test_git_outside_repo_fails(self):
        cfg2 = make_cfg()  # no git init
        r = tools.execute("git.status", {}, cfg2)
        self.assertFalse(r.ok)
        self.assertIn("not inside a git", r.error)


class ExecFailureTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_nonzero_exit_reported(self):
        r = tools.execute(
            "exec.run", {"command": "python3 -c 'import sys; sys.exit(3)'"},
            self.cfg, approvals=("exec.run",))
        self.assertTrue(r.ok)  # tool ran; the *command* failed
        self.assertEqual(r.output["returncode"], 3)

    def test_stderr_captured(self):
        r = tools.execute(
            "exec.run", {"command": "python3 -c 'import sys; sys.stderr.write(\"boom\")'"},
            self.cfg, approvals=("exec.run",))
        self.assertTrue(r.ok)
        self.assertIn("boom", r.output["stderr"])

    def test_timeout_kills_command(self):
        cfg = make_cfg()
        cfg.step_timeout_s = 2
        r = tools.execute(
            "exec.run", {"command": "python3 -c 'import time; time.sleep(30)'", "timeout": 2},
            cfg, approvals=("exec.run",))
        self.assertFalse(r.ok)
        self.assertIn("timed out", r.error.lower())


class BrowserAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from nextclaw import browsing
        try:
            cls.sess = browsing.get_session()
        except browsing.BrowserError as exc:
            raise unittest.SkipTest(f"no browser available: {exc}")

    @classmethod
    def tearDownClass(cls):
        from nextclaw import browsing
        browsing.close_all()

    def test_navigate_and_snapshot(self):
        self.sess.navigate(
            "data:text/html,<html><head><title>T</title></head>"
            "<body><h1>Hi</h1><button id='go'>Go</button></body></html>")
        snap = self.sess.snapshot()
        self.assertIn("<button>", snap)
        self.assertIn("#go", snap)

    def test_click_missing_selector_errors(self):
        from nextclaw import browsing
        with self.assertRaises(browsing.BrowserError):
            self.sess.click("#does-not-exist-xyz")

    def test_fill_and_readback(self):
        self.sess.navigate(
            "data:text/html,<body><input id='n' value=''></body></html>")
        self.sess.fill("#n", "hello")
        val = self.sess.evaluate("document.querySelector('#n').value")
        self.assertEqual(val, "hello")

    def test_wait_for_text(self):
        self.sess.navigate("data:text/html,<body><p>waiting</p></body></html>")
        r = self.sess.wait_for(text="waiting", timeout_s=5)
        self.assertTrue(r["ok"])

    def test_wait_timeout_errors(self):
        from nextclaw import browsing
        with self.assertRaises(browsing.BrowserError):
            self.sess.wait_for(text="never-appears-xyz", timeout_s=2)

    def test_screenshot_is_png(self):
        raw = self.sess.screenshot()
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))


class BrowserToolPolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_open_rejects_bad_scheme(self):
        r = tools.execute("browser.open", {"url": "gopher://x"}, self.cfg)
        self.assertFalse(r.ok)

    def test_open_rejects_file_by_default(self):
        r = tools.execute("browser.open", {"url": "file:///etc/passwd"}, self.cfg)
        self.assertFalse(r.ok)
        self.assertIn("file://", r.error)

    def test_allowlist_blocks(self):
        from nextclaw.config import Config
        cfg = make_cfg()
        cfg.browser_navigate_allowlist = ("example.com",)
        r = tools.execute("browser.open", {"url": "https://other.test/"}, cfg)
        self.assertFalse(r.ok)
        self.assertIn("allowlist", r.error)

    def test_click_requires_approval(self):
        with self.assertRaises(tools.ApprovalRequired):
            tools.execute("browser.click", {"selector": "#x"}, self.cfg)


class MissionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.store = MissionStore(self.cfg.data_dir)

    def _simple_plan(self):
        return [
            {"name": "write", "tool": "fs.write",
             "args": {"path": "out.txt", "content": "hello"}},
            {"name": "read", "tool": "fs.read", "args": {"path": "out.txt"},
             "depends_on": ["write"]},
        ]

    def test_run_to_completion(self):
        mid = self.store.create_mission("test", plan=self._simple_plan())
        final = missions.run_mission(mid, self.cfg)
        self.assertEqual(final["state"], "completed")
        steps = self.store.get_steps(mid)
        self.assertTrue(all(s["status"] == "completed" for s in steps))
        r = tools.execute("fs.read", {"path": "out.txt"}, self.cfg)
        self.assertEqual(r.output, "hello")

    def test_pause_and_resume(self):
        mid = self.store.create_mission("test", plan=self._simple_plan())
        self.store.request_pause(mid)
        m1 = missions.run_mission(mid, self.cfg)
        self.assertEqual(m1["state"], "paused")
        # Only zero/one steps ran before the pause check; resume finishes.
        m2 = missions.resume_mission(mid, self.cfg)
        self.assertEqual(m2["state"], "completed")

    def test_cancel(self):
        mid = self.store.create_mission("test", plan=self._simple_plan())
        self.store.request_cancel(mid)
        m = missions.run_mission(mid, self.cfg)
        self.assertEqual(m["state"], "cancelled")

    def test_step_failure_retries_then_fails(self):
        mid = self.store.create_mission(
            "test",
            plan=[{"name": "bad", "tool": "fs.read", "args": {"path": "missing.txt"}}],
            budgets={"max_retries_per_step": 2})
        m = missions.run_mission(mid, self.cfg)
        self.assertEqual(m["state"], "failed")
        steps = self.store.get_steps(mid)
        self.assertEqual(steps[0]["status"], "failed")
        self.assertEqual(steps[0]["attempts"], 2)

    def test_budget_exhaustion(self):
        plan = [{"name": f"s{i}", "tool": "fs.write",
                 "args": {"path": f"f{i}.txt", "content": "x"}} for i in range(5)]
        mid = self.store.create_mission("test", plan=plan,
                                        budgets={"max_tool_calls": 2})
        m = missions.run_mission(mid, self.cfg)
        self.assertEqual(m["state"], "failed")
        self.assertIn("budget", m["error"])

    def test_idempotent_recovery_no_duplicates(self):
        calls = []

        orig_execute = tools.execute

        def counting(name, args, cfg, approvals=()):
            calls.append(name)
            return orig_execute(name, args, cfg, approvals)

        tools.execute = counting
        try:
            mid = self.store.create_mission("test", plan=self._simple_plan())
            missions.run_mission(mid, self.cfg)
            n_first = len(calls)
            # Simulate a second run after "restart": completed actions must not repeat.
            m2 = missions.run_mission(mid, self.cfg)
            self.assertEqual(m2["state"], "completed")
            self.assertEqual(len(calls), n_first)
        finally:
            tools.execute = orig_execute

    def test_crash_recovery_resets_running(self):
        mid = self.store.create_mission("test", plan=self._simple_plan())
        steps = self.store.get_steps(mid)
        self.store.set_step_status(steps[0]["id"], "running")  # simulate crash mid-step
        reset = self.store.reset_running_steps(mid)
        self.assertEqual(reset, 1)
        m = missions.run_mission(mid, self.cfg)
        self.assertEqual(m["state"], "completed")

    def test_permission_denied_fails_without_retry(self):
        mid = self.store.create_mission(
            "test",
            plan=[{"name": "evil", "tool": "fs.read", "args": {"path": "../x"}}],
            budgets={"max_retries_per_step": 3})
        m = missions.run_mission(mid, self.cfg)
        self.assertEqual(m["state"], "failed")
        self.assertIn("permission", m["error"].lower())
        steps = self.store.get_steps(mid)
        self.assertEqual(steps[0]["attempts"], 1)  # no retry on policy denial

    def test_waiting_for_approval_state(self):
        mid = self.store.create_mission(
            "test",
            plan=[{"name": "risky", "tool": "exec.run",
                   "args": {"command": "echo hi"}}])
        m = missions.run_mission(mid, self.cfg)  # no approver wired
        self.assertEqual(m["state"], "waiting_for_approval")


class VerificationEngineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_files_pass(self):
        tools.execute("fs.write", {"path": "r.md", "content": "Total lines: 5"}, self.cfg)
        res = verify_mod.verify_criteria(
            [{"criterion": "report exists", "method": "files",
              "files": [{"path": "r.md", "contains": "Total lines:"}]}], self.cfg)
        self.assertEqual(res[0]["status"], "pass")

    def test_files_fail(self):
        res = verify_mod.verify_criteria(
            [{"criterion": "missing file", "method": "files",
              "files": [{"path": "nope.txt"}]}], self.cfg)
        self.assertEqual(res[0]["status"], "fail")

    def test_command_pass(self):
        res = verify_mod.verify_criteria(
            [{"criterion": "echo works", "method": "command",
              "command": "python3 -c 'print(123)'", "expect_exit": 0,
              "expect_output": "123"}], self.cfg)
        self.assertEqual(res[0]["status"], "pass")

    def test_command_fail_wrong_exit(self):
        res = verify_mod.verify_criteria(
            [{"criterion": "bad exit", "method": "command",
              "command": "python3 -c 'import sys; sys.exit(1)'", "expect_exit": 0}],
            self.cfg)
        self.assertEqual(res[0]["status"], "fail")

    def test_tests_method(self):
        tools.execute("fs.write", {"path": "t_ok.py",
                                   "content": "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n"},
                      self.cfg)
        res = verify_mod.verify_criteria(
            [{"criterion": "suite green", "method": "tests",
              "command": "python3 -m unittest t_ok -v"}], self.cfg)
        self.assertEqual(res[0]["status"], "pass")

    def test_unknown_method_inconclusive(self):
        res = verify_mod.verify_criteria(
            [{"criterion": "mystery", "method": "telepathy"}], self.cfg)
        self.assertEqual(res[0]["status"], "inconclusive")

    def test_missing_spec_inconclusive(self):
        res = verify_mod.verify_criteria(
            [{"criterion": "vague", "method": "files", "files": []}], self.cfg)
        self.assertEqual(res[0]["status"], "inconclusive")

    def test_summarize(self):
        s = verify_mod.summarize([
            {"status": "pass"}, {"status": "fail"}, {"status": "inconclusive"}])
        self.assertEqual(s["verdict"], "fail")
        self.assertEqual((s["passed"], s["failed"], s["inconclusive"]), (1, 1, 1))


class AcceptanceCriteriaLoopTests(unittest.TestCase):
    """The agent loop verifies acceptance criteria before finishing."""

    def setUp(self):
        self.cfg = make_cfg()
        self.cfg.max_turns = 6

    def _run(self, script, params):
        from nextclaw import agent_llm, llm as llm_mod
        from nextclaw.tasks import TaskStore

        class ScriptedProvider(llm_mod.LLMProvider):
            name = "scripted"

            def __init__(self, script):
                self.script = list(script)

            def chat(self, system, history, declarations, timeout_s=60):
                if not self.script:
                    return llm_mod.LLMResponse(text="done", tool_calls=[])
                text, calls = self.script.pop(0)
                return llm_mod.LLMResponse(
                    text=text,
                    tool_calls=[llm_mod.ToolCall(id=f"c{i}", name=n, args=a)
                                for i, (n, a) in enumerate(calls)],
                    latency_ms=1)

        store = TaskStore(self.cfg.data_dir)
        tid = store.create_task("fix it", "agent", params)
        return agent_llm.run_llm_task(tid, self.cfg, provider=ScriptedProvider(script))

    def test_criteria_pass_completes(self):
        tools.execute("fs.write", {"path": "ok.txt", "content": "yes"}, self.cfg)
        task = self._run(
            [("done", [])],
            {"acceptance_criteria": [
                {"criterion": "file exists", "method": "files",
                 "files": [{"path": "ok.txt", "contains": "yes"}]}]})
        self.assertEqual(task["status"], "completed")
        self.assertTrue(task["result"]["verified"])

    def test_criteria_fail_retries_then_fails(self):
        # Model declares done twice without fixing; verification keeps failing.
        task = self._run(
            [("done", []), ("done again", []), ("done thrice", [])],
            {"acceptance_criteria": [
                {"criterion": "impossible", "method": "files",
                 "files": [{"path": "never.txt"}]}],
             "verify_retries": 1})
        self.assertEqual(task["status"], "failed")
        self.assertIn("acceptance criteria", task["error"])
        self.assertFalse(task["result"]["verified"])


if __name__ == "__main__":
    unittest.main()
