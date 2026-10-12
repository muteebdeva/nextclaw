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
- `llm.py` - real provider abstraction (`GeminiProvider` with function
  calling incl. thoughtSignature round-trip, `OllamaProvider` for local
  models). Keys only from environment; unconfigured providers fail closed.
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
- `cli.py` - user interface.

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
