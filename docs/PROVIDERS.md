# AI Providers & Models

NextClaw is provider-agnostic. The agent loop talks to a normalized
`ChatProvider` interface; vendor specifics live in small adapters in
`nextclaw/providers.py`. Switching models never requires code changes —
configure providers in the dashboard (Providers page), the API, or the CLI.

## Why not LiteLLM?

LiteLLM was evaluated as the integration layer and deliberately **not**
adopted:

1. **Dependency weight.** LiteLLM pulls ~30 transitive dependencies
   (pydantic, httpx, tiktoken, tokenizers, jinja2, …). NextClaw is
   zero-dependency by design; adopting LiteLLM would trade that away.
2. **It doesn't remove our layer.** LiteLLM is a completion API. We would
   still need our own tool-call loop, history normalization, quota/budget
   protection, provider config store, and dashboard — the adapter *underneath*
   would just be someone else's code to debug through.
3. **The landscape collapses anyway.** ~15 cloud vendors and all major local
   servers speak OpenAI-compatible `/v1/chat/completions`. Only three
   protocols need native adapters (Anthropic, Gemini, Bedrock).

So: five small stdlib adapters instead of a heavy dependency. Every adapter
is unit-tested against its wire format with mocked HTTP.

## Adapters

| Adapter | Protocol | Covers |
|---|---|---|
| `openai_compat` | `POST {base}/v1/chat/completions` (+ SSE streaming) | OpenAI, Azure OpenAI, Groq, OpenRouter, DeepSeek, Mistral, xAI, Together, Fireworks, Cerebras, GitHub Models, Hugging Face router, LM Studio, llama.cpp server, LocalAI, vLLM, any custom endpoint |
| `anthropic` | `POST {base}/v1/messages` | Anthropic Claude (native tool_use) |
| `gemini` | `POST …/v1beta/models/…:generateContent` | Google Gemini / AI Studio (native function calling) |
| `ollama` | `POST {base}/api/chat` | Ollama (native tools) |
| `bedrock` | `POST bedrock-runtime.{region}…/model/…/converse` + SigV4 | AWS Bedrock (stdlib HMAC signing) |

A preset appearing in the dashboard means "we know how to talk to it and the
wire format is tested" — not "we verified it live". Live verification is
always your own **Test** button.

## Adding a provider

**Dashboard:** Providers → Add provider → pick a preset (fills adapter,
endpoint, env var) → enter model → paste key *or* set "Key from env var" →
Save → Test → Test prompt.

**CLI:**
```bash
python3 -m nextclaw.cli providers presets     # list presets
python3 -m nextclaw.cli providers list        # list configured
python3 -m nextclaw.cli providers set-default --name my-openai
python3 -m nextclaw.cli providers test --name my-openai
```

**API:** `POST /api/providers/configs` with the JSON fields below.

### Configuration fields

| Field | Meaning |
|---|---|
| `name` | Unique label (letters, digits, space, `_`, `-`) |
| `adapter` | One of the five adapters |
| `base_url` | Endpoint root (validated; see SSRF guard) |
| `model` | Model/deployment id |
| `api_key` | Stored server-side (0600 file). Omitted on update = keep existing |
| `api_key_env` | Env var name (e.g. `OPENAI_API_KEY`). **Preferred** — wins over stored key, never touches disk |
| `aws_secret_key` | Bedrock only (or `AWS_SECRET_ACCESS_KEY` env) |
| `extra_headers` | JSON object, e.g. `{"HTTP-Referer": "https://myapp.com"}` for OpenRouter |
| `extra_params` | JSON object, e.g. `{"api-version": "2024-10-21", "region": "us-east-1"}` |
| `timeout_s` / `max_retries` | Per-provider limits |
| `allow_private` | Allow private/loopback base URLs (LAN inference) |
| `compat_json_tools` | Experimental JSON tool-compat mode (see below) |
| `enabled` | Disabled providers are skipped by routing |

## Connecting local models

**Ollama:** preset fills `http://127.0.0.1:11434`. No key needed. Use
**Discover** (or `GET …/models`) to list installed models, pick one, save.

**LM Studio / llama.cpp / LocalAI / vLLM:** presets point at their default
OpenAI-compatible ports. In LM Studio, enable the OpenAI-compatible server
first; for llama.cpp, start with `--chat-completion`.

