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
- [ ] Harden `exec.run` policy with more denylist cases + tests
- [ ] Scheduler (cron-like) with persistent due tasks
- [ ] Event triggers (file watcher) with persistent cursor
- [ ] Multi-agent delegation (parent/child tasks)
- [ ] Web dashboard (read-only task/memory views)
- [ ] Telegram adapter behind explicit pairing + approvals
- [ ] Browser automation tool behind allowlisted domains
- [ ] Cost/token accounting per task and provider
- [ ] Head-to-head benchmark harness vs OpenClaw on equivalent tasks
