#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PLIST_LABEL="com.apartmenthunter.daily"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
RUN_SCRIPT="$SCRIPT_DIR/run_hunter.sh"
HOUR=8
MINUTE=0

echo "Apartment Hunter macOS installer"
echo "Project: $SCRIPT_DIR"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is not installed." >&2
  exit 1
fi

if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo "No .env configuration file found."
  echo "Launching the interactive setup wizard..."
  echo
  if ! python3 "$SCRIPT_DIR/setup.py"; then
    echo "Setup wizard failed or was cancelled. Please configure your .env file manually." >&2
    exit 1
  fi
fi

echo "Creating project virtualenv..."
python3 -m venv "$SCRIPT_DIR/venv"

echo "Installing dependencies..."
source "$SCRIPT_DIR/venv/bin/activate"
python -m pip install --upgrade pip
pip install -r "$SCRIPT_DIR/requirements.txt"

echo
echo "Running dry run..."
if ! "$RUN_SCRIPT" --dry-run; then
  echo
  echo "Dry run failed. Scheduler was not installed." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$RUN_SCRIPT</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$HOUR</integer>
        <key>Minute</key>
        <integer>$MINUTE</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/logs/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/logs/launchd_stderr.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
launchctl start "$PLIST_LABEL"

echo
echo "Install complete."
echo "Dry run passed."
echo "Daily schedule installed for $(printf "%02d:%02d" "$HOUR" "$MINUTE")."
echo "LaunchAgent: $PLIST_PATH"
