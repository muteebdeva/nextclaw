import os
import tempfile
import unittest

from nextclaw import agent
from nextclaw.config import Config, ensure_data_dir
from nextclaw.memory import MemoryStore


def _write(ws, rel, content):
    real = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "w", encoding="utf-8") as f:
        f.write(content)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="nc-a-ws-")
        self.data = tempfile.mkdtemp(prefix="nc-a-data-")
        self.cfg = Config(data_dir=self.data, workspace_root=self.ws)
        ensure_data_dir(self.cfg)

    def _fixture(self):
        _write(self.ws, "src/a.py", "x = 1\n" * 10)  # 10 lines
        _write(self.ws, "src/b.py", "y = 2\n" * 4)   # 4 lines

    def test_file_report_end_to_end(self):
        self._fixture()
        tid = agent.create_task(
            "Count python files and write verified report",
            "file_report",
            {"root": "src", "pattern": "**/*.py", "output": "reports/out.md"},
            self.cfg,
        )
        task = agent.run_task(tid, self.cfg)
        self.assertEqual(task["status"], "completed", task.get("error"))
        result = task["result"]
        self.assertTrue(result["verified"])
        self.assertEqual(result["total_files"], 2)
        self.assertEqual(result["total_lines"], 14)
        self.assertTrue(os.path.isfile(os.path.join(self.ws, "reports/out.md")))
        # memory recorded the completion
        mem = MemoryStore(self.data)
        hits = mem.recall(tid)
        self.assertTrue(any("Completed task" in h.text for h in hits))

    def test_crash_recovery_resumes(self):
        self._fixture()
        tid = agent.create_task(
            "Recover after interruption",
            "file_report",
            {"root": "src", "pattern": "**/*.py", "output": "reports/r.md"},
            self.cfg,
        )
        with self.assertRaises(RuntimeError):
            agent.run_task(tid, self.cfg, fail_after_step=1)
        task = agent.run_task(tid, self.cfg)
        self.assertEqual(task["status"], "completed", task.get("error"))
        self.assertTrue(task["result"]["verified"])

    def test_unsupported_task_type_fails_closed(self):
        with self.assertRaises(Exception):
            agent.create_task("do magic", "teleport", {}, self.cfg)


if __name__ == "__main__":
    unittest.main()
