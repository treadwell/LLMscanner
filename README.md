# LLMscanner

Utilities for extracting actionable items from meeting transcripts stored in a Calibre library.

## Quick Start
- Ensure Python 3.11+ is installed.
- Optional: create and activate a virtual environment (`python3 -m venv .venv && source .venv/bin/activate`).
- Run the meeting processor (defaults to the last 7 days):  
  `python3 scripts/process_meetings.py`

## Meeting Processing
- Source: Calibre library at `/Users/kbrooks/Dropbox/Books/calibreGPT_test_lg`.
- Meetings are tagged `Meetings.YYYY-MM-DD` in `metadata.db`.
- Text is pulled from `full-text-search.db`; if absent, PDFs are attempted when `pypdf` is available.
- Outputs: Markdown logs in `logs/` (`risks.md`, `issues.md`, `tasks.md`, `development.md`).

## Customize Date Range
- Include start/end dates:  
  `python3 scripts/process_meetings.py --start 2025-11-01 --end 2025-11-30`
- Dry-run to preview counts:  
  `python3 scripts/process_meetings.py --dry-run`

## File Layout
- `scripts/` — automation and helpers.
- `logs/` — generated Markdown logs.
- `AGENTS.md` — contributor guidelines.

## Nightly Automation (cron)
- Use the helper runner: `scripts/nightly_meeting_job.sh` (includes fixed paths for Calibre and logs).
- Suggested crontab entry (runs daily at 2:15am, logs to `logs/cron.log`):
  ```
  15 2 * * * /Users/kbrooks/Dropbox/Projects/LLMscanner/scripts/nightly_meeting_job.sh
  ```
- Ensure the script is executable (`chmod +x scripts/nightly_meeting_job.sh`) and that `python3` lives at `/Library/Frameworks/Python.framework/Versions/3.11/bin/python3` (override with `PYTHON_BIN` env var in the crontab line if needed).
