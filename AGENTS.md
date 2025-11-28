# Repository Guidelines

This repository is currently a clean slate. Use these guidelines to keep new contributions consistent, easy to review, and safe to run.

## Project Structure & Module Organization
- Prefer a simple layout: `src/` for library code, `tests/` for automated checks, `scripts/` for CLIs or one-off utilities, and `docs/` for reference material. Keep data or fixtures under `tests/fixtures/`.
- Group related modules by feature, not by type (e.g., `src/llmscanner/scanner.py` next to `src/llmscanner/config.py`).
- Avoid deeply nested packages; two to three levels is usually enough for readability.

## Build, Test, and Development Commands
- Create a virtual environment before installing dependencies:
  - `python -m venv .venv && source .venv/bin/activate`
  - `pip install -r requirements.txt` (or `pip install -e .` if a `pyproject.toml`/`setup.cfg` is added).
- Run tests locally with `pytest`. Add `-q` for faster feedback during development.
- If you introduce a build step (e.g., packaging), prefer `python -m build` so outputs land in `dist/`.

## Coding Style & Naming Conventions
- Target Python 3.11+ with type hints on all public functions. Keep modules under 500 lines where practical.
- Follow PEP 8 defaults: 4-space indentation, snake_case for functions/variables, PascalCase for classes, UPPER_CASE for constants.
- Use `ruff format` and `ruff check` (or `black`/`flake8` if those configs appear) before opening a PR to keep diffs minimal and consistent.

## Testing Guidelines
- Co-locate tests mirroring the module path (e.g., `tests/test_scanner.py` for `src/llmscanner/scanner.py`).
- Favor small, deterministic tests; use fixtures for shared setup. Name tests with intent: `test_<behavior>_<expectation>`.
- Add regression tests for every bug fix. Aim to keep coverage steady or rising; explain any intentional gaps in the PR description.

## Commit & Pull Request Guidelines
- Commit messages: short imperative subject (`Add scanner config validation`), optional body explaining intent and notable decisions. Avoid bundling unrelated changes.
- Pull requests should include: a summary of changes, testing performed (`pytest`, manual steps), any new scripts/config, and screenshots or logs when behavior changes.
- Link related issues and describe rollout risks. Flag follow-up work explicitly rather than leaving it implicit.

## Security & Configuration Tips
- Keep secrets out of the repo. Use `.env` for local-only variables and document required keys in `docs/configuration.md` (without values).
- Treat any scanning or model credentials as ephemeral; prefer environment variables over inline literals or committed files.
