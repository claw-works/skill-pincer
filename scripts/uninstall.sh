#!/bin/bash
set -e
SERVICE_NAME="pincer-daemon"
systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/$SERVICE_NAME.service"
systemctl --user daemon-reload
echo "✅ pincer-daemon uninstalled."
