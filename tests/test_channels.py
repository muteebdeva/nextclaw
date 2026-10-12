"""Tests for the Telegram channel (fully mocked transport)."""
import tempfile
import unittest

from nextclaw.channels import ChannelRunner, IncomingMessage, TelegramChannel
from nextclaw.config import Config


class FakeTransport:
    def __init__(self):
        self.sent = []          # (chat_id, text)
        self.updates = []       # queued update dicts

    def get(self, url, params):
        assert url.endswith("/getUpdates"), url
        offset = params.get("offset", 0)
        result = [u for u in self.updates if u["update_id"] >= offset]
        return {"ok": True, "result": result}

    def post(self, url, data):
        assert url.endswith("/sendMessage"), url
        self.sent.append((data["chat_id"], data["text"]))
        return {"ok": True, "result": {}}


def _update(uid, user_id, text, chat_id=100):
    return {"update_id": uid,
            "message": {"message_id": uid, "chat": {"id": chat_id},
                        "from": {"id": user_id}, "text": text}}


class TelegramChannelTest(unittest.TestCase):
    def test_get_updates_parses(self):
        ft = FakeTransport()
        ft.updates = [_update(7, 42, "hello")]
        ch = TelegramChannel("dummy-token", transport=ft)
        msgs = ch.get_updates(offset=0)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].update_id, 7)
        self.assertEqual(msgs[0].user_id, 42)
        self.assertEqual(msgs[0].text, "hello")

    def test_send_chunks_long_messages(self):
        ft = FakeTransport()
        ch = TelegramChannel("dummy-token", transport=ft)
        ch.send(100, "x" * 9000)
        self.assertEqual(len(ft.sent), 3)
        self.assertTrue(all(len(t) <= 4000 for _, t in ft.sent))

    def test_needs_token(self):
        from nextclaw.channels import ChannelError
        with self.assertRaises(ChannelError):
            TelegramChannel("")


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ws = tempfile.mkdtemp()
        self.cfg = Config(data_dir=self.d, workspace_root=self.ws)
        self.ft = FakeTransport()
        self.ch = TelegramChannel("dummy-token", transport=self.ft)

    def _runner(self, allowed):
        import os
        os.environ["NEXTCLAW_TELEGRAM_ALLOWED_IDS"] = ",".join(str(i) for i in allowed)
        try:
            return ChannelRunner(self.cfg, channel=self.ch)
        finally:
            os.environ.pop("NEXTCLAW_TELEGRAM_ALLOWED_IDS", None)

    def test_unauthorized_ignored(self):
        r = self._runner([1])
        msg = IncomingMessage(1, 100, 999, "/tasks")
        self.assertIsNone(r.handle_message(msg))
        self.assertEqual(self.ft.sent, [])

    def test_tasks_empty(self):
        r = self._runner([42])
        r.handle_message(IncomingMessage(1, 100, 42, "/tasks"))
        self.assertEqual(len(self.ft.sent), 1)
        self.assertIn("No tasks", self.ft.sent[0][1])

    def test_help(self):
        r = self._runner([42])
        r.handle_message(IncomingMessage(1, 100, 42, "hi there"))
        self.assertIn("/task", self.ft.sent[0][1])

    def test_offset_persistence_no_duplicates(self):
        r = self._runner([42])
        self.assertEqual(r._load_offset(), 0)
        r._save_offset(123)
        r2 = self._runner([42])
        self.assertEqual(r2._load_offset(), 123)

    def test_cancel_unknown(self):
        r = self._runner([42])
        r.handle_message(IncomingMessage(1, 100, 42, "/cancel nope"))
        self.assertIn("Unknown task", self.ft.sent[0][1])


if __name__ == "__main__":
    unittest.main()
