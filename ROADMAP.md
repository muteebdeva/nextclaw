# Roadmap

- [x] MVP 0.1.0: agent runtime, tool registry + permissions, SQLite task queue,
      memory, model router, sandboxed exec, CLI, benchmarks, docs
- [ ] Harden `exec.run` policy with more denylist cases + tests
- [ ] Scheduler (cron-like) with persistent due tasks
- [ ] Event triggers (file watcher) with persistent cursor
- [ ] Multi-agent delegation (parent/child tasks)
- [ ] Web dashboard (read-only task/memory views)
- [ ] Telegram adapter behind explicit pairing + approvals
- [ ] Browser automation tool behind allowlisted domains
- [ ] Cost/token accounting per task and provider
- [ ] Head-to-head benchmark harness vs OpenClaw on equivalent tasks
