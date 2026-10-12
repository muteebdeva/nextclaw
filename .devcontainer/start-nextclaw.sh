#!/usr/bin/env bash
# Idempotent nextclaw dashboard launcher for the codespace.
# Safe to run on every container start: does nothing if already serving.
set -u
DATA_DIR="${NEXTCLAW_DATA_DIR:-$HOME/.nextclaw}"
mkdir -p "$DATA_DIR"
cd "$(dirname "$0")/.." || exit 1

if curl -sf --max-time 3 http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
  echo "$(date -u +%FT%TZ) serve already running" >>"$DATA_DIR/serve.log"
  exit 0
fi

{
  echo "=== start $(date -u +%FT%TZ) ==="
  if [ -z "${NEXTCLAW_GEMINI_API_KEY:-}" ]; then
    echo "WARNING: NEXTCLAW_GEMINI_API_KEY not set - Gemini provider unavailable"
  else
    echo "NEXTCLAW_GEMINI_API_KEY present (${#NEXTCLAW_GEMINI_API_KEY} chars)"
  fi
} >>"$DATA_DIR/serve.log" 2>&1

nohup python3 -m nextclaw.cli serve --port 8765 >>"$DATA_DIR/serve.log" 2>&1 &
echo "$(date -u +%FT%TZ) serve started (pid $!)" >>"$DATA_DIR/serve.log"
