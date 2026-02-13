#!/bin/bash
# Health check for monitoring systems

PORT=${STREAM_CONTROLLER_PORT:-8000}
BASE_URL="http://localhost:${PORT}"

# Check health endpoint
HEALTH=$(curl -s -w "\n%{http_code}" "${BASE_URL}/health" 2>/dev/null)

# Get status code (last line)
STATUS_CODE=$(echo "$HEALTH" | tail -n1)

if [ "$STATUS_CODE" = "200" ]; then
  echo "OK"
  exit 0
else
  echo "UNAVAILABLE"
  exit 1
fi
