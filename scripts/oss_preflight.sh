#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[1/4] Python compile check"
python3 -m compileall -q src scripts tests

if [[ "${SKIP_GUI_BUILD:-0}" != "1" ]]; then
  echo "[2/4] GUI build check"
  npm --prefix gui run build >/dev/null
else
  echo "[2/4] GUI build check (skipped: SKIP_GUI_BUILD=1)"
fi

echo "[3/4] Personal path / identifier scan"
if rg -n --glob '!scripts/oss_preflight.sh' "/Users/|takumitochizawa|com\\.takumitochizawa" docs gui src scripts tests >/dev/null; then
  echo "Found personal path/identifier. Remove before publish." >&2
  exit 1
fi

echo "[4/4] Secret / large artifact scan"
if rg -n "BEGIN (RSA|OPENSSH|EC|PRIVATE) KEY|AIza[0-9A-Za-z\\-_]{20,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-" . >/dev/null; then
  echo "Found potential secret token/key. Remove before publish." >&2
  exit 1
fi
if git ls-files '*.mp3' '*.wav' '*.m4a' '*.flac' '*.aac' '*.ogg' '*.log' '*.db' '*.sqlite' | rg . >/dev/null; then
  echo "Found tracked media/log/db artifact. Remove before publish." >&2
  exit 1
fi

echo "OK: lightweight OSS preflight passed"
