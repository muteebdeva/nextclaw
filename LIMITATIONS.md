# NextClaw — Known Limitations (2026-10-12)

Honest inventory of what the product cannot do yet, or does with caveats.
Updated as capabilities land.

## Resolved by the dashboard milestone

- ~~No web UI~~ → `nextclaw serve` dashboard (stdlib HTTP, vanilla JS SPA).
- ~~Approvals only via CLI flags~~ → SQLite approval queue + API + UI dialogs.
- ~~No task cancellation~~ → cooperative cancel between steps/turns.
- ~~Single deterministic task type~~ → `file_report`, `file_write`,
  `workspace_inspect` (+ `run-agent` for open-ended goals).
- ~~Scheduler invisible~~ → API + UI for jobs, runs, enable/disable/run-now.
- ~~No messaging~~ → Telegram channel module (opt-in, mock-tested).

## Still true

1. **Browser automation is fetch-only.** `browser.fetch` retrieves and parses
   pages over HTTP with SSRF protection. No JS rendering, clicking, form
   filling, or screenshots — headless Chromium is non-functional in this
   environment, so interactive browsing is unimplemented, not stubbed.
2. **Ollama provider unverified.** Implemented against the Ollama REST API
   but never exercised with a real model here (insufficient RAM).
3. **Live Gemini tests are quota-gated.** The tool-calling loop was verified
   live (loop, self-correction, resume), but a clean end-to-end completion
   run is still pending a quota reset.
4. **No multi-agent delegation.** Parent/child tasks are on the roadmap.
5. **No cost/token accounting.** Quota budgets exist (request counts), but
   per-task token spend is not tracked.
6. **Dashboard binds localhost by default.** Remote access requires the
   operator to configure a reverse proxy + HTTPS + token auth (see
   `docs/DEPLOYMENT.md`). The API token is the only access control.
7. **Telegram is opt-in and untested live.** No bot token is configured;
   the existing `@gogomuteebyou_bot` (OpenClaw) is untouched and unrelated.
8. **No head-to-head benchmark vs alternatives.** Claimed differentiators
   remain evidence-gated per SPEC.md §2.
