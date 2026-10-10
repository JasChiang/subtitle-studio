#!/usr/bin/env bash
# Starts the web GUI at http://127.0.0.1:8765 (reads GEMINI_API_KEY from .env or the environment).
#   PORT=8766 ./run.sh      another port        NO_OPEN=1 ./run.sh      don't open the browser
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"

# (Re)build the Swift helpers when missing or older than their sources (e.g. right after a git pull).
if [ ! -x bin/apple-asr ] || [ ! -x bin/text-render ] || [ asr/main.swift -nt bin/apple-asr ] \
   || [ render/main.swift -nt bin/text-render ]; then
  ./setup.sh
fi

# Something already listening on the port?
pids="$(lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$pids" ]; then
  if curl -s --max-time 2 "http://127.0.0.1:$PORT/api/defaults" | grep -q '"has_asr"'; then
    echo "連接埠 $PORT 上已有一個 Subtitle Studio（可能是舊版本），先把它關掉再重開…"
    kill $pids 2>/dev/null || true
    for _ in $(seq 1 50); do lsof -t -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.1; done
  else
    echo "連接埠 $PORT 被其他程式占用：" >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
    echo "可以關掉那個程式，或換一個連接埠：PORT=8766 ./run.sh" >&2
    exit 1
  fi
fi

[ -n "${NO_OPEN:-}" ] || ( sleep 1; open "http://127.0.0.1:$PORT" ) &
exec python3 -m studio.server --port "$PORT" "$@"
