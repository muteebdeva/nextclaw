<p align="center">
  <img src="assets/nextclaw-banner.svg" alt="NextClaw — the reliability-first AI agent platform" width="100%">
</p>

<p align="center">
  <a href="https://github.com/muteebdeva/nextclaw/actions/workflows/ci.yml"><img src="https://github.com/muteebdeva/nextclaw/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/dependencies-zero-brightgreen" alt="Zero dependencies">
</p>

# NextClaw

**The reliability-first AI agent platform.** NextClaw runs AI agents that plan,
use tools, and *prove* their work: every task has explicit success criteria,
every step is verified against real tool output, and every run is checkpointed
to SQLite so interrupted tasks resume instead of restarting.

```bash
git clone https://github.com/muteebdeva/nextclaw.git
cd nextclaw
python3 -m nextclaw.cli init
python3 -m unittest discover -s tests   # 44 tests, zero dependencies, no API key needed
```

## What makes it different

Most agent frameworks trust the model's word. NextClaw doesn't:

- **Verified execution** — a task is only "done" when a verifier confirms the
  result against recomputed evidence (not when the model says so).
- **Persistent autonomy** — tasks survive crashes and restarts via SQLite
  checkpoints; resume never repeats completed work.
- **Permission-checked tools** — allowlisted tools, workspace confinement,
  destructive-command denylist, secret redaction, and human approval for
  high-risk actions.
- **Quota-safe by design** — API budgets, exhaustion cooldowns, and an offline
  mode protect shared keys.

## Web dashboard (new)

NextClaw ships a real control plane — no terminal required:

```bash
python3 -m nextclaw.cli serve
# → http://127.0.0.1:8765  (paste the printed API token to unlock)
```

- **Overview** — task stats, pending approvals, quota, next scheduled run
- **Tasks** — create (deterministic or model-driven), filter, inspect the
  execution timeline, tool calls with arguments/outputs, and verification
  evidence; cancel or resume from checkpoints
- **Approvals** — pending high-risk actions with full context; approve or
  reject; every decision lands in the audit log
- **Scheduler** — jobs, next runs, run history, enable/disable/run-now
- **Providers** — Gemini/Ollama status, connectivity tests, safe config
  (keys stay in environment variables and are never shown)
- **Settings** — workspace, execution limits, permission policy, theme

The UI is a dependency-free single-page app (`nextclaw/web/`) backed by a
JSON API (`nextclaw/server.py`, stdlib only) with Bearer-token auth,
per-IP rate limits, and Server-Sent Events for live task updates.
See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) before exposing it beyond
localhost.

## Quickstart: a verified agent run

No API key needed — the deterministic planner runs fully offline:

```bash
python3 -m nextclaw.cli --workspace . run \
  --goal "Count the Python files and write a verified report" \
  --type file_report \
  --param root=nextclaw --param pattern='**/*.py' \
  --param output=reports/quickstart.md
```

The agent plans 4 steps (collect → measure → write → verify), executes each
through the permission system, checkpoints to SQLite, then **recomputes the
measurements** and compares them against the report's claimed totals:

```json
{
  "report_path": "reports/quickstart.md",
  "sha256": "e5e9dabade42914a9f75f10b1d8108e3dc845537df81ec284d519372b62e0996",
  "total_files": 13,
  "total_lines": 2791,
  "verified": true
}
```

## Real-model agents (`run-agent`)

With an API key, the model itself reasons, picks tools, and inspects real
outputs in a tool-calling loop:

```bash
export NEXTCLAW_GEMINI_API_KEY="your-key-here"
python3 -m nextclaw.cli run-agent \
  --goal "Count the Python files under src and write a verified report to reports/r.md" \
  --verify-tool verify.file_report \
  --verify-arg report_path=reports/r.md \
  --verify-arg root=src --verify-arg pattern='**/*.py'
```

Every turn is persisted, so `resume --task-id <id>` continues an interrupted
run from the saved conversation. If the verifier reports `ok:true`, the loop
stops on the explicit success criterion instead of second-guessing it.

