#!/usr/bin/env bash
# Builds the Apple ASR helper (needs macOS 26+, Swift toolchain; ffmpeg for audio/proxy extraction).
set -euo pipefail
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || { echo "需要 ffmpeg：brew install ffmpeg" >&2; exit 1; }
mkdir -p bin
swiftc -O -parse-as-library asr/main.swift -o bin/apple-asr
echo "built bin/apple-asr — supported locales:"
./bin/apple-asr --list-locales | tr '\n' ' '; echo
[ -f .env ] || { echo "GEMINI_API_KEY=" > .env; chmod 600 .env; echo "已建立 .env，請填入 GEMINI_API_KEY"; }
