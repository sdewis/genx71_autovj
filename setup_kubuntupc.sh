#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "==> [1/5] Installing system packages (ffmpeg, python3-venv, curl)..."
sudo apt-get update -y
sudo apt-get install -y ffmpeg python3-venv python3-pip curl

echo "==> [2/5] Installing & configuring Tailscale..."
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
sudo systemctl enable --now tailscaled
if ! tailscale status >/dev/null 2>&1; then
  echo "==> Authenticating Tailscale (follow the link if prompted)..."
  sudo tailscale up
fi

echo "==> [3/5] Setting up Python virtual environment & dependencies..."
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install --with-deps chromium

echo "==> [4/5] Installing 24/7 systemd service (genx-autovj.service)..."
SERVICE_FILE="/etc/systemd/system/genx-autovj.service"
sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=GenX71 AutoVJ 24/7 TikTok Studio & Scheduler
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/uvicorn api.app:app --host 0.0.0.0 --port 8710
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now genx-autovj.service

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
TS_IP=$(tailscale ip -4 2>/dev/null | head -n 1 || echo "<run: tailscale ip -4>")

echo ""
echo "=========================================================="
echo "  GenX71 AutoVJ v2.0 is RUNNING on $(hostname)!"
echo "  Local Browser (Kubuntu) : http://localhost:8710"
echo "  Samsung S24 (Wi-Fi LAN) : http://${LOCAL_IP}:8710"
echo "  Samsung S24 (Tailscale) : http://${TS_IP}:8710"
echo "  Service Status          : sudo systemctl status genx-autovj"
echo "=========================================================="
