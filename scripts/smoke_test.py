#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None


def parse_args() -> argparse.Namespace:
    today = dt.date.today()
    default_start = default_end = (today - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description="Lightweight smoke test for meeting processing.")
    parser.add_argument("--start", default=default_start, help="Start date (YYYY-MM-DD). Defaults to yesterday.")
    parser.add_argument("--end", default=default_end, help="End date (YYYY-MM-DD). Defaults to yesterday.")
    parser.add_argument(
        "--llm",
        choices=["none", "openai"],
        default="none",
        help="Extraction mode passed through to process_meetings.",
    )
    parser.add_argument("--llm-model", default="gpt-4o-mini", help="LLM model when using OpenAI.")
    parser.add_argument(
        "--ping-openai",
        action="store_true",
        help="Send a minimal OpenAI chat completion to verify API key and connectivity.",
    )
    parser.add_argument(
        "--write-logs",
        action="store_true",
        help="Persist output logs (default is dry-run so logs are untouched).",
    )
    parser.add_argument("--calibre-root", type=Path, default=None, help="Override CALIBRE_ROOT if needed.")
    parser.add_argument("--log-dir", type=Path, default=None, help="Override log directory for the run.")
    return parser.parse_args()


def maybe_load_env() -> None:
    if load_dotenv:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def ping_openai(model: str) -> bool:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OpenAI ping skipped: OPENAI_API_KEY not set.")
        return False
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:
        print(f"OpenAI ping skipped: openai package not installed ({exc}).")
        return False
    client = OpenAI(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0,
        )
        print(f"OpenAI ping ok (model={model}, id={resp.id})")
        return True
    except Exception as exc:  # pragma: no cover - external call
        print(f"OpenAI ping failed: {exc}")
        return False


def run_processor(args: argparse.Namespace) -> int:
    script_path = Path(__file__).resolve().parent / "process_meetings.py"
    cmd = [
        sys.executable,
        str(script_path),
        "--start",
        args.start,
        "--end",
        args.end,
        "--llm",
        args.llm,
        "--llm-model",
        args.llm_model,
    ]
    if not args.write_logs:
        cmd.append("--dry-run")
    if args.calibre_root:
        cmd.extend(["--calibre-root", str(args.calibre_root)])
    if args.log_dir:
        cmd.extend(["--log-dir", str(args.log_dir)])

    print("Running:", " ".join(cmd))
    return subprocess.call(cmd)


def main() -> None:
    args = parse_args()
    maybe_load_env()
    if args.ping_openai:
        ping_openai(args.llm_model)
    exit_code = run_processor(args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
