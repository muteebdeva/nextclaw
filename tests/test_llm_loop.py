import os
import tempfile
import unittest
from unittest import mock

from nextclaw import agent_llm, llm as llm_mod, tools
from nextclaw.config import Config, ensure_data_dir
from nextclaw.tasks import TaskStore


def _resp(text="", calls=()):
    return llm_mod.LLMResponse(
        text=text,
        tool_calls=[llm_mod.ToolCall(id=f"c{i}", name=n, args=a) for i, (n, a) in enumerate(calls)],
    )


class FakeProvider(llm_mod.LLMProvider):
    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def chat(self, system, history, declarations, timeout_s=60):
        resp = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return resp


class LLMLoopTests(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="nc-llm-ws-")
        self.data = tempfile.mkdtemp(prefix="nc-llm-data-")
        self.cfg = Config(data_dir=self.data, workspace_root=self.ws)
        ensure_data_dir(self.cfg)

    def _write(self, rel, content):
        real = os.path.join(self.ws, rel)
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "w") as f:
            f.write(content)

    def test_tool_loop_completes(self):
        self._write("src/a.py", "x=1\n")
        tid = agent_llm.create_agent_task("list src", self.cfg)
        provider = FakeProvider([
            _resp(calls=[("fs.list", {"path": "src", "pattern": "*.py"})]),
            _resp(text="Done: found src/a.py"),
        ])
        task = agent_llm.run_llm_task(tid, self.cfg, provider=provider)
        self.assertEqual(task["status"], "completed", task.get("error"))
        self.assertIn("Done", task["result"]["final"])
        store = TaskStore(self.data)
        self.assertEqual(store.count_turns(tid), 4)  # user, asst, tool, asst
        steps = [s for s in store.get_steps(tid) if s["status"] == "done"]
        self.assertTrue(any(s["tool"] == "fs.list" for s in steps))

    def test_crash_resume_continues_conversation(self):
        tid = agent_llm.create_agent_task("list then done", self.cfg)
        p1 = FakeProvider([_resp(calls=[("fs.list", {"path": ".", "pattern": "*.py"})])])
        with self.assertRaises(RuntimeError):
            agent_llm.run_llm_task(tid, self.cfg, provider=p1, fail_after_turn=1)
        p2 = FakeProvider([_resp(text="All done")])
        task = agent_llm.run_llm_task(tid, self.cfg, provider=p2)
        self.assertEqual(task["status"], "completed", task.get("error"))
        # fs.list ran exactly once across the crash
        store = TaskStore(self.data)
        done = [s for s in store.get_steps(tid) if s["tool"] == "fs.list" and s["status"] == "done"]
        self.assertEqual(len(done), 1)

    def test_high_risk_tool_needs_approval(self):
        tid = agent_llm.create_agent_task("run something", self.cfg)
        provider = FakeProvider([_resp(calls=[("exec.run", {"command": "echo hi"})])])
        task = agent_llm.run_llm_task(tid, self.cfg, provider=provider)
        self.assertEqual(task["status"], "awaiting_approval")
        # now approve and resume
        provider2 = FakeProvider([_resp(text="done")])
        task2 = agent_llm.run_llm_task(tid, self.cfg, provider=provider2, approvals=("exec.run",))
        # model already moved on; the approved call is not retried, loop ends
        self.assertIn(task2["status"], ("completed", "awaiting_approval", "failed"))

    def test_verification_gate(self):
        # model writes a report with wrong totals; verifier must fail the task
        tid = agent_llm.create_agent_task(
            "write report",
            self.cfg,
            verify={"tool": "verify.file_report",
                    "args": {"report_path": "r.md", "root": ".", "pattern": "*.py"}},
        )
        self._write("a.py", "x\n" * 3)
        provider = FakeProvider([
            _resp(calls=[("fs.write", {"path": "r.md", "content": "# R\n\nFiles: 99\nTotal lines: 99\n"})]),
            _resp(text="report written"),
        ])
        task = agent_llm.run_llm_task(tid, self.cfg, provider=provider)
        self.assertEqual(task["status"], "failed")
        self.assertIn("verification failed", task["error"])

    def test_max_turns_bound(self):
        tid = agent_llm.create_agent_task("never ends", self.cfg)
        provider = FakeProvider([_resp(calls=[("fs.list", {"path": ".", "pattern": "*"})])])
        cfg = Config(data_dir=self.data, workspace_root=self.ws)
        cfg.max_turns = 2
        task = agent_llm.run_llm_task(tid, cfg, provider=provider)
        self.assertEqual(task["status"], "failed")
        self.assertIn("did not finish", task["error"])

    def test_finished_task_never_reruns(self):
        tid = agent_llm.create_agent_task("quick", self.cfg)
        provider = FakeProvider([_resp(text="done")])
        t1 = agent_llm.run_llm_task(tid, self.cfg, provider=provider)
        self.assertEqual(t1["status"], "completed")
        calls_before = provider.calls
        t2 = agent_llm.run_llm_task(tid, self.cfg, provider=provider)
        self.assertEqual(t2["status"], "completed")
        self.assertEqual(provider.calls, calls_before)

    def test_policy_denial_reported_to_model(self):
        tid = agent_llm.create_agent_task("escape", self.cfg)
        provider = FakeProvider([
            _resp(calls=[("fs.read", {"path": "/etc/passwd"})]),
            _resp(text="blocked, stopping"),
        ])
        task = agent_llm.run_llm_task(tid, self.cfg, provider=provider)
        self.assertEqual(task["status"], "completed")
        store = TaskStore(self.data)
        tool_turns = [t for t in store.get_turns(tid) if t["role"] == "tool"]
        self.assertTrue(any("denied" in str(t.get("output", "")).lower() for t in tool_turns))

    def test_gemini_fails_closed_without_key(self):
        with mock.patch.dict(os.environ, {"NEXTCLAW_GEMINI_API_KEY": ""}):
            with self.assertRaises(llm_mod.NotConfigured):
                llm_mod.GeminiProvider()

    def test_unknown_llm_provider_fails_closed(self):
        cfg = Config(data_dir=self.data, workspace_root=self.ws)
        cfg.llm_provider = "mystery"
        with self.assertRaises(llm_mod.ProviderError):
            llm_mod.llm_provider(cfg)

    def test_declarations_respect_allowlist(self):
        decls = llm_mod.build_declarations(("fs.list", "nope.tool"))
        self.assertEqual([d["name"] for d in decls], ["fs.list"])

    def test_verified_ok_short_circuits(self):
        # Once the task's verifier reports ok:true, the loop must stop
        # instead of letting the model second-guess the verified result.
        self._write("a.py", "x\n" * 3)
        tid = agent_llm.create_agent_task(
            "write a correct report",
            self.cfg,
            verify={"tool": "verify.file_report",
                    "args": {"report_path": "r.md", "root": ".", "pattern": "*.py"}},
        )
        provider = FakeProvider([
            _resp(calls=[("fs.write", {"path": "r.md",
                                       "content": "# R\n\nFiles: 1\nTotal lines: 3\n"})]),
            _resp(calls=[("verify.file_report",
                           {"report_path": "r.md", "root": ".", "pattern": "*.py"})]),
            _resp(calls=[("fs.write", {"path": "r.md", "content": "second guess"})]),
        ])
        task = agent_llm.run_llm_task(tid, self.cfg, provider=provider)
        self.assertEqual(task["status"], "completed", task.get("error"))
        self.assertTrue(task["result"]["verified"])
        self.assertEqual(provider.calls, 2)  # never reached the third scripted turn

    def test_thought_signature_roundtrip(self):
        body = {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [
                    {"functionCall": {"name": "fs.list", "args": {"path": "src"}},
                     "thoughtSignature": "sig123"}
                ]},
            }]
        }
        resp = llm_mod._parse_gemini_response(body, 5)
        self.assertEqual(resp.tool_calls[0].thought_signature, "sig123")
        provider = llm_mod.GeminiProvider.__new__(llm_mod.GeminiProvider)
        contents = provider._to_contents([{
            "role": "assistant", "text": "",
            "tool_calls": [{"name": "fs.list", "args": {"path": "src"},
                            "thought_signature": "sig123"}],
        }])
        part = contents[0]["parts"][0]
        self.assertEqual(part.get("thoughtSignature"), "sig123")
        self.assertEqual(part["functionCall"]["name"], "fs.list")


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="nc-q-data-")
        self.cfg = Config(data_dir=self.data,
                          workspace_root=tempfile.mkdtemp(prefix="nc-q-ws-"))
        ensure_data_dir(self.cfg)

    def _provider(self):
        with mock.patch.dict(os.environ, {"NEXTCLAW_GEMINI_API_KEY": "test-key"}):
            return llm_mod.GeminiProvider(model="gemini-3.7-flash")

    def test_quota_429_not_retried(self):
        calls = []

        def _fake(url, payload, timeout_s, headers=None):
            calls.append(1)
            return 429, {"_http_error": "You exceeded your current quota"}, {}

        with mock.patch("nextclaw.llm._http_post_json", _fake):
            with self.assertRaises(llm_mod.QuotaExhausted):
                agent_llm._chat_with_retry(self._provider(), "s", [], [], self.cfg,
                                           retries=3)
        self.assertEqual(len(calls), 1)  # no retry loop on quota exhaustion

    def test_transient_429_is_retried(self):
        calls = []

        def _fake(url, payload, timeout_s, headers=None):
            calls.append(1)
            if len(calls) < 3:
                return 429, {"_http_error": "too many requests, slow down"}, {}
            return 200, {"candidates": [{"finishReason": "STOP",
                                         "content": {"parts": [{"text": "hi"}]}}]}, {}

        with mock.patch("nextclaw.llm._http_post_json", _fake):
            with mock.patch("time.sleep", lambda s: None):
                resp = agent_llm._chat_with_retry(self._provider(), "s", [], [],
                                                 self.cfg, retries=3)
        self.assertEqual(resp.text, "hi")
        self.assertEqual(len(calls), 3)

    def test_retry_after_header_respected(self):
        seen = {}

        def _fake(url, payload, timeout_s, headers=None):
            return 429, {"_http_error": "slow down"}, {"Retry-After": "7"}

        def _sleep(s):
            seen["slept"] = s

        with mock.patch("nextclaw.llm._http_post_json", _fake):
            with mock.patch("time.sleep", _sleep):
                with self.assertRaises(llm_mod.RateLimited):
                    agent_llm._chat_with_retry(self._provider(), "s", [], [], self.cfg,
                                               retries=1)
        self.assertEqual(seen.get("slept"), 7)

    def test_budget_daily_cap_blocks_without_http(self):
        budget = llm_mod.RequestBudget(self.data)
        self.cfg.llm_max_requests_per_day = 1
        budget.record()
        with self.assertRaises(llm_mod.QuotaExhausted):
            budget.check(self.cfg)

    def test_budget_cooldown_after_quota_exhausted(self):
        budget = llm_mod.RequestBudget(self.data)
        budget.record(quota_exhausted=True)
        with self.assertRaises(llm_mod.QuotaExhausted) as ctx:
            budget.check(self.cfg)
        self.assertIn("cooldown", str(ctx.exception).lower())

    def test_offline_mode_fails_closed(self):
        with mock.patch.dict(os.environ, {"NEXTCLAW_OFFLINE": "1"}):
            with self.assertRaises(llm_mod.NotConfigured):
                llm_mod.llm_provider(self.cfg)


if __name__ == "__main__":
    unittest.main()
