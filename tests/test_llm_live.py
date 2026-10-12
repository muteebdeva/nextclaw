"""LIVE model tests - kept separate from mocked tests.

These only run with NEXTCLAW_LIVE_TESTS=1 and a configured API key, so the
normal suite never burns quota or depends on the network.
"""
import json
import os
import tempfile
import unittest

LIVE = os.environ.get("NEXTCLAW_LIVE_TESTS") == "1"


@unittest.skipUnless(LIVE, "live model test: set NEXTCLAW_LIVE_TESTS=1")
class LiveAgentTests(unittest.TestCase):
    def test_real_file_report_end_to_end(self):
        from nextclaw import agent_llm
        from nextclaw.config import Config, ensure_data_dir

        ws = tempfile.mkdtemp(prefix="nc-live-ws-")
        data = tempfile.mkdtemp(prefix="nc-live-data-")
        os.makedirs(os.path.join(ws, "src"), exist_ok=True)
        with open(os.path.join(ws, "src", "a.py"), "w") as f:
            f.write("x = 1\n" * 6)
        with open(os.path.join(ws, "src", "b.py"), "w") as f:
            f.write("y = 2\n" * 4)

        cfg = Config(data_dir=data, workspace_root=ws)
        ensure_data_dir(cfg)
        tid = agent_llm.create_agent_task(
            "Count the Python files under src and write a verified report to reports/r.md",
            cfg,
            verify={"tool": "verify.file_report",
                    "args": {"report_path": "reports/r.md", "root": "src",
                             "pattern": "**/*.py"}},
        )
        task = agent_llm.run_llm_task(tid, cfg)
        self.assertEqual(task["status"], "completed", task.get("error"))
        self.assertTrue(task["result"]["verified"])
        self.assertEqual(task["result"]["verify_output"]["expected_lines"], 10)


if __name__ == "__main__":
    unittest.main()
