# Contributing to NextClaw

Thanks for considering a contribution. NextClaw is early and small — good
contributions are focused, tested, and honest.

## Getting started

```bash
git clone https://github.com/muteebdeva/nextclaw.git
cd nextclaw
python3 -m unittest discover -s tests   # must pass before you change anything
```

Python 3.10+ required. There are no dependencies — please keep it that way
unless a new one is clearly justified (discuss in an issue first).

## Ground rules

1. **Tests for every behavior change.** New tools, providers, verifiers, and
   recovery paths need unit tests. Tests must run offline — mock the network,
   never call a live API (see `tests/test_llm_loop.py` for the pattern; live
   coverage lives behind `NEXTCLAW_LIVE_TESTS=1`).
2. **No fabricated claims.** Don't claim a feature works unless a test proves
   it. Don't invent benchmarks, integrations, or compatibility.
3. **No secrets, ever.** Keys come from environment variables only. Check
   `git diff` for accidental credentials before pushing.
4. **Preserve the safety rails.** Permission checks, approvals, bounded turns,
   retries, deadlines, and budgets are load-bearing — don't weaken them to
   make a feature convenient.
5. **Small, reviewable changes.** One concern per pull request.

## Adding a tool

1. Register it in `nextclaw/tools.py` with the `@tool` decorator, a clear
   description, and a risk level (`low`/`medium`/`high`).
2. High-risk tools must set `requires_approval=True`.
3. Filesystem access must go through `resolve_within_root`.
4. Add the parameter schema to `PARAM_SPECS` in `nextclaw/llm.py` if the model
   should be able to call it.
5. Add tests in `tests/test_tools.py` (success + denial cases).

## Adding a model provider

1. Subclass `llm.LLMProvider` and implement `chat()` returning `LLMResponse`.
2. Unconfigured providers must raise `NotConfigured`, never hallucinate.
3. Respect the quota system: raise `QuotaExhausted` for quota errors (no
   retry), `RateLimited` for transient limits.
4. Add mocked tests; do not add live tests to the default suite.

## Pull requests

Open an issue first for anything beyond a trivial fix. Fill in the PR
template, keep the diff focused, and make sure CI is green.
