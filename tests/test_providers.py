"""Tests for the universal provider system (all mocked — no network).

Covers: adapter registry, wire-format normalization (OpenAI-compatible,
Anthropic, Bedrock SigV4), error taxonomy, SSRF guard, secret handling,
provider config store, fallback chains, JSON tool-compat mode, streaming,
and the provider dashboard API.
"""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from nextclaw import llm as llm_mod
from nextclaw import providers as P
from nextclaw.config import load_config


# ---------------------------------------------------------------------------
# HTTP fakes
# ---------------------------------------------------------------------------

class FakeHTTPResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status = status
        self._body = body if isinstance(body, (bytes, str)) else json.dumps(body or {})
        if isinstance(self._body, str):
            self._body = self._body.encode()
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def fake_urlopen_factory(routes):
    """routes: list of (method_hint, response-or-exception). Records requests."""
    calls = []

    def _fake(req, timeout=None):
        calls.append(req)
        if not routes:
            raise AssertionError("unexpected HTTP call")
        item = routes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    _fake.calls = calls
    return _fake


def http_error(code, body="boom"):
    return urllib.error.HTTPError("http://x/", code, "err", {}, io.BytesIO(body.encode()))


def _store(tmp=None):
    return P.ProviderStore(tmp or tempfile.mkdtemp())


# ---------------------------------------------------------------------------
# Registry & presets
# ---------------------------------------------------------------------------

class TestRegistry(unittest.TestCase):
    def test_adapters_registered(self):
        self.assertEqual(sorted(P.ADAPTERS),
                         ["anthropic", "bedrock", "gemini", "ollama", "openai_compat"])

    def test_unknown_adapter_rejected(self):
        s = _store()
        with self.assertRaises(P.ProviderError):
            s.create({"name": "x", "adapter": "mystery", "model": "m"})

    def test_presets_well_formed(self):
        self.assertGreaterEqual(len(P.PRESETS), 15)
        for p in P.PRESETS:
            self.assertIn(p["adapter"], P.ADAPTERS, p["id"])
            self.assertTrue(p["display"])

    def test_capabilities_static(self):
        caps = P.adapter_capabilities("openai_compat")
        self.assertTrue(caps["tools"] and caps["streaming"])
        self.assertFalse(caps["vision"])

    def test_build_openai_compat(self):
        s = _store()
        row = s.create({"name": "oai", "adapter": "openai_compat",
                        "base_url": "https://api.openai.com",
                        "model": "gpt-5-mini", "api_key": "sk-test",
                        "allow_private": False})
        full = s.get(row["id"], include_secret=True)
        prov = P.build_provider(full)
        self.assertIsInstance(prov, P.OpenAICompatProvider)
        self.assertEqual(prov.model, "gpt-5-mini")


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter
# ---------------------------------------------------------------------------

