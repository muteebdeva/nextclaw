# Honest comparison

## What OpenClaw is

OpenClaw is an open-source, self-hosted personal AI agent gateway (MIT): one
long-lived Gateway process owns messaging channels (WhatsApp, Telegram, Slack,
Discord, and more), the agent harness, a plugin/skill registry, scheduling, and
local state/memory, with a WebSocket control plane and Control UI. Tools run on
the host for the main session unless sandboxing is configured; permissions are
enforced at agent, sandbox, and network tiers, with skill-permission manifests
still in active RFC at the time of research.

## Where OpenClaw is strong (acknowledged)

- Mature channel coverage (20+ messaging surfaces) and a real plugin/skill
ecosystem with a registry.
- Local-first state: sessions, memory, and credentials live on the user's
hardware; models are swappable providers.
- Active security work: argument-scoped approvals, secret substitution at the
egress boundary, and a published weakness taxonomy (prompt injection via
skills, credential exposure, skill supply chain, sandbox escape, over-broad
permissions, memory poisoning, network exposure).

## Documented weaknesses we designed against

- **Context bloat:** smaller/medium models can burn ~15-20k tokens before the
task starts (workspace files ~5-8k, skills ~8-12k, system prompts + tools
~3-5k), leaving the model "exhausted" and pushing fallback usage.
NextClaw's answer: a tiny, explicit tool surface (7 tools), no prompt
assembly in the MVP, and verifiers that don't depend on model claims.
- **Operational fragility:** long sessions accumulate context, retries, and
repair loops; users report agents getting lost, repeating themselves, and
setups that feel fragile to touch.
NextClaw's answer: bounded retries, explicit step state in SQLite, and crash
recovery tested by an injected-crash benchmark.
- **Cost of failure:** orchestration overhead (8-18k tokens before the task)
makes failures expensive, not just annoying.
NextClaw's answer: deterministic planning for the MVP task family and
per-run evidence instead of token-burning repair loops.

## Alternatives (context)

- **LangGraph:** strongest fit for production stateful workflows - explicit
state, checkpointing, debuggability - at higher setup cost.
- **CrewAI:** fastest for role-based multi-agent prototypes; less visibility
when things go wrong.
- **AutoGen:** conversational multi-agent research; not ideal for precise
execution order.
- **AutoGPT:** built for exploration, not production; weak error recovery and
observability.

## What NextClaw does NOT claim

- We have **not** benchmarked head-to-head against OpenClaw; our benchmarks
measure our MVP only.
- We do not have channels, browser automation, scheduling, or a dashboard yet.
- The MVP planner is deterministic, not an LLM; LLM providers are pluggable
but disabled by default.

The honest summary: OpenClaw is the broader, battle-tested platform today.
NextClaw's MVP is narrower but measurably stricter about verification,
persistence, and permission boundaries - and every claim above is backed by a
test or benchmark you can run.
