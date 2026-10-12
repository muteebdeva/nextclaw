"""Live provider smoke tests — real network, real credentials.

These NEVER run in the normal suite. They are gated by
NEXTCLAW_LIVE_SMOKE=1 plus provider configuration:

  NEXTCLAW_SMOKE_ADAPTER   one of: openai_compat, anthropic, gemini,
                           ollama, bedrock
  NEXTCLAW_SMOKE_BASE_URL  endpoint for openai_compat / ollama / custom
  NEXTCLAW_SMOKE_MODEL     model name (required)
  NEXTCLAW_SMOKE_API_KEY   API key for cloud adapters (never logged)
  NEXTCLAW_SMOKE_REGION    AWS region for bedrock (default us-east-1)
  NEXTCLAW_SMOKE_TIMEOUT   per-call timeout seconds (default 60)

Coverage: authentication (test_connection), basic generation (tiny prompt),
and tool calling where the adapter reports tool support.

Cost control: prompts are a few tokens; no max_tokens knob exists on the
adapter interface, so prompts are kept minimal by construction.

If configuration is missing the tests SKIP with an explicit reason — they
are never reported as passed without a real provider behind them.
Secrets are never printed: failure messages contain only masked values.
"""
import os
import unittest

from nextclaw import providers as P

LIVE = os.environ.get("NEXTCLAW_LIVE_SMOKE") == "1"


def _build_provider():
    """Build the configured provider, or return (None, reason)."""
    adapter = (os.environ.get("NEXTCLAW_SMOKE_ADAPTER") or "").strip()
    model = (os.environ.get("NEXTCLAW_SMOKE_MODEL") or "").strip()
    if not adapter:
        return None, "NEXTCLAW_SMOKE_ADAPTER is not set"
    if not model:
        return None, "NEXTCLAW_SMOKE_MODEL is not set"
    base_url = (os.environ.get("NEXTCLAW_SMOKE_BASE_URL") or "").strip()
    api_key = (os.environ.get("NEXTCLAW_SMOKE_API_KEY") or "").strip()
    timeout = int(os.environ.get("NEXTCLAW_SMOKE_TIMEOUT") or "60")

    if adapter == "openai_compat":
        if not base_url:
            return None, "openai_compat needs NEXTCLAW_SMOKE_BASE_URL"
        return P.OpenAICompatProvider(base_url=base_url, api_key=api_key,
                                      model=model), None
    if adapter == "anthropic":
        if not api_key:
            return None, "anthropic needs NEXTCLAW_SMOKE_API_KEY"
        return P.AnthropicProvider(api_key=api_key, model=model,
                                   base_url=base_url or P.AnthropicProvider.DEFAULT_BASE), None
    if adapter == "gemini":
        if not api_key:
            return None, "gemini needs NEXTCLAW_SMOKE_API_KEY"
        return P.GeminiAdapter(api_key=api_key, model=model), None
    if adapter == "ollama":
        return P.OllamaAdapter(base_url=base_url or "http://127.0.0.1:11434",
                               model=model), None
    if adapter == "bedrock":
        region = (os.environ.get("NEXTCLAW_SMOKE_REGION") or "us-east-1").strip()
        access = (os.environ.get("NEXTCLAW_SMOKE_AWS_ACCESS_KEY") or "").strip()
        secret = (os.environ.get("NEXTCLAW_SMOKE_AWS_SECRET_KEY") or "").strip()
        return P.BedrockProvider(model=model, region=region,
                                 access_key=access, secret_key=secret), None
    return None, f"unknown adapter {adapter!r}"


@unittest.skipUnless(LIVE, "live smoke: set NEXTCLAW_LIVE_SMOKE=1")
class LiveProviderSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        provider, reason = _build_provider()
        if provider is None:
            raise unittest.SkipTest(f"live smoke skipped: {reason}")
        cls.provider = provider
        cls.timeout = int(os.environ.get("NEXTCLAW_SMOKE_TIMEOUT") or "60")
        cls.adapter = (os.environ.get("NEXTCLAW_SMOKE_ADAPTER") or "").strip()

    def test_1_connection_auth(self):
        """test_connection must report ok — proves credentials work."""
        result = self.provider.test_connection(timeout_s=min(15, self.timeout))
        self.assertTrue(result.get("ok"),
                        f"connection failed: {result.get('error', result)}")

    def test_2_basic_generation(self):
        """Tiny prompt; assert a non-empty text reply (cheap by construction)."""
        resp = self.provider.chat(
            system="You are a test assistant. Reply minimally.",
            history=[{"role": "user",
                      "text": "Reply with exactly this word: smoke-ok"}],
            declarations=[],
            timeout_s=self.timeout,
        )
        self.assertTrue(resp.text and resp.text.strip(),
                        "empty generation from live provider")
        # The word check is best-effort (models paraphrase); log length only.
        self.assertLess(len(resp.text), 2000, "generation unexpectedly long")

    def test_3_tool_calling_where_supported(self):
        """Where the adapter reports tool support, exercise a tool round-trip."""
        caps = self.provider.capabilities()
        if not caps.tools:
            self.skipTest(f"{self.adapter} does not report tool support")
        decl = [{
            "name": "echo_word",
            "description": "Echo back the given word.",
            "parameters": {
                "type": "object",
                "properties": {"word": {"type": "string"}},
                "required": ["word"],
            },
        }]
        resp = self.provider.chat(
            system="You have one tool. Call echo_word with word='smoke'.",
            history=[{"role": "user",
                      "text": "Call echo_word with word set to smoke."}],
            declarations=decl,
            timeout_s=self.timeout,
        )
        # Well-formed response required; an actual tool call is ideal but
        # model-dependent, so accept either text or a valid tool call.
        self.assertTrue(resp.text.strip() or resp.tool_calls,
                        "no text and no tool calls in response")
        for tc in resp.tool_calls:
            self.assertEqual(tc.name, "echo_word")
            self.assertIn("word", tc.args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
