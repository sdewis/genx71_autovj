#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
else
  source .venv/bin/activate
fi

PORT="${PORT:-8710}"
HOST="${HOST:-0.0.0.0}"

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
TS_IP=$(tailscale ip -4 2>/dev/null | head -n 1 || true)

echo "=========================================="
echo "  GenX71 AutoVJ v2.0 Studio & Daemon"
echo "  Linux Desktop : http://localhost:${PORT}"
echo "  LAN (Wi-Fi)   : http://${LOCAL_IP}:${PORT}"
if [ -n "${TS_IP}" ]; then
  echo "  Tailscale     : http://${TS_IP}:${PORT}"
fi
echo "=========================================="

exec uvicorn api.app:app --host "$HOST" --port "$PORT"
