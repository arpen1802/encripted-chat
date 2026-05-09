#!/bin/bash
# EncryptedChat startup script
set -e

echo "=================================================="
echo "  EncryptedChat — E2E Encrypted Internal Chat"
echo "=================================================="

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Please install Python 3.9+."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Install dependencies if needed
if ! python3 -c "import fastapi" &>/dev/null; then
  echo "Installing dependencies..."
  pip3 install -r requirements.txt --quiet
fi

# Generate a secure SECRET_KEY if one isn't set
if [ -z "$SECRET_KEY" ]; then
  export SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  echo "Generated session key (restart will invalidate existing tokens)"
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

echo ""
echo "Starting server on http://$HOST:$PORT"
echo "Default login: admin / admin123"
echo "Press Ctrl+C to stop"
echo ""

python3 -m uvicorn main:app --host "$HOST" --port "$PORT" --reload
