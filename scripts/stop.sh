#!/bin/bash
# Stop the stream controller service

set -e

echo "Stopping stream controller..."

# Stop systemd service
systemctl --user stop stream-controller

echo "Stream controller stopped"
