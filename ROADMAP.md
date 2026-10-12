# Roadmap

- [x] MVP 0.1.0: agent runtime, tool registry + permissions, SQLite task queue,
      memory, model router, sandboxed exec, CLI, benchmarks, docs
- [x] Milestone 1 (real LLM): `GeminiProvider` (function calling verified live)
      + `OllamaProvider` (implemented, untested - no local capacity here);
      `run-agent` tool-calling loop with persistent turns, resume, bounded
      turns/retries/deadlines, verifier short-circuit, approvals
- [ ] Clean real-model end-to-end completion (blocked 2026-10-12: free-tier
      429 quota exhausted; loop, resume, and verification already exercised
      live - see cycle report)
- [x] Scheduler (milestone 5): persistent 40-min dev-cycle job, atomic
      overlap prevention, stale-run recovery, catch-up + hourly fallback,
      per-run timeouts, daemon start/stop/status/run-now - all tested
- [x] Quota safeguards (milestone 3): QuotaExhausted vs transient, Retry-After,
      daily + per-task budgets, post-exhaustion cooldown, offline mode;
      live tests env-gated and separate from mocked tests
- [ ] Harden `exec.run` policy with more denylist cases + tests
- [ ] Scheduler (cron-like) with persistent due tasks
- [ ] Event triggers (file watcher) with persistent cursor
- [ ] Multi-agent delegation (parent/child tasks)
- [x] Web dashboard with real task management, execution visibility, provider
      status, and user controls (`nextclaw serve` — stdlib API + vanilla JS SPA,
      Bearer auth, SSE live updates)
- [x] Human approval queue + audit log (dashboard/API/CLI; gate blocks agent
      until decided; denials fail closed)
- [x] Task cancellation (cooperative, checkpoint-safe) via dashboard/API/CLI
- [x] Telegram channel behind explicit allowlist + approvals
      (`nextclaw channels telegram`; mock-tested, opt-in)
- [x] Browser research tool (`browser.fetch`: read-only, SSRF-guarded,
      tested). Interactive browsing (click/forms/screenshots) still needs a
      working headless Chromium — not available in this environment.
- [x] Deterministic task types beyond file_report: `file_write`,
      `browser_research` (explicit plans + verifiers)
- [ ] Cost/token accounting per task and provider
- [ ] Head-to-head benchmark harness vs OpenClaw on equivalent tasks
