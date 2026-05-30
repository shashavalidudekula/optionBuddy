#!/bin/bash
# setup.sh — VPS one-command setup (Ubuntu 22.04)
# Usage: bash setup.sh

set -e

echo "── Installing system deps ─────────────────────────────"
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv git

echo "── Creating virtualenv ────────────────────────────────"
python3 -m venv venv
source venv/bin/activate

echo "── Installing Python packages ─────────────────────────"
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "── Creating .env from example ─────────────────────────"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "⚠  .env created. Fill in your keys before running."
fi

echo "── Setting up data directory ──────────────────────────"
mkdir -p data logs

echo "── Installing systemd service ─────────────────────────"
SERVICE_FILE="/etc/systemd/system/trading-agent.service"
WORK_DIR=$(pwd)

cat > $SERVICE_FILE <<EOF
[Unit]
Description=F&O Trading Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=$WORK_DIR
ExecStart=$WORK_DIR/venv/bin/python main.py
Restart=on-failure
RestartSec=30
EnvironmentFile=$WORK_DIR/.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable trading-agent
echo "✅ Service installed. Start with: systemctl start trading-agent"
echo "   Logs: journalctl -u trading-agent -f"
echo ""
echo "── Next steps ─────────────────────────────────────────"
echo "1. Fill in .env with your API keys"
echo "2. Run: source venv/bin/activate && python -m core.kite_auth"
echo "3. Start: systemctl start trading-agent"
