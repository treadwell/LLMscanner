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
