#!/usr/bin/env bash
# Run the meeting processor on a daily schedule (suitable for cron).
set -euo pipefail

PROJECT_ROOT="/Users/kbrooks/Dropbox/Projects/LLMscanner"
CALIBRE_ROOT="/Users/kbrooks/Dropbox/Books/Calibre Travel Library"
DEFAULT_PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
if [ ! -x "$DEFAULT_PYTHON_BIN" ]; then
  DEFAULT_PYTHON_BIN="/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
fi
PYTHON_BIN="${PYTHON_BIN:-$DEFAULT_PYTHON_BIN}"
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

# Force native arm64 on Apple Silicon when available (avoids Rosetta/x86_64 wheel mismatches).
ARCH_DESC="native"
USE_ARCH=0
if command -v arch >/dev/null 2>&1 && sysctl -n hw.optional.arm64 >/dev/null 2>&1; then
  if sysctl -n hw.optional.arm64 | grep -q "1"; then
    ARCH_DESC="arch -arm64"
    USE_ARCH=1
  fi
fi

{
  echo "[$TIMESTAMP] Running nightly meeting processor with $PYTHON_BIN ($ARCH_DESC)..."
  if [ "$USE_ARCH" -eq 1 ]; then
    arch -arm64 "$PYTHON_BIN" "$PROJECT_ROOT/scripts/process_meetings.py" \
      --calibre-root "$CALIBRE_ROOT" \
      --log-dir "$LOG_DIR"
  else
    "$PYTHON_BIN" "$PROJECT_ROOT/scripts/process_meetings.py" \
      --calibre-root "$CALIBRE_ROOT" \
      --log-dir "$LOG_DIR"
  fi
  echo "[$TIMESTAMP] Completed."
} >>"$LOG_FILE" 2>&1
