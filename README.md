# LLMscanner

Utilities for extracting actionable items from meeting transcripts stored in a Calibre library.

## Quick Start
- Ensure Python 3.11+ is installed.
- Optional: create and activate a virtual environment (`python3 -m venv .venv && source .venv/bin/activate`).
- Optional: install `python-dotenv` to auto-load `.env` (`pip install python-dotenv`), or export env vars manually.
- Run the meeting processor (defaults to today only):  
  `python3 scripts/process_meetings.py`

## Meeting Processing
- Source: Calibre library at `/Users/kbrooks/Dropbox/Books/Calibre Travel Library` (override with `CALIBRE_ROOT` env var or in `.env`).
- Meetings are tagged `Meetings.YYYY-MM-DD` in `metadata.db`.
- Filters: by default, only books authored by `Tactiq` and tags starting with `Meetings.` or `Meeting.` are processed (override with `--author` and `--tag-prefix`).
- Text is pulled from `full-text-search.db`; if absent, PDFs are attempted when `pypdf` is available.
- Outputs: Markdown logs in `logs/` (`risks.md`, `issues.md`, `tasks.md`, `development.md` for grows/glows, `development_runs.md` for run history).
- After each non-dry run, PDFs are rendered in landscape alongside each log (`*.pdf`), overwriting prior PDFs. The development PDF is regrouped by person with one page per person. Requires `pandoc` + a PDF engine (e.g., `pdflatex`) installed.

## Smoke Test
- Dry-run the processor for a narrow window (defaults to today):  
  `python3 scripts/smoke_test.py`
- Include OpenAI extraction and an API ping to validate the key/connectivity:  
  `python3 scripts/smoke_test.py --llm openai --ping-openai --start 2025-11-24 --end 2025-11-25`
- `--write-logs` persists output; otherwise the smoke test leaves logs untouched.

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
  python3 scripts/process_meetings.py --llm openai --llm-model gpt-5.2
  ```
- The script defaults to keyword heuristics when `--llm` is `none`. LLM mode truncates transcripts to `--llm-max-chars` (default 20,000) to control tokens.
- If the transcript contains `AI: Behaviors`, the LLM only receives the 10,000 characters starting at that marker (fallback is the head of the transcript per `--llm-max-chars`).
- If you see `finish_reason=length`, raise the output budget with `--llm-max-output-tokens` (default 1600), e.g.:
  `python3 scripts/process_meetings.py --llm openai --llm-max-output-tokens 3000`
- Extracted types: risks, issues, tasks, and people development items: `grows` (coaching/development) and `glows` (praise). Grows/Glows live in `logs/development.md`; a run log lives in `logs/development_runs.md`.
- LLM mode requires outbound network access to `api.openai.com` and a valid `OPENAI_API_KEY` in the environment (see `.env.example`).

## LLM Debugging
- Capture the full LLM request/response payloads to disk:
  `python3 scripts/process_meetings.py --llm-debug`
- Limit debug logging to a specific meeting title (case-insensitive match):
  `python3 scripts/process_meetings.py --llm-debug --llm-debug-title "Supply Chain weekly working session #1"`
- Debug logs are written to `logs/llm_debug/` (override with `--llm-debug-dir`).
- Debug payloads include transcript excerpts and model outputs; `logs/` is gitignored.

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
