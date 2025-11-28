#!/usr/bin/env bash
# Run the meeting processor on a daily schedule (suitable for cron).
set -euo pipefail

PROJECT_ROOT="/Users/kbrooks/Dropbox/Projects/LLMscanner"
CALIBRE_ROOT="/Users/kbrooks/Dropbox/Books/calibreGPT_test_lg"
PYTHON_BIN="${PYTHON_BIN:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}"
LOG_DIR="$PROJECT_ROOT/logs"
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
LOG_FILE="$LOG_DIR/cron.log"

mkdir -p "$LOG_DIR"
cd "$PROJECT_ROOT"

{
  echo "[$TIMESTAMP] Running nightly meeting processor..."
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/process_meetings.py" \
    --calibre-root "$CALIBRE_ROOT" \
    --log-dir "$LOG_DIR"
  echo "[$TIMESTAMP] Completed."
} >>"$LOG_FILE" 2>&1