### Providers

| Provider | Setup | Status |
|---|---|---|
| **Gemini** (default) | `NEXTCLAW_GEMINI_API_KEY` | Function calling verified working with `gemini-3.7-flash`; quota-guarded |
| **Ollama** | `NEXTCLAW_OLLAMA_URL` + `NEXTCLAW_OLLAMA_MODEL` | Implemented; not tested in this environment (needs local models) |

An OpenAI-compatible provider interface exists but is not yet implemented —
it fails closed with a clear error rather than pretending to work.

## How it works

```mermaid
flowchart LR
    A[Goal] --> B{Planner}
    B -->|deterministic| C[Explicit step plan]
    B -->|run-agent| D[LLM tool-calling loop]
    C --> E[Permission check]
    D --> E
    E --> F[Execute tool]
    F --> G[Verify against real output]
    G --> H[(SQLite checkpoint)]
    H -->|more steps| E
    H -->|verifier ok| I[Done + evidence]
    G -->|failed| J[Bounded retry → fail with diagnosis]
```

- **Planning** — `run` uses an explicit deterministic planner; `run-agent`
  lets the model plan in its own words via function calling.
- **Tools** — 9 built-ins: `fs.list`, `fs.read`, `fs.write`, `fs.hash`,
  `fs.measure`, `browser.fetch`, `verify.file_report`, `verify.file_write`,
  `exec.run`. All run through one permission policy.
- **Deterministic task types** — `file_report`, `file_write`,
  `browser_research` (each with explicit success criteria and verifiers),
  plus open-ended `run-agent` goals with a configured model.
- **Verification** — verifiers recompute results independently. A wrong report
  fails the task with a diagnosis; the model can read the error and try again.
- **Checkpoints** — task state, steps, and conversation turns persist after
  every tool call. Crash recovery is tested, not assumed.
- **Safety rails** — bounded turns (default 12), bounded retries (3),
  task deadlines, per-task and daily API budgets, approvals for high-risk
  tools, secret redaction on all tool output.

## Autonomous dev cycles

A persistent scheduler runs the test suite and records project state every
40 minutes:

```bash
python3 -m nextclaw.cli scheduler ensure   # create the 40-minute job
python3 -m nextclaw.cli scheduler start    # start the daemon
python3 -m nextclaw.cli scheduler status   # jobs, next runs, last outcomes
python3 -m nextclaw.cli scheduler run-now  # manual trigger
python3 -m nextclaw.cli scheduler stop
```

State survives restarts; overlapping runs are impossible by atomic claim;
stale runs are marked `interrupted` on recovery. Scheduler cycles never call
the LLM, so they burn no quota.

## Project structure

