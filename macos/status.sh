#!/usr/bin/env bash
set -euo pipefail

LABEL="com.anyrouter.proxy"

if launchctl list | grep -q "$LABEL"; then
  echo "Running: $LABEL"
else
  echo "Not running: $LABEL"
fi
