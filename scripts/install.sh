#!/bin/bash
# install.sh — Install pincer-daemon as a systemd user service
set -e

CONFIG_PATH="${1:-$HOME/.openclaw/pincer-daemon.json}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DAEMON_SCRIPT="$SCRIPT_DIR/daemon.py"
SERVICE_NAME="pincer-daemon"
SERVICE_FILE="$HOME/.config/systemd/user/$SERVICE_NAME.service"

if [ ! -f "$CONFIG_PATH" ]; then
  echo "❌ Config not found: $CONFIG_PATH"
  echo "   Copy references/config.example.json → $CONFIG_PATH and fill in your credentials."
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "❌ python3 not found"
  exit 1
fi

python3 -c "import websockets" 2>/dev/null || {
  echo "Installing websockets..."
  pip3 install websockets
}

mkdir -p "$(dirname "$SERVICE_FILE")"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Pincer WebSocket Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=python3 $DAEMON_SCRIPT --config $CONFIG_PATH
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user start "$SERVICE_NAME"

echo "✅ pincer-daemon installed and started."
echo ""
echo "Management:"
echo "  systemctl --user status $SERVICE_NAME"
echo "  journalctl --user -u $SERVICE_NAME -f"
echo "  systemctl --user restart $SERVICE_NAME"
echo "  systemctl --user stop $SERVICE_NAME"
