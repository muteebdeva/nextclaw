# Deploying NextClaw safely

The dashboard + API is a **privileged control plane**: it can create tasks,
approve high-risk tool actions, and read your workspace. Treat it accordingly.

## Default: localhost only

`nextclaw serve` binds `127.0.0.1:8765` and requires a Bearer token for every
API route except `/api/health`. This is safe on a single-user machine.

```bash
python3 -m nextclaw.cli serve                 # http://127.0.0.1:8765
python3 -m nextclaw.cli serve --port 9000     # custom port
```

The token comes from `NEXTCLAW_API_TOKEN`, or is auto-generated into
`<data_dir>/api_token` (mode 600) and printed once at startup.

## Exposing beyond localhost

If you need LAN or internet access, put a reverse proxy in front and never
expose the Python server directly:

1. Keep `nextclaw serve` on `127.0.0.1`.
2. Terminate TLS at the proxy (Caddy, nginx, Cloudflare Tunnel).
3. Require the `Authorization: Bearer <token>` header — the dashboard's
   unlock screen handles this; never put the token in a URL you share.
4. Restrict source IPs at the proxy / firewall where possible.

Example (Caddy):

```
nextclaw.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Caddy gets you HTTPS automatically. With nginx, add your own certificates
and forward only to loopback.

## What the server does and doesn't do

- No CORS headers are emitted; the dashboard is same-origin.
- Request bodies are capped at 1 MB; JSON only.
- Sensitive endpoints (task creation, approvals, scheduler triggers) are
  rate-limited per IP (30/min).
- API keys are **never** accepted or returned by the API — they come from
  the server's environment only.
- The browser tool refuses private/loopback/link-local addresses by
  default (SSRF guard). Only set `NEXTCLAW_BROWSER_ALLOW_NONPUBLIC=1`
  behind an egress proxy, never on an internet-facing host.
- Logs never include `Authorization` headers or request bodies.

## Checklist before going public

- [ ] HTTPS terminated at the proxy, HTTP redirected
- [ ] Strong `NEXTCLAW_API_TOKEN` (32+ random chars), rotated from the default
- [ ] Firewall / security group restricts source IPs
- [ ] `NEXTCLAW_BROWSER_ALLOW_NONPUBLIC` is **not** set
- [ ] Telegram `NEXTCLAW_TELEGRAM_ALLOWED_IDS` lists only your user ID
- [ ] You understand approvals: the dashboard approve button authorizes
      real tool execution — don't approve what you don't understand
