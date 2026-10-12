# NextClaw (MVP 0.1.0)

An original, open-source AI agent platform focused on **measurable reliability**:
explicit success criteria, persistent resumable tasks, permission-checked tools,
structured memory, and reproducible benchmarks.

> Status: working vertical slice. It accepts a goal, plans explicit steps, runs
> permission-checked tools, verifies each step against real tool output, persists
> state to SQLite, and resumes after an interrupted run. See
> [COMPARISON.md](COMPARISON.md) for an honest comparison and
> [SPEC.md](SPEC.md) for the full product spec.

## Install

Zero dependencies. Python 3.10+ only.

```bash
cd ~/workspace/nextclaw
python3 -m nextclaw.cli init
python3 -m unittest discover -s tests
```

## Quickstart: a verified file report

```bash
python3 -m nextclaw.cli run \
  --goal "Count Python files under nextclaw/nextclaw and write a verified report" \
  --type file_report \
  --param root=nextclaw/nextclaw \
  --param pattern='**/*.py' \
  --param output=reports/mvp-report.md
```

What happens:

1. The planner produces an explicit 4-step plan (collect -> measure -> write -> verify).
2. Each step runs through the permission system (workspace-confined, denylisted
   commands blocked, high-risk tools need `--approve`).
3. Each step is verified against real tool output (not the model's claims).
4. State is checkpointed to SQLite after every step; `resume` continues a
   crashed run without repeating completed steps.
5. The final report is re-verified by recomputation; a SHA-256 and counts are
   returned as evidence, and completion is written to long-term memory.

Other commands:

```bash
python3 -m nextclaw.cli status --task-id <id>
python3 -m nextclaw.cli resume --task-id <id> [--approve exec.run]
python3 -m nextclaw.cli remember --text "prefers terse replies" --tag pref
python3 -m nextclaw.cli recall --query "prefers"
python3 -m nextclaw.cli bench     # reproducible benchmark suite
python3 -m nextclaw.cli cycle     # autonomous dev cycle: runs tests, records state
```

## Real-model agent (`run-agent`)

The deterministic `run` planner is kept, but the platform now also supports
live model reasoning with a tool-calling loop:

```bash
export NEXTCLAW_GEMINI_API_KEY="..."   # never commit this
python3 -m nextclaw.cli run-agent \
  --goal "Count the Python files under src and write a verified report to reports/r.md" \
  --verify-tool verify.file_report \
  --verify-arg report_path=reports/r.md \
  --verify-arg root=src \
  --verify-arg pattern='**/*.py'
```

The model plans in its own words, calls tools, and sees real outputs. Every
turn and tool call is checkpointed to SQLite, so `resume` continues an
interrupted run from the persisted conversation. When the task's verifier
reports `ok:true`, the loop stops on the explicit success criterion instead
of letting the model second-guess a verified result. Unconfigured providers
(Gemini without a key, unreachable Ollama) fail closed with a clear error.

Provider selection: `NEXTCLAW_LLM_PROVIDER` (`gemini` default, `ollama`
supported where local models are feasible), `NEXTCLAW_LLM_MODEL`
(default `gemini-3.7-flash`), `NEXTCLAW_OLLAMA_URL` / `NEXTCLAW_OLLAMA_MODEL`
for local.

### Quota protection

The Gemini key may be shared (e.g. with a Telegram bot), so the agent
protects it aggressively:

- 429s mentioning quota/billing raise `QuotaExhausted`: **no retries**.
  Transient 429/503s are retried with backoff, respecting `Retry-After`.
- After a quota-exhaustion, a cooldown (default 1h, `llm_quota_cooldown_s`)
  fails fast **without any HTTP call**.
- Daily request cap (default 200, `NEXTCLAW_LLM_MAX_PER_DAY`) and per-task
  cap (default 40 model calls).
- `NEXTCLAW_OFFLINE=1` disables all model calls (fail closed).
- Scheduler-driven dev cycles never call the LLM at all.
- Keys live only in environment variables; they are never printed, logged,
  or committed.

## Scheduler: autonomous dev cycles every 40 minutes

```bash
python3 -m nextclaw.cli scheduler ensure   # create the 40-min dev-cycle job
python3 -m nextclaw.cli scheduler start    # start the persistent daemon
python3 -m nextclaw.cli scheduler status   # jobs, next runs, last outcomes
python3 -m nextclaw.cli scheduler run-now  # manual trigger (same safe path)
python3 -m nextclaw.cli scheduler stop     # stop the daemon
```

How it behaves:

- Job state, run history, timestamps, and results persist in SQLite
  (`scheduler.db`); a restart loses nothing.
- Jobs are claimed atomically — two schedulers can never run the same job
  twice (duplicate start is refused via pidfile).
- On startup, stale `running` runs are marked `interrupted` and stuck jobs
  reset; a missed schedule gets one catch-up run, while outages over an
  hour just reschedule (no stampede).
- Each cycle runs in a subprocess with a hard timeout; the cycle itself runs
  the test suite and records changes, results, and the next task — no LLM
  calls, so no quota is burned.

Or run the self-contained demo (uses temp dirs, touches nothing real):

```bash
python3 examples/demo.py
```

## Docs

- [SPEC.md](SPEC.md) - product vision, MVP scope, measurable benchmarks
- [ARCHITECTURE.md](ARCHITECTURE.md) - module design and data flow
- [BENCHMARKS.md](BENCHMARKS.md) - what the suite measures and how to run it
- [COMPARISON.md](COMPARISON.md) - honest comparison with OpenClaw and alternatives
- [ROADMAP.md](ROADMAP.md) - what comes next

## Limitations (MVP)

- Deterministic `run` supports one task type (`file_report`); other goals fail
  closed with a clear error. `run-agent` handles open-ended goals but needs a
  configured model provider and (for machine verification) a `--verify-tool`.
- LLM providers are real but quota-bound: the Gemini free tier can return
  429/503, which the loop surfaces as a failed task with the provider's
  message (429/503 are retried with backoff first).
- No messaging channels, browser automation, scheduler, or web dashboard yet -
  these are roadmap items, not claims.
