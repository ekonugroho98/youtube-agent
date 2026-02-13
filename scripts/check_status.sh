#!/bin/bash
# Check stream controller status

PORT=${STREAM_CONTROLLER_PORT:-8000}
BASE_URL="http://localhost:${PORT}"

echo "Checking stream controller status..."

# Health check
echo -n "Health: "
HEALTH=$(curl -s "${BASE_URL}/health")
if echo "$HEALTH" | grep -q "healthy"; then
  echo "OK"
else
  echo "UNAVAILABLE"
  exit 1
fi

# Stream status
echo ""
echo "Stream Status:"
STATUS=$(curl -s "${BASE_URL}/streams/status")
echo "$STATUS" | python3 -m json.tool 2>/dev/null || echo "$STATUS"
