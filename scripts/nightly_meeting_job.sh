#!/usr/bin/env bash
# Run the meeting processor on a daily schedule (suitable for cron).
set -euo pipefail

PROJECT_ROOT="/Users/kbrooks/Dropbox/Projects/LLMscanner"
CALIBRE_ROOT="/Users/kbrooks/Dropbox/Books/Calibre Travel Library"
PYTHON_BIN="${PYTHON_BIN:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}"
LOG_DIR="$PROJECT_ROOT/logs"
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
LOG_FILE="$LOG_DIR/cron.log"
ENV_FILE="$PROJECT_ROOT/.env"

mkdir -p "$LOG_DIR"
cd "$PROJECT_ROOT"

# Load local env vars (e.g., OPENAI_API_KEY) without requiring python-dotenv.
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck source=/Users/kbrooks/Dropbox/Projects/LLMscanner/.env
  . "$ENV_FILE"
  set +a
fi

{
  echo "[$TIMESTAMP] Running nightly meeting processor..."
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/process_meetings.py" \
    --calibre-root "$CALIBRE_ROOT" \
    --log-dir "$LOG_DIR"
  echo "[$TIMESTAMP] Completed."
} >>"$LOG_FILE" 2>&1
