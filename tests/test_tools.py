import os
import tempfile
import unittest

from nextclaw import tools
from nextclaw.config import Config, ensure_data_dir


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="nc-t-ws-")
        self.data = tempfile.mkdtemp(prefix="nc-t-data-")
        self.cfg = Config(data_dir=self.data, workspace_root=self.ws)
        ensure_data_dir(self.cfg)

    def test_fs_write_read_roundtrip(self):
        r = tools.execute("fs.write", {"path": "notes/a.txt", "content": "hello"}, self.cfg)
        self.assertTrue(r.ok, r.error)
        r2 = tools.execute("fs.read", {"path": "notes/a.txt"}, self.cfg)
        self.assertTrue(r2.ok, r2.error)
        self.assertEqual(r2.output, "hello")

    def test_path_traversal_blocked(self):
        with self.assertRaises(tools.PermissionDenied):
            tools.execute("fs.write", {"path": "../evil.txt", "content": "x"}, self.cfg)

    def test_absolute_escape_blocked(self):
        with self.assertRaises(tools.PermissionDenied):
            tools.execute("fs.read", {"path": "/etc/passwd"}, self.cfg)

    def test_exec_requires_approval(self):
        with self.assertRaises(tools.ApprovalRequired):
            tools.execute("exec.run", {"command": "echo hi"}, self.cfg)

    def test_exec_denylist(self):
        with self.assertRaises(tools.PermissionDenied):
            tools.execute(
                "exec.run",
                {"command": "rm -rf /tmp/should-not-run"},
                self.cfg,
                approvals=("exec.run",),
            )

    def test_unknown_tool(self):
        with self.assertRaises(tools.ToolError):
            tools.execute("nope.missing", {}, self.cfg)


if __name__ == "__main__":
    unittest.main()
