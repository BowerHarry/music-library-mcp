#!/bin/bash
# Starts the server (settings come from .env, see .env.example). The launch agent runs this.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
mkdir -p data
exec uv run --quiet python -m library_mcp.server "$@"
