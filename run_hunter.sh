#!/bin/bash
# Apartment Hunter — local cron runner
# Loads .env, activates venv, runs the hunter, and logs output.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/hunt_$(date +'%Y-%m-%d_%H%M%S').log"

# Load environment variables from .env
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
else
    echo "ERROR: .env file not found. Copy .env.example to .env and fill in your secrets." >&2
    exit 1
fi

# Use project-local virtualenv when present
PYTHON_BIN="python3"
if [ -f "$SCRIPT_DIR/venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/venv/bin/python"
fi

# Run the hunter and log output
echo "=== Apartment Hunter — $(date) ===" | tee "$LOG_FILE"
"$PYTHON_BIN" "$SCRIPT_DIR/main.py" "$@" 2>&1 | tee -a "$LOG_FILE"

# Clean up logs older than 30 days
find "$LOG_DIR" -name "hunt_*.log" -mtime +30 -delete 2>/dev/null || true

echo "Log saved to: $LOG_FILE"
