#!/bin/bash
# Start the stream controller service

set -e

echo "Starting stream controller..."

# Check if running as root
if [ "$EUID" -eq 0 ]; then
  echo "Error: Don't run as root. Use: sudo -u stream $0"
  exit 1
fi

# Check if venv exists
if [ ! -d "venv" ]; then
  echo "Error: Virtual environment not found. Run setup first."
  exit 1
fi

# Start systemd service
systemctl --user start stream-controller

echo "Stream controller started"
echo "Check status with: scripts/check_status.sh"