```
nextclaw/
  nextclaw/
    agent.py        # deterministic planner + verified executor
    agent_llm.py    # live-model tool-calling loop
    llm.py          # provider abstraction (Gemini, Ollama) + quota budgets
    tools.py        # tool registry, permissions, sandbox, secret redaction
    browsertools.py # SSRF-guarded web fetching (read-only research)
    approvals.py    # human approval gate + audit log
    channels.py     # Telegram channel (opt-in, mock-tested)
    server.py       # dashboard API + static server (stdlib only)
    web/            # dashboard SPA (index.html, styles.css, app.js)
    tasks.py        # SQLite task queue + step state machine + turns + events
    memory.py       # structured long-term memory with provenance
    scheduler.py    # persistent 40-min dev-cycle scheduler
    bench.py        # reproducible benchmark suite
    cli.py          # command-line interface
    cycle.py        # autonomous dev-cycle runner
  tests/            # 85 tests, all offline (live test env-gated)
  docs/             # deployment guide
  examples/demo.py  # self-contained end-to-end demo (temp dirs only)
  assets/           # logo, banner, social preview (SVG)
  .github/          # CI, issue/PR templates
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NEXTCLAW_DATA_DIR` | `~/.nextclaw` | SQLite state, logs |
| `NEXTCLAW_WORKSPACE` | `~/workspace` | agent workspace root (tools are confined here) |
| `NEXTCLAW_MODEL_PROVIDER` | `stub` | deterministic planner provider |
| `NEXTCLAW_LLM_PROVIDER` | `gemini` | `gemini` \| `ollama` |
| `NEXTCLAW_LLM_MODEL` | `gemini-3.7-flash` | model name |
| `NEXTCLAW_GEMINI_API_KEY` | — | required for `run-agent` with Gemini |
| `NEXTCLAW_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama base URL |
| `NEXTCLAW_OLLAMA_MODEL` | — | required for Ollama provider |
| `NEXTCLAW_LLM_TIMEOUT_S` | `60` | per model-call timeout |
| `NEXTCLAW_LLM_MAX_PER_DAY` | `200` | daily API request cap |
| `NEXTCLAW_OFFLINE` | — | set to `1` to disable all model calls |

Copy `.env.example` to `.env` for a documented starting point (never commit
real keys).

## Testing

```bash
python3 -m unittest discover -s tests          # full suite (85 tests), offline
NEXTCLAW_LIVE_TESTS=1 python3 -m unittest tests.test_llm_live   # needs API key
python3 examples/demo.py                        # end-to-end demo in temp dirs
python3 -m nextclaw.cli bench                   # benchmark suite
```

CI runs the suite on Python 3.10–3.12. The live-model test is skipped by
default so CI and contributors never burn quota.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Gemini provider needs NEXTCLAW_GEMINI_API_KEY` | export the key, or use offline `run` |
| `quota cooldown active` | free-tier quota exhausted; wait or raise `llm_quota_cooldown_s` |
| `approval required for tool 'exec.run'` | resume with `--approve exec.run` after reviewing, or approve in the dashboard |
| `path escapes workspace root` | tools are confined to `NEXTCLAW_WORKSPACE` by design |
| `did not finish within 12 turns` | raise `max_turns`, or simplify the goal |
| dashboard asks for a token | paste the token printed by `nextclaw serve` (or set `NEXTCLAW_API_TOKEN`) |

## Security

- No secrets in code, docs, or git history — keys come only from the
  environment. See [SECURITY.md](SECURITY.md).
- Tool outputs are redacted for secret-like patterns before the model sees them.
- `exec.run` never uses a shell, is denylisted for destructive patterns, is
  confined to the workspace, and requires explicit human approval.
- Unconfigured providers fail closed with a clear error.
- The dashboard API requires a Bearer token; keys are never exposed by any
  endpoint; browser fetching is SSRF-guarded. See
  [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Messaging

An opt-in Telegram channel (`python3 -m nextclaw.cli channels telegram`)
maps messages to tasks: `/task <goal>`, `/status`, `/tasks`, `/cancel`.
Access is restricted to `NEXTCLAW_TELEGRAM_ALLOWED_IDS`, update offsets
persist (no duplicate tasks on restart), and approvals are never granted
from chat — they require the dashboard or CLI. Token via
`NEXTCLAW_TELEGRAM_BOT_TOKEN`. Any pre-existing bot is untouched.

## Roadmap

- [x] Verified deterministic task execution
- [x] Live-model tool-calling loop (Gemini; Ollama implemented)
- [x] Persistent 40-minute scheduler
- [x] Quota budgets and cooldowns
- [x] Web dashboard + API + human approvals
- [x] Task cancellation and audit log
- [x] Browser research (fetch-only; interactive browsing needs headless Chromium)
- [x] Telegram channel (opt-in)
- [ ] Multi-agent delegation
- [ ] Cost/token accounting per task
- [ ] Head-to-head benchmark vs alternatives

See [ROADMAP.md](ROADMAP.md) for detail.

## Contributing

Bug reports and pull requests are welcome. Please read
[CONTRIBUTING.md](CONTRIBUTING.md) first — it covers the test suite, how to add
tools and providers, and the no-fabrication rule: every claim needs a test.

## License

The license is being finalized (MIT proposed). Until `LICENSE` lands, all
rights are reserved by the repository owner.

---

*Built with zero dependencies — Python's standard library only.*