_OAI_OK = {
    "choices": [{
        "message": {
            "content": "hello",
            "tool_calls": [{
                "id": "call_9", "type": "function",
                "function": {"name": "fs.list",
                             "arguments": '{"path": "src", "pattern": "**/*.py"}'}}]},
        "finish_reason": "tool_calls"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


class TestOpenAICompat(unittest.TestCase):
    def _prov(self):
        return P.OpenAICompatProvider(base_url="https://api.openai.com",
                                      api_key="sk-test", model="gpt-5-mini")

    def test_chat_parses_tools(self):
        fake = fake_urlopen_factory([FakeHTTPResp(200, _OAI_OK)])
        with mock.patch("urllib.request.urlopen", fake):
            resp = self._prov().chat("sys", [{"role": "user", "text": "hi"}], [])
        self.assertEqual(resp.text, "hello")
        self.assertEqual(len(resp.tool_calls), 1)
        tc = resp.tool_calls[0]
        self.assertEqual(tc.name, "fs.list")
        self.assertEqual(tc.args["path"], "src")
        self.assertEqual(tc.id, "call_9")
        self.assertEqual(resp.finish_reason, "tool_calls")
        # Authorization header sent, key not in URL.
        req = fake.calls[0]
        self.assertEqual(req.headers.get("Authorization"), "Bearer sk-test")
        self.assertNotIn("sk-test", req.full_url)

    def test_history_mapping_tool_ids(self):
        prov = self._prov()
        msgs = prov._messages("s", [
            {"role": "assistant", "text": "",
             "tool_calls": [{"id": "call_1", "name": "fs.read", "args": {"path": "a"}}]},
            {"role": "tool", "name": "fs.read", "output": {"ok": True},
             "tool_call_id": "call_1"},
        ])
        asst = msgs[1]
        self.assertEqual(asst["tool_calls"][0]["id"], "call_1")
        self.assertEqual(msgs[2]["role"], "tool")
        self.assertEqual(msgs[2]["tool_call_id"], "call_1")

    def test_401_is_auth_error(self):
        fake = fake_urlopen_factory([http_error(401, "invalid key")])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(P.AuthError):
                self._prov().chat("s", [], [])

    def test_404_is_invalid_model(self):
        fake = fake_urlopen_factory([http_error(404, "model not found")])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(P.InvalidModelError):
                self._prov().chat("s", [], [])

    def test_429_quota_vs_ratelimit(self):
        fake = fake_urlopen_factory([http_error(429, "exceeded your current quota")])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(P.QuotaExhausted):
                self._prov().chat("s", [], [])
        fake = fake_urlopen_factory([http_error(429, "slow down")])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(P.RateLimited):
                self._prov().chat("s", [], [])

    def test_500_is_transient(self):
        fake = fake_urlopen_factory([http_error(503, "overloaded")])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(P.TransientError):
                self._prov().chat("s", [], [])

    def test_connection_refused_is_transient(self):
        fake = fake_urlopen_factory([urllib.error.URLError("refused")])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(P.TransientError):
                self._prov().chat("s", [], [])

    def test_test_connection_ok(self):
        fake = fake_urlopen_factory([
            FakeHTTPResp(200, {"data": [{"id": "gpt-5-mini"}, {"id": "gpt-5"}]})])
        with mock.patch("urllib.request.urlopen", fake):
            r = self._prov().test_connection()
        self.assertTrue(r["ok"])
        self.assertIn("gpt-5-mini", r["models_sample"])

    def test_test_connection_bad_key(self):
        fake = fake_urlopen_factory([http_error(401, "bad key")])
        with mock.patch("urllib.request.urlopen", fake):
            r = self._prov().test_connection()
        self.assertFalse(r["ok"])
        self.assertIn("invalid key", r["error"])

    def test_stream_assembles_tool_call(self):
        chunks = [
            'data: {"choices": [{"delta": {"content": "working"}}]}',
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_s", "function": {"name": "fs.", "arguments": ""}}]}}]}',
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "list", "arguments": "{\\"path\\""}}]}}]}',
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": \\"x\\"}"}}]}}]}',
            'data: [DONE]',
        ]
        body = ("\n".join(chunks) + "\n").encode()

        class StreamResp(FakeHTTPResp):
            def __iter__(self):
                return iter(body.split(b"\n"))

        fake = fake_urlopen_factory([StreamResp(200, b"")])
        with mock.patch("urllib.request.urlopen", fake):
            events = list(self._prov().chat_stream("s", [{"role": "user", "text": "hi"}], []))
        texts = [e[1] for e in events if e[0] == "text"]
        tools = [e[1] for e in events if e[0] == "tool"]
        self.assertEqual(texts, ["working"])
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "fs.list")
        self.assertEqual(tools[0].args, {"path": "x"})


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------

_ANTH_OK = {
    "type": "message", "stop_reason": "tool_use",
    "content": [
        {"type": "text", "text": "checking"},
        {"type": "tool_use", "id": "toolu_1", "name": "fs.read",
         "input": {"path": "a.txt"}},
    ],
}


