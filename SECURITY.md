# Security Policy

## Reporting a vulnerability

Open a GitHub Security Advisory on this repository
(`Security` → `Advisories` → `New draft advisory`), or open a private issue
if advisories are unavailable. Please include:

- What you found and why it matters
- Steps to reproduce (without attacking any live systems)
- Whether you believe secrets or user data are at risk

We aim to acknowledge reports within 7 days. Please do not disclose
vulnerabilities publicly until we've had a chance to address them.

## What we promise

- API keys and tokens are never hardcoded, committed, or logged. They are
  read from environment variables only.
- Tool outputs are redacted for secret-like patterns before being shown to
  models or written to logs.
- `exec.run` never uses a shell, is confined to the workspace, is denylisted
  for destructive patterns, and requires explicit human approval.
- Dependencies are intentionally zero (stdlib only), minimizing supply-chain
  surface.

## Scope

This policy covers the code in this repository. It does not cover the
security of third-party model providers or the infrastructure you run
NextClaw on — keep your own API keys and machines secure.
