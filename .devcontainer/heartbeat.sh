#!/usr/bin/env bash
# Self-reporting health check: proves from inside the codespace that the
# dashboard is up and which providers are configured. Pushes the result to
# the `heartbeat` branch (master stays clean).
set -u
cd "$(dirname "$0")/.." || exit 1
OUT=".devcontainer/heartbeat.log"
{
  echo "=== $(date -u +%FT%TZ) ==="
  if curl -sf --max-time 10 http://127.0.0.1:8765/api/health; then
    echo
  else
    echo "HEALTH: DOWN"
  fi
  if [ -n "${NEXTCLAW_API_TOKEN:-}" ]; then
    echo "--- providers (keys masked by API) ---"
    curl -sf --max-time 10 -H "Authorization: Bearer $NEXTCLAW_API_TOKEN" \
      http://127.0.0.1:8765/api/providers/configs | head -c 2000
    echo
  else
    echo "NO_API_TOKEN"
  fi
} > "$OUT" 2>&1
git add "$OUT"
git -c user.name="nextclaw" -c user.email="nextclaw@codespace.local" \
  commit -qm "heartbeat $(date -u +%FT%TZ)" || true
git push -q origin HEAD:heartbeat 2>&1 | tail -1 || true
