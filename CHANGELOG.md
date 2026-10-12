# Changelog

All notable changes to NextClaw. Dates are in IST (Asia/Kolkata).

## Unreleased — universal provider system

- **Universal AI provider system** (`nextclaw/providers.py`): modular,
  provider-agnostic model layer. Five stdlib adapters — `openai_compat`
  (OpenAI, Azure, Groq, OpenRouter, DeepSeek, Mistral, xAI, Together,
  Fireworks, Cerebras, GitHub Models, Hugging Face, LM Studio, llama.cpp,
  LocalAI, vLLM, custom endpoints), `anthropic` (native Messages API),
  `gemini` (native function calling), `ollama` (native), `bedrock`
  (Converse API + SigV4 signing) — behind one normalized interface.
  LiteLLM was evaluated and deliberately not adopted (dependency weight;
  see `docs/PROVIDERS.md`).
- **Provider configuration store**: SQLite (`providers.db`, 0600) with
  add/edit/test/enable/disable/remove; secrets via env-var reference
  (preferred) or stored masked; SSRF guard on custom URLs; masked secrets
  in every API response; redaction in logs.
- **Dashboard Providers page rewritten**: provider cards with capability
  badges, test-connectivity, test-prompt with live results, model
  discovery, default + ordered fallbacks UI, full CRUD form with 21 presets.
- **Agent decoupled from vendors**: `run-agent` resolves a default +
  fallback chain; transient errors retry with backoff, quota exhaustion
  never retries the same provider, auth errors never loop. Tool calls are
  validated against declared tools (unknown tools never execute).
  Experimental opt-in JSON tool-compat mode for models without native
  calling, explicitly labeled.
- **CLI**: `nextclaw providers list|test|set-default|presets`.
- **Chromium audit**: `browsertools.check_chromium()` CDP render probe +
  `/api/status` reporting. Corrected 2026-10-12: the binary renders fine
  via the DevTools protocol (navigate + evaluate + screenshot verified);
  only the `--dump-dom`/`--screenshot` CLI flags are dead in this build.
- **Docs**: new `docs/PROVIDERS.md` (setup, local models, custom
  endpoints, fallbacks, security, migration); README/LIMITATIONS updated.
- **Tests**: 57 new (57 provider + API), 142 total, all offline/mocked.

## Unreleased — dashboard milestone

- **Web dashboard** (`nextclaw serve`): dependency-free SPA + stdlib JSON API
  with Bearer-token auth, live task updates over SSE, and per-IP rate limits.
  Pages: Overview, Tasks (+ detail timeline, tool-call inspection, verification
  evidence), Approvals, Scheduler, Providers, Settings. Dark/light themes.
- **Human approval gate** (`nextclaw/approvals.py`): SQLite approval queue;
  the agent blocks until a human approves/rejects via dashboard, API, or CLI;
  every decision is audit-logged. Denials and timeouts fail closed.
- **Task cancellation**: cooperative cancel between steps/turns via
  dashboard, API, or `nextclaw cancel`; pending approvals are denied on cancel.
- **New deterministic task types**: `file_write` (sha256-verified) and
  `browser_research` (fetch → report → verified).
- **Browser research** (`browser.fetch`): read-only page fetch with SSRF
  protection (blocks private/loopback/link-local/metadata IPs, validates
  redirect targets), timeouts, and size caps. Tested against a local server.
- **Telegram channel** (`nextclaw channels telegram`): opt-in, allowlisted
  users only, persistent update offsets, mock-tested. Never auto-approves.
- **Scheduler in dashboard/API**: jobs, run history, enable/disable/run-now.
- **API additions**: health, status, tasks CRUD, approvals, policy, providers
  (+ connectivity test, safe config updates), quota, audit, memory, SSE.
- `run-agent`/`run` now fail gracefully (no traceback) on unconfigured
  providers; `OllamaProvider` reads `ollama_url`/`ollama_model` from config.
- Test suite: 85 tests (was 45), all offline; pyflakes clean.
- Docs: `LIMITATIONS.md` (honest capability inventory), `docs/DEPLOYMENT.md`
  (safe exposure checklist), README/ROADMAP updated.

## Unreleased

- Repository launch polish: redesigned README, SVG visual identity
  (`assets/`), `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`,
  issue/PR templates, CI workflow, `.env.example`, `pyproject.toml`.
- `run-agent` now fails a task with a clear message (instead of a traceback)
  when the model provider is unconfigured.

## 2026-10-12 — Milestones 3 & 5

- **Scheduler**: persistent 40-minute autonomous dev-cycle scheduler
  (`nextclaw/scheduler.py`) — SQLite jobs/runs, atomic overlap prevention,
  stale-run recovery, missed-schedule catch-up with hourly fallback,
  per-run timeouts, daemon `start`/`stop`/`status`/`run-now`.
- **Quota safeguards** (`nextclaw/llm.py`, `agent_llm.py`): `QuotaExhausted`
  vs transient errors, `Retry-After` support, daily + per-task request
  budgets, post-exhaustion cooldown, `NEXTCLAW_OFFLINE=1` mode.
- Live-model tests moved to `tests/test_llm_live.py` (env-gated, skipped
  by default). Suite: 44 tests passing.

## 2026-10-12 — Milestone 1: real LLM integration

- `GeminiProvider` with function calling (verified live against
  `gemini-3.7-flash`); `OllamaProvider` implemented for local models.
- `run-agent` tool-calling loop (`nextclaw/agent_llm.py`): model plans,
  calls tools, inspects outputs; turns checkpointed to SQLite; resume
  continues the persisted conversation; loop stops on verifier `ok:true`.
- Fixed via live testing: Gemini 3.x `thoughtSignature` round-trip;
  model second-guessing verified results.

## 2026-10-12 — MVP 0.1.0

- Initial working agent: deterministic planner, 7 permission-checked tools,
  SQLite task queue with crash recovery, structured memory, CLI, benchmark
  suite, and docs. 14 tests passing, zero dependencies.
