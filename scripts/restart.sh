#!/bin/bash
# Restart the stream controller service

set -e

echo "Restarting stream controller..."

# Restart systemd service
systemctl --user restart stream-controller

echo "Stream controller restarted"
echo "Check status with: scripts/check_status.sh"
