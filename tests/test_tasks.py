import tempfile
import unittest

from nextclaw.config import Config, ensure_data_dir
from nextclaw.tasks import TaskStore


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="nc-ts-")
        self.cfg = Config(data_dir=self.data, workspace_root=tempfile.mkdtemp(prefix="nc-ts-ws-"))
        ensure_data_dir(self.cfg)
        self.store = TaskStore(self.data)

    def test_create_and_step_lifecycle(self):
        tid = self.store.create_task("goal", "file_report", {"a": 1})
        self.store.add_steps(tid, [{"name": "s1", "tool": "fs.list", "args": {}, "verify": {}}])
        steps = self.store.get_steps(tid)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["status"], "pending")
        self.store.update_step(steps[0]["id"], status="running", attempts=1)
        self.assertEqual(self.store.reset_running_steps(tid), 1)
        self.assertEqual(self.store.get_steps(tid)[0]["status"], "pending")
        self.store.set_task_status(tid, "completed", result={"ok": True})
        self.assertEqual(self.store.get_task(tid)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