class TestAnthropic(unittest.TestCase):
    def _prov(self):
        return P.AnthropicProvider(api_key="sk-ant-test", model="claude-sonnet-4-6")

    def test_chat_parses_tool_use(self):
        fake = fake_urlopen_factory([FakeHTTPResp(200, _ANTH_OK)])
        with mock.patch("urllib.request.urlopen", fake):
            resp = self._prov().chat("sys", [{"role": "user", "text": "hi"}], [])
        self.assertEqual(resp.text, "checking")
        self.assertEqual(len(resp.tool_calls), 1)
        self.assertEqual(resp.tool_calls[0].id, "toolu_1")
        self.assertEqual(resp.tool_calls[0].args, {"path": "a.txt"})
        req = fake.calls[0]
        self.assertEqual(req.get_header("X-api-key"), "sk-ant-test")
        self.assertEqual(req.get_header("Anthropic-version"), "2023-06-01")

    def test_history_tool_results_merged(self):
        prov = self._prov()
        msgs = prov._to_messages([
            {"role": "user", "text": "go"},
            {"role": "assistant", "text": "",
             "tool_calls": [{"id": "toolu_1", "name": "fs.read", "args": {}}]},
            {"role": "tool", "name": "fs.read", "output": {"ok": True},
             "tool_call_id": "toolu_1"},
            {"role": "tool", "name": "fs.list", "output": [1],
             "tool_call_id": "toolu_2"},
        ])
        # user, assistant, then ONE user msg with two tool_result blocks
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        blocks = msgs[2]["content"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["tool_use_id"], "toolu_1")

    def test_no_key_fails_closed(self):
        with self.assertRaises(P.NotConfigured):
            P.AnthropicProvider(api_key="", model="x").chat("s", [], [])

    def test_401_is_auth_error(self):
        fake = fake_urlopen_factory([http_error(401, "bad key")])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(P.AuthError):
                self._prov().chat("s", [], [])


# ---------------------------------------------------------------------------
# Bedrock (SigV4)
# ---------------------------------------------------------------------------

_BEDROCK_OK = {
    "output": {"message": {"role": "assistant", "content": [
        {"text": "done"},
        {"toolUse": {"toolUseId": "tu-1", "name": "fs.list",
                     "input": {"path": "."}}}]}},
    "stopReason": "tool_use",
    "usage": {"inputTokens": 5, "outputTokens": 7},
}


