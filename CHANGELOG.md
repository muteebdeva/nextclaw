# Changelog

All notable changes to NextClaw. Dates are in IST (Asia/Kolkata).

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
