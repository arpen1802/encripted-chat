#!/bin/bash
# EncryptedChat dev startup script.
# Creates an isolated virtualenv on first run so we don't fight macOS /
# Homebrew PEP-668 protection. Production deployments should use a systemd
# unit instead — see README.md.
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

VENV_DIR="$SCRIPT_DIR/.venv"

# Create the venv if missing
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtualenv at .venv ..."
  python3 -m venv "$VENV_DIR"
fi

# Use the venv's interpreter & pip explicitly (no need to activate)
PY="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

# Install dependencies if missing or stale
if ! "$PY" -c "import fastapi" &>/dev/null; then
  echo "Installing dependencies into .venv ..."
  "$PIP" install --upgrade pip --quiet
  "$PIP" install -r requirements.txt --quiet
fi

# Generate a secure SECRET_KEY if one isn't set
if [ -z "$SECRET_KEY" ]; then
  export SECRET_KEY=$("$PY" -c "import secrets; print(secrets.token_hex(32))")
  echo "Generated session key (restart will invalidate existing tokens)"
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

echo ""
echo "Starting server on http://$HOST:$PORT"
echo "Default login: admin / admin123"
echo "Press Ctrl+C to stop"
echo ""

exec "$PY" -m uvicorn main:app --host "$HOST" --port "$PORT" --reload
