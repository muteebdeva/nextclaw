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

- One supported task type (`file_report`); other goals fail closed with a clear error.
- Default planner is deterministic (`stub`); LLM providers are pluggable but
  unconfigured providers refuse to run rather than hallucinate.
- No messaging channels, browser automation, scheduler, or web dashboard yet -
  these are roadmap items, not claims.
