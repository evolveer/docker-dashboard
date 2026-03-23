#!/bin/bash
# Docker Dashboard - Startup Script
# Requires: Python 3, pip, Docker running on the host

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Docker Dashboard ==="
echo ""

# Check Docker
if ! docker info > /dev/null 2>&1; then
  echo "ERROR: Docker is not running or not accessible."
  echo "Make sure Docker is running and your user has access to the Docker socket."
  echo "  sudo usermod -aG docker \$USER  (then log out and back in)"
  exit 1
fi

# Install dependencies
echo "Installing Python dependencies..."
pip3 install --upgrade flask docker requests --quiet

echo "Starting Docker Dashboard on http://localhost:5050"
echo "Press Ctrl+C to stop."
echo ""

python3 app.py
