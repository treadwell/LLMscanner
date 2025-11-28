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
- Filters: by default, only books authored by `Tactiq` and tags starting with `Meetings.` or `Meeting.` are processed (override with `--author` and `--tag-prefix`).
- Text is pulled from `full-text-search.db`; if absent, PDFs are attempted when `pypdf` is available.
- Outputs: Markdown logs in `logs/` (`risks.md`, `issues.md`, `tasks.md`, `development.md` for grows/glows, `development_runs.md` for run history).

## Customize Date Range
- Include start/end dates:  
  `python3 scripts/process_meetings.py --start 2025-11-01 --end 2025-11-30`
- Dry-run to preview counts:  
  `python3 scripts/process_meetings.py --dry-run`
- Override tag/author filters, e.g.:
  `python3 scripts/process_meetings.py --author Tactiq --tag-prefix Meetings. --tag-prefix Meeting.`

## Optional LLM Extraction
- Enable LLM extraction (improves signal from noisy transcripts):
  ```
  OPENAI_API_KEY=sk-... \
  python3 scripts/process_meetings.py --llm openai --llm-model gpt-4o-mini
  ```
- The script defaults to keyword heuristics when `--llm` is `none`. LLM mode truncates transcripts to `--llm-max-chars` (default 12,000) to control tokens.
- Extracted types: risks, issues, tasks, and people development items: `grows` (coaching/development) and `glows` (praise). Grows/Glows live in `logs/development.md`; a run log lives in `logs/development_runs.md`.
- LLM mode requires outbound network access to `api.openai.com` and a valid `OPENAI_API_KEY` in the environment (see `.env.example`).

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