class TestBedrock(unittest.TestCase):
    def _prov(self):
        return P.BedrockProvider(
            model="anthropic.claude-sonnet-4-6-20250514-v1:0",
            region="us-east-1", access_key="AKID", secret_key="SECRET")

    def test_sigv4_headers_wellformed(self):
        fake = fake_urlopen_factory([FakeHTTPResp(200, _BEDROCK_OK)])
        with mock.patch("urllib.request.urlopen", fake):
            resp = self._prov().chat("s", [{"role": "user", "text": "hi"}], [])
        req = fake.calls[0]
        auth = req.headers.get("Authorization", "")
        self.assertTrue(auth.startswith("AWS4-HMAC-SHA256 Credential=AKID/"))
        self.assertIn("SignedHeaders=", auth)
        self.assertIn("Signature=", auth)
        self.assertIn("x-amz-date", {k.lower() for k in req.headers})
        self.assertNotIn("SECRET", auth)
        # Converse endpoint shape.
        self.assertIn("bedrock-runtime.us-east-1.amazonaws.com", req.full_url)
        self.assertIn("/converse", req.full_url)
        # Response parsed.
        self.assertEqual(resp.text, "done")
        self.assertEqual(resp.tool_calls[0].name, "fs.list")
        self.assertEqual(resp.tool_calls[0].id, "tu-1")

    def test_sigv4_deterministic(self):
        h1 = P._sigv4_headers("POST", "https://x/model/m/converse", "us-east-1",
                             "bedrock", "AK", "SK", "", b"{}")
        # Different payload -> different signature (proves payload is signed).
        h2 = P._sigv4_headers("POST", "https://x/model/m/converse", "us-east-1",
                             "bedrock", "AK", "SK", "", b'{"a":1}')
        self.assertNotEqual(h1["authorization"], h2["authorization"])

    def test_missing_creds_fails_closed(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AWS_ACCESS_KEY_ID", None)
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
            with self.assertRaises(P.NotConfigured):
                P.BedrockProvider(model="m", access_key="", secret_key="")

    def test_400_is_provider_error(self):
        fake = fake_urlopen_factory([http_error(400, "bad request")])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(P.ProviderError):
                self._prov().chat("s", [], [])


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

class TestSSRF(unittest.TestCase):
    def test_metadata_blocked_always(self):
        with self.assertRaises(P.ProviderError):
            P.validate_base_url("http://169.254.169.254/latest", allow_private=True)

    def test_private_blocked_by_default(self):
        for url in ("http://192.168.1.5:1234", "http://10.0.0.2/", "http://127.0.0.1:11434"):
            with self.assertRaises(P.ProviderError, msg=url):
                P.validate_base_url(url)

    def test_private_allowed_with_flag(self):
        self.assertEqual(
            P.validate_base_url("http://192.168.1.5:1234/", allow_private=True),
            "http://192.168.1.5:1234")

    def test_public_ok(self):
        self.assertEqual(
            P.validate_base_url("https://api.openai.com/"), "https://api.openai.com")

    def test_creds_in_url_rejected(self):
        with self.assertRaises(P.ProviderError):
            P.validate_base_url("https://user:pass@api.openai.com")

    def test_non_http_rejected(self):
        with self.assertRaises(P.ProviderError):
            P.validate_base_url("ftp://example.com")

    def test_header_validation(self):
        with self.assertRaises(P.ProviderError):
            P.validate_extra_headers({"Authorization": "x"})
        with self.assertRaises(P.ProviderError):
            P.validate_extra_headers({"X-Ok\n": "x"})
        self.assertEqual(P.validate_extra_headers({"HTTP-Referer": "https://a.b"}),
                         {"HTTP-Referer": "https://a.b"})


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

class TestSecrets(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(P.mask_secret("sk-abcdefghijklmnop"), "sk-…mnop")
        self.assertEqual(P.mask_secret(""), "")
        self.assertEqual(P.mask_secret("short"), "••••")

    def test_redact(self):
        self.assertEqual(
            P.redact_secrets("key=sk-abcdefghijklmnop end", ["sk-abcdefghijklmnop"]),
            "key=[redacted] end")

    def test_store_masks(self):
        s = _store()
        row = s.create({"name": "s", "adapter": "openai_compat",
                        "base_url": "https://api.openai.com", "model": "m",
                        "api_key": "sk-abcdefghijklmnop"})
        pub = s.get(row["id"])
        self.assertNotIn("sk-abcdefghijklmnop", json.dumps(pub))
        self.assertEqual(pub["api_key_masked"], "sk-…mnop")
        # Env reference shown, never the value.
        row2 = s.create({"name": "s2", "adapter": "openai_compat",
                         "base_url": "https://api.openai.com", "model": "m",
                         "api_key_env": "OPENAI_API_KEY"})
        self.assertEqual(s.get(row2["id"])["api_key_masked"], "env:OPENAI_API_KEY")

    def test_env_wins_over_stored(self):
        s = _store()
        row = s.create({"name": "e", "adapter": "openai_compat",
                        "base_url": "https://api.openai.com", "model": "m",
                        "api_key": "sk-stored", "api_key_env": "TEST_NC_KEY"})
        with mock.patch.dict(os.environ, {"TEST_NC_KEY": "sk-from-env"}):
            key, _ = P._resolve_key(s.get(row["id"], include_secret=True))
        self.assertEqual(key, "sk-from-env")


# ---------------------------------------------------------------------------
# ProviderStore CRUD + routing
# ---------------------------------------------------------------------------

class TestProviderStore(unittest.TestCase):
    def test_crud(self):
        s = _store()
        row = s.create({"name": "a", "adapter": "ollama",
                        "base_url": "http://127.0.0.1:11434",
                        "model": "llama3", "allow_private": True})
        self.assertEqual(row["name"], "a")
        self.assertEqual(s.get_setting("default_id"), row["id"])  # first = default
        upd = s.update(row["id"], {"model": "llama3.1"})
        self.assertEqual(upd["model"], "llama3.1")
        # Secret untouched when omitted from update.
        row2 = s.create({"name": "b", "adapter": "openai_compat",
                         "base_url": "https://api.openai.com", "model": "m",
                         "api_key": "sk-keepme"})
        s.update(row2["id"], {"model": "m2"})
        full = s.get(row2["id"], include_secret=True)
        self.assertEqual(full["api_key"], "sk-keepme")
        s.delete(row2["id"])
        self.assertIsNone(s.get(row2["id"]))

    def test_duplicate_name_rejected(self):
        s = _store()
        s.create({"name": "dup", "adapter": "ollama", "model": "m",
                  "allow_private": True})
        with self.assertRaises(P.ProviderError):
            s.create({"name": "dup", "adapter": "ollama", "model": "m2",
                      "allow_private": True})

    def test_cannot_delete_default(self):
        s = _store()
        row = s.create({"name": "only", "adapter": "ollama", "model": "m",
                        "allow_private": True})
        with self.assertRaises(P.ProviderError):
            s.delete(row["id"])

    def test_routing(self):
        s = _store()
        a = s.create({"name": "a", "adapter": "ollama", "model": "m",
                      "allow_private": True})
        b = s.create({"name": "b", "adapter": "ollama", "model": "m2",
                      "allow_private": True})
        s.set_default(b["id"])
        self.assertEqual(s.get_setting("default_id"), b["id"])
        s.set_fallbacks([a["id"]])
        self.assertEqual(s.get_fallbacks(), [a["id"]])
        with self.assertRaises(P.ProviderError):  # default can't be fallback
            s.set_fallbacks([b["id"]])
        with self.assertRaises(P.ProviderError):  # unknown id
            s.set_fallbacks(["pc_nope"])

    def test_resolve_chain_order(self):
        s = _store()
        a = s.create({"name": "a", "adapter": "ollama",
                      "base_url": "http://127.0.0.1:11434", "model": "m",
                      "allow_private": True})
        b = s.create({"name": "b", "adapter": "ollama",
                      "base_url": "http://127.0.0.1:11434", "model": "m2",
                      "allow_private": True})
        s.set_default(a["id"])
        s.set_fallbacks([b["id"]])
        chain = P.resolve_chain(s)
        self.assertEqual([pid for pid, _ in chain], [a["id"], b["id"]])

    def test_seed_from_legacy(self):
        d = tempfile.mkdtemp()
        s = P.ProviderStore(d)
        cfg = load_config(overrides={"data_dir": d})
        cfg.llm_provider = "gemini"
        with mock.patch.dict(os.environ, {"NEXTCLAW_GEMINI_API_KEY": "k"}):
            P.seed_from_legacy_config(s, cfg)
        rows = s.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["adapter"], "gemini")
        # Idempotent.
        P.seed_from_legacy_config(s, cfg)
        self.assertEqual(len(s.list()), 1)


# ---------------------------------------------------------------------------
# Fallback chains in the agent loop
# ---------------------------------------------------------------------------

class FakeChainProvider:
    def __init__(self, script, max_retries=0):
        self.script = list(script)
        self.calls = 0
        self.max_retries = max_retries
        self.compat_json_tools = False

    def capabilities(self):
        return P.Capabilities(tools=True)

    def chat(self, system, history, declarations, timeout_s=60):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(text="done"):
    return llm_mod.LLMResponse(text=text)


class TestFallbacks(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = load_config(overrides={"data_dir": self.d})
        self.budget = llm_mod.RequestBudget(self.d)

    def _run(self, chain):
        from nextclaw import agent_llm
        with mock.patch("time.sleep", lambda s: None):
            return agent_llm._chat_with_fallbacks(
                chain, "sys", [], [], self.cfg, self.budget)

    def test_primary_success_no_fallback(self):
        p1 = FakeChainProvider([_ok("one")])
        p2 = FakeChainProvider([_ok("two")])
        resp, pid = self._run([("p1", p1), ("p2", p2)])
        self.assertEqual(resp.text, "one")
        self.assertEqual(p2.calls, 0)

    def test_transient_falls_to_next(self):
        p1 = FakeChainProvider([P.TransientError("boom")])
        p2 = FakeChainProvider([_ok("two")])
        resp, pid = self._run([("p1", p1), ("p2", p2)])
        self.assertEqual(resp.text, "two")
        self.assertEqual(pid, "p2")

    def test_auth_error_not_retried_on_same_provider(self):
        p1 = FakeChainProvider([P.AuthError("bad key"), _ok("should not happen")],
                               max_retries=3)
        p2 = FakeChainProvider([_ok("two")])
        resp, _ = self._run([("p1", p1), ("p2", p2)])
        self.assertEqual(p1.calls, 1)  # no retry loop on auth failure
        self.assertEqual(resp.text, "two")

    def test_ratelimited_retries_then_falls_back(self):
        p1 = FakeChainProvider([P.RateLimited("slow"), P.RateLimited("slow")],
                               max_retries=1)
        p2 = FakeChainProvider([_ok("two")])
        resp, _ = self._run([("p1", p1), ("p2", p2)])
        self.assertEqual(p1.calls, 2)  # 1 retry, then fallback
        self.assertEqual(resp.text, "two")

    def test_all_quota_exhausted_records_cooldown(self):
        p1 = FakeChainProvider([P.QuotaExhausted("empty")])
        with self.assertRaises(P.QuotaExhausted):
            self._run([("p1", p1)])
        st = self.budget.status(self.cfg)
        self.assertTrue(st["cooldown_active"])

    def test_quota_falls_back_without_global_cooldown(self):
        p1 = FakeChainProvider([P.QuotaExhausted("empty")])
        p2 = FakeChainProvider([_ok("two")])
        resp, _ = self._run([("p1", p1), ("p2", p2)])
        self.assertEqual(resp.text, "two")
        # No global cooldown: a fresh check passes.
        self.budget.check(self.cfg)

    def test_all_fail_reports_every_provider(self):
        p1 = FakeChainProvider([P.AuthError("bad1")])
        p2 = FakeChainProvider([P.InvalidModelError("nope")])
        with self.assertRaises(P.ProviderError) as ctx:
            self._run([("p1", p1), ("p2", p2)])
        self.assertIn("p1", str(ctx.exception))
        self.assertIn("p2", str(ctx.exception))


# ---------------------------------------------------------------------------
# Tool-call guards + compat mode
# ---------------------------------------------------------------------------

class TestToolGuards(unittest.TestCase):
    def test_unknown_tool_never_executes(self):
        from nextclaw import agent_llm
        d = tempfile.mkdtemp()
        cfg = load_config(overrides={"data_dir": d, "workspace_root": d})

        class Evil(llm_mod.LLMProvider):
            def __init__(self):
                self.n = 0

            def chat(self, system, history, declarations, timeout_s=60):
                self.n += 1
                if self.n == 1:
                    return llm_mod.LLMResponse(
                        text="",
                        tool_calls=[llm_mod.ToolCall(id="c1", name="evil.tool",
                                                     args={})])
                return llm_mod.LLMResponse(text="done")

        tid = agent_llm.create_agent_task("do nothing", cfg)
        task = agent_llm.run_llm_task(tid, cfg, provider=Evil())
        # The model was told the tool is unknown; nothing executed.
        from nextclaw.tasks import TaskStore
        turns = TaskStore(d).get_turns(tid)
        tool_turns = [t for t in turns if t["role"] == "tool"]
        self.assertTrue(tool_turns)
        self.assertIn("unknown tool", json.dumps(tool_turns[0]["output"]))
        self.assertEqual(task["status"], "completed")  # model stopped, no crash

    def test_compat_json_tools_parsed(self):
        from nextclaw import agent_llm

        class NoTools:
            compat_json_tools = True
            max_retries = 0

            def capabilities(self):
                return P.Capabilities(tools=False)

            def chat(self, system, history, declarations, timeout_s=60):
                self.seen_system = system
                return llm_mod.LLMResponse(
                    text='Let me list files.\n<tool_calls>[{"name": "fs.list", '
                         '"args": {"path": ".", "pattern": "*"}}]</tool_calls>')

        d = tempfile.mkdtemp()
        cfg = load_config(overrides={"data_dir": d})
        budget = llm_mod.RequestBudget(d)
        prov = NoTools()
        resp, _ = agent_llm._chat_with_fallbacks(
            [("p", prov)], "sys", [], [{"name": "fs.list"}], cfg, budget)
        self.assertIn("COMPATIBILITY MODE", prov.seen_system)
        self.assertEqual(len(resp.tool_calls), 1)
        self.assertEqual(resp.tool_calls[0].name, "fs.list")

    def test_compat_off_by_default(self):
        from nextclaw import agent_llm
        d = tempfile.mkdtemp()
        cfg = load_config(overrides={"data_dir": d})
        budget = llm_mod.RequestBudget(d)

        class NoTools:
            max_retries = 0

            def capabilities(self):
                return P.Capabilities(tools=False)

            def chat(self, system, history, declarations, timeout_s=60):
                return llm_mod.LLMResponse(text="no tools here")

        resp, _ = agent_llm._chat_with_fallbacks(
            [("p", NoTools())], "sys", [], [{"name": "fs.list"}], cfg, budget)
        self.assertEqual(resp.tool_calls, [])


if __name__ == "__main__":
    unittest.main()