**Network namespaces:** "localhost" means different things to the browser,
the backend, a container, and a remote server. If the dashboard runs in your
browser but the backend runs elsewhere, `127.0.0.1` in a provider URL is the
*backend's* loopback. Point it at the backend-reachable address instead.

**Do not expose a private inference server to the internet** without
authentication in front of it — anyone with the URL can run inference on
your hardware.

NextClaw is fully useful with only a local model configured and no cloud
keys at all.

## Custom endpoints

Choose the "Custom OpenAI-compatible endpoint" preset, set `base_url` to the
server root (the adapter appends `/v1/chat/completions`), plus model and key.
Any server implementing the OpenAI chat-completions schema works, including
self-hosted gateways.

## Tool calling

The agent sends tool definitions on every turn for providers whose
capabilities report `tools: true`. Returned calls are validated:

1. The tool name must be one we declared — unknown names are **never
   executed**; the model gets an error it can correct.
2. Arguments must be a JSON object.
3. Execution goes through the same permission policy as always
   (allowlist, workspace confinement, denylist, human approvals).

If a model has no native tool calling, it fails closed with a clear error
unless you explicitly enable **JSON tool-compat mode** on its config. That
mode is experimental: the model is instructed to emit
`<tool_calls>[…]</tool_calls>` JSON, which we parse and validate like native
calls. Prefer a tool-capable model.

## Fallbacks & retries

Routing = one **default** + an ordered **fallback** list (dashboard Routing
card or `POST /api/providers/routing`).

- **Transient** (5xx, timeouts, connection errors): retried with exponential
  backoff (honors `Retry-After`), then the next fallback is tried.
- **RateLimited**: same, per provider.
- **QuotaExhausted**: never retried on the same provider; the next fallback
  is tried immediately. The shared cooldown is recorded only when the whole
  chain is exhausted.
- **AuthError / InvalidModelError**: never retried on the same provider;
  the next fallback gets one attempt (different credentials may work).
- Permanent failures across the whole chain fail the task with every
  provider's error listed — no silent behavior.

Fallbacks are user-configured, so using them is consented routing. Nothing
ever silently jumps to a provider you didn't configure.

## Credentials & security

- Preferred: `api_key_env` — the key lives only in the process environment.
- Stored keys: SQLite `providers.db`, file mode `0600`, never returned in
  full by any API (masked as `sk-…abcd`), redacted from logs and errors.
- The dashboard never receives full keys; the key field is a password input
  and blank means "keep existing".
- No `.env` files are committed; `git status` stays clean of secrets.
- **SSRF guard** on `base_url`: cloud metadata endpoints
  (`169.254.169.254`, `100.100.100.200`) are always blocked; private/loopback
  IPs require explicit `allow_private`. Hostname-based check only — DNS
  rebinding is a documented limitation.
- Custom headers are allowlisted by name; `Authorization`, `host`,
  `content-length`, and signing headers cannot be overridden.

## Capability reporting

`GET /api/providers/configs` includes per-adapter capabilities
(`tools`, `streaming`, `vision`, `json_mode`) shown as badges in the
dashboard. These describe the *adapter*, not a live probe of the model —
a local model behind the Ollama adapter may or may not honor tools.
`GET /api/status` also reports the Chromium render probe for browser
automation (see LIMITATIONS.md).

## Migrating from 0.2.x

Existing installs keep working with zero action: on first start, the old
`llm_provider`/`llm_model`/`NEXTCLAW_GEMINI_API_KEY`/`ollama_*` settings are
migrated into provider configs ("gemini", "ollama-local") and the previous
default is preserved. `NEXTCLAW_OFFLINE=1` still disables all model calls.

## Known limitations

- Bedrock model discovery isn't supported (region-scoped); enter the model id.
- Azure OpenAI: set `base_url` to your resource root and `model` to the
  **deployment name**; add `{"api-version": "2024-10-21"}` to extra params.
- Streaming is implemented for `openai_compat` (tested); the agent loop uses
  non-streaming turns by design (checkpointed, resumable).
- Vision/file inputs are not wired through the agent loop yet, even where the
  adapter supports them.
- Live provider verification requires your own keys or a reachable local
  server; CI runs mocked tests only.
