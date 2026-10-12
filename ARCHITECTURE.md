# NextClaw architecture (MVP)

```
                ┌─────────────┐
                │     CLI     │  init/run/resume/status/memory/bench/cycle
                └──────┬──────┘
                       ▼
                ┌─────────────┐
                │    Agent    │  plan -> execute -> verify -> checkpoint
                └──────┬──────┘
           ┌───────────┼───────────┐
           ▼           ▼           ▼
     ┌──────────┐ ┌────────┐ ┌──────────┐
     │  Models  │ │ Tools  │ │  Memory  │
     │ (router) │ │(registry│ │ (SQLite) │
     └──────────┘ │+policy) │ └──────────┘
                  └────┬────┘
                       ▼
                ┌─────────────┐
                │    Tasks    │  SQLite queue + step state machine
                └─────────────┘
```

## Modules (`nextclaw/`)

- `config.py` - zero-dependency config; env overrides (`NEXTCLAW_DATA_DIR`,
  `NEXTCLAW_WORKSPACE`, `NEXTCLAW_MODEL_PROVIDER`).
- `models.py` - provider abstraction + router. `StubProvider` generates explicit
  plans for supported task types. Unconfigured providers raise `NotConfigured`
  instead of hallucinating.
- `tools.py` - tool registry, permission policy, secret redaction, sandboxed
  exec. Filesystem tools resolve inside `workspace_root`; `exec.run` uses
  `shell=False`, a destructive-command denylist, and requires approval.
- `tasks.py` - persistent task queue. Steps are checkpointed after every tool
  call; `reset_running_steps()` makes crash recovery safe.
- `memory.py` - structured memory with text/tags/source/timestamps and
  keyword recall.
- `agent.py` - deterministic planner invocation, permission-checked execution,
  per-step verifiers, bounded retries, evidence building, completion memory.
- `agent_llm.py` - live-model tool-calling loop: the model plans in its own
  words, calls tools, inspects real outputs. Turns + tool calls checkpointed
  to the `turns` table; resume continues the persisted conversation.
  Stops on the task's explicit verifier (`ok:true`) to prevent second-guessing.
  Resolves a default + fallback provider chain per task; unknown tool names
  are never executed; experimental JSON tool-compat mode is opt-in only.
- `providers.py` - universal provider system: five stdlib adapters
  (`openai_compat`, `anthropic`, `gemini`, `ollama`, `bedrock` w/ SigV4)
  behind one normalized `ChatProvider` interface; `ProviderStore` (SQLite)
  for user configs with masked secrets, env-var key references, SSRF-guard
  URL validation, and default/fallback routing. Legacy `llm_*` settings
  auto-migrate on first run.
- `llm.py` - shared error taxonomy (`AuthError`, `TransientError`,
  `QuotaExhausted`, …), native Gemini/Ollama implementations (wrapped as
  adapters), and `RequestBudget` (daily cap + quota cooldown).
  429-with-quota raises `QuotaExhausted` (never retried); transient 429/503
  retried with backoff honoring `Retry-After`. `RequestBudget` enforces a
  daily cap and a post-exhaustion cooldown that fails fast without HTTP.
- `scheduler.py` - persistent 40-minute dev-cycle scheduler: SQLite jobs +
  runs, atomic claim (no overlaps), stale-run recovery, missed-schedule
  catch-up with hourly fallback, per-run subprocess timeouts, pidfile
  daemon with start/stop/status/run-now.
- `bench.py` - reproducible benchmark suite (runs the real code).
- `cycle.py` - autonomous dev cycle: runs tests, records state, picks next
  roadmap item.
- `approvals.py` - human approval gate: SQLite approval queue + audit log.
  `ApprovalGate` blocks the agent until a human approves/rejects (dashboard,
  API, or CLI); denials and timeouts fail closed. Pre-approved tools via
  `preapproved_approver` (CLI `--approve`).
- `server.py` - dashboard API + static file server (stdlib only):
  Bearer-token auth, per-IP rate limits, background task runner
  (ThreadPoolExecutor), SSE task-event streams, endpoints for tasks,
  approvals, policy, providers, scheduler, quota, audit, memory.
- `browsertools.py` - read-only web research: SSRF-guarded fetch (blocks
  private/loopback/link-local/metadata IPs, validates redirect targets),
  HTML text/link extraction. No JS execution.
- `channels.py` - Telegram channel (opt-in): long-polling client with
  injectable transport, allowlisted users, persistent update offsets,
  `/task` `/status` `/tasks` `/cancel` commands. Never auto-approves.
- `cli.py` - user interface (`serve`, `cancel`, `channels telegram` added).
- `web/` - dependency-free dashboard SPA (dark/light themes).

## Data flow (file_report)

1. `create_task(goal, "file_report", params)` -> provider returns 4 steps.
2. Steps persist to `tasks.db` as `pending`.
3. `run_task` executes each step: permission check -> tool -> verifier ->
   checkpoint (`done`). Template `{{file_report}}` is rendered from prior
   step outputs before `fs.write`.
4. `verify.file_report` recomputes measurements and compares against the
   report's claimed totals.
5. Evidence (`sha256`, counts) is stored on the task; completion is written
   to memory with provenance `nextclaw-agent`.

## Failure handling

- Tool/policy errors: step retries up to `max_step_attempts`, then the task
  fails with a diagnosis (no infinite loops).
- Approval needed: task pauses as `awaiting_approval`; resume with
  `--approve <tool>`.
- Crash: steps already `done` are skipped on resume; orphaned `running`
  steps reset to `pending`.
