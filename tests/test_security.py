import tempfile
import unittest

from nextclaw import tools
from nextclaw.config import Config, ensure_data_dir


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="nc-s-ws-")
        self.data = tempfile.mkdtemp(prefix="nc-s-data-")
        self.cfg = Config(data_dir=self.data, workspace_root=self.ws)
        ensure_data_dir(self.cfg)

    def test_secret_redaction_in_read(self):
        tools.execute(
            "fs.write",
            {"path": "s.txt", "content": "api_key = sk-abcdefghijklmnop123456"},
            self.cfg,
        )
        r = tools.execute("fs.read", {"path": "s.txt"}, self.cfg)
        self.assertTrue(r.ok, r.error)
        self.assertNotIn("sk-abcdefghijklmnop123456", r.output)
        self.assertIn("[REDACTED]", r.output)

    def test_redact_private_key_block(self):
        blob = "-----BEGIN PRIVATE KEY-----\nABCDEF\n-----END PRIVATE KEY-----"
        self.assertIn("[REDACTED]", tools.redact_secrets(blob))


if __name__ == "__main__":
    unittest.main()
