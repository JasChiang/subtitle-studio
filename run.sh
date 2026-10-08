#!/usr/bin/env bash
# Starts the web GUI at http://127.0.0.1:8765 (reads GEMINI_API_KEY from .env or the environment).
set -euo pipefail
cd "$(dirname "$0")"
{ [ -x bin/apple-asr ] && [ -x bin/text-render ]; } || ./setup.sh
( sleep 1; open "http://127.0.0.1:${PORT:-8765}" ) &
exec python3 -m studio.server --port "${PORT:-8765}" "$@"
