#!/usr/bin/env bash
# One-time codespace creation bootstrap: init state, then start the dashboard.
set -u
DATA_DIR="${NEXTCLAW_DATA_DIR:-$HOME/.nextclaw}"
mkdir -p "$DATA_DIR"
cd "$(dirname "$0")/.." || exit 1
python3 -m nextclaw.cli init >>"$DATA_DIR/postcreate.log" 2>&1 || echo "init failed (continuing)" >>"$DATA_DIR/postcreate.log"
bash "$(dirname "$0")/start-nextclaw.sh"
