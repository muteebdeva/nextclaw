"""Provider dashboard API tests (real HTTP, temp data dir, mocked providers)."""
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest import mock

from nextclaw import providers as P
from nextclaw.config import load_config
from nextclaw.server import Handler, ServerContext


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ProviderApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.ws = tempfile.mkdtemp()
        cfg = load_config(overrides={"data_dir": cls.d, "workspace_root": cls.ws})
        cls.ctx = ServerContext(cfg)
        cls.ctx.token = "test-token"
        Handler.ctx = cls.ctx
        cls.port = _free_port()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", cls.port), Handler)
        cls.srv.daemon_threads = True
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.ctx.runner.shutdown(wait=False)

    def req(self, path, method="GET", body=None, token="test-token"):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_presets_listed(self):
        code, data = self.req("/api/providers/presets")
        self.assertEqual(code, 200)
        self.assertGreaterEqual(len(data["presets"]), 15)
        ids = {p["id"] for p in data["presets"]}
        self.assertIn("openai", ids)
        self.assertIn("ollama", ids)

    def test_crud_masks_secrets(self):
        code, created = self.req("/api/providers/configs", "POST", {
            "name": "api-test-oai", "adapter": "openai_compat",
            "base_url": "https://api.openai.com", "model": "gpt-5-mini",
            "api_key": "sk-secret-value-1234567890"})
        self.assertEqual(code, 201)
        pid = created["id"]
        self.assertEqual(created["api_key_masked"], "sk-…7890")
        self.assertNotIn("sk-secret-value", json.dumps(created))

        code, listed = self.req("/api/providers/configs")
        self.assertEqual(code, 200)
        mine = [c for c in listed["configs"] if c["id"] == pid][0]
        self.assertIn("capabilities", mine)
        self.assertTrue(mine["capabilities"]["tools"])
        self.assertNotIn("sk-secret-value", json.dumps(listed))

        # Update without api_key keeps the stored secret.
        code, upd = self.req(f"/api/providers/configs/{pid}", "PUT",
                             {"model": "gpt-5"})
        self.assertEqual(code, 200)
        self.assertEqual(upd["api_key_masked"], "sk-…7890")

        # Unknown adapter rejected.
        code, data = self.req("/api/providers/configs", "POST",
                              {"name": "bad", "adapter": "nope", "model": "m"})
        self.assertEqual(code, 400)

        # SSRF: metadata endpoint rejected.
        code, data = self.req("/api/providers/configs", "POST",
                              {"name": "evil", "adapter": "openai_compat",
                               "base_url": "http://169.254.169.254/", "model": "m"})
        self.assertEqual(code, 400)
        self.assertIn("metadata", data["error"])

        # Delete.
        code, _ = self.req(f"/api/providers/configs/{pid}", "DELETE")
        self.assertEqual(code, 200)
        code, _ = self.req(f"/api/providers/configs/{pid}")
        self.assertEqual(code, 404)

    def test_routing(self):
        _, a = self.req("/api/providers/configs", "POST",
                        {"name": "rt-a", "adapter": "ollama", "model": "m",
                         "base_url": "http://127.0.0.1:11434", "allow_private": True})
        _, b = self.req("/api/providers/configs", "POST",
                        {"name": "rt-b", "adapter": "ollama", "model": "m2",
                         "base_url": "http://127.0.0.1:11434", "allow_private": True})
        code, _ = self.req("/api/providers/routing", "POST",
                           {"default_id": b["id"], "fallback_ids": [a["id"]]})
        self.assertEqual(code, 200)
        code, r = self.req("/api/providers/routing")
        self.assertEqual(r["default_id"], b["id"])
        self.assertEqual(r["fallback_ids"], [a["id"]])
        # Default cannot be its own fallback.
        code, data = self.req("/api/providers/routing", "POST",
                              {"fallback_ids": [b["id"]]})
        self.assertEqual(code, 400)

    def test_test_endpoint_uses_adapter(self):
        _, c = self.req("/api/providers/configs", "POST",
                        {"name": "rt-t", "adapter": "ollama", "model": "m",
                         "base_url": "http://127.0.0.1:11434", "allow_private": True})
        with mock.patch.object(P.OllamaAdapter, "test_connection",
                               return_value={"ok": True, "models": ["m"]}):
            code, data = self.req(f"/api/providers/configs/{c['id']}/test", "POST")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])

    def test_chat_endpoint(self):
        _, c = self.req("/api/providers/configs", "POST",
                        {"name": "rt-c", "adapter": "openai_compat",
                         "base_url": "https://api.openai.com", "model": "m",
                         "api_key": "sk-x"})
        fake_resp = P.LLMResponse(text="hello test", finish_reason="stop",
                                  latency_ms=12)
        with mock.patch.object(P.OpenAICompatProvider, "chat",
                               return_value=fake_resp):
            code, data = self.req(f"/api/providers/configs/{c['id']}/chat",
                                  "POST", {"prompt": "hi"})
        self.assertEqual(code, 200)
        self.assertEqual(data["text"], "hello test")
        self.assertEqual(data["latency_ms"], 12)

    def test_chat_validates_prompt(self):
        _, c = self.req("/api/providers/configs", "POST",
                        {"name": "rt-v", "adapter": "ollama", "model": "m",
                         "base_url": "http://127.0.0.1:11434", "allow_private": True})
        code, _ = self.req(f"/api/providers/configs/{c['id']}/chat", "POST",
                           {"prompt": ""})
        self.assertEqual(code, 400)

    def test_legacy_providers_summary(self):
        code, data = self.req("/api/providers")
        self.assertEqual(code, 200)
        self.assertIn("providers", data)
        self.assertIn("default_id", data)


if __name__ == "__main__":
    unittest.main()
