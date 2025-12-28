#!/usr/bin/env python3
"""Extract meeting transcripts from a Calibre library and update Markdown logs."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from html import unescape
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None

CALIBRE_ROOT_DEFAULT = Path(
    os.getenv("CALIBRE_ROOT", "/Users/kbrooks/Dropbox/Books/Calibre Travel Library")
)
MEETING_TAG_PREFIXES_DEFAULT = ("Meetings",)
DATE_FMT = "%Y-%m-%d"
REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
LLM_SYSTEM_PROMPT_PATH = PROMPTS_DIR / "llm_extraction_system.txt"
LLM_USER_PROMPT_PATH = PROMPTS_DIR / "llm_extraction_user.txt"
LLM_RISKS_PROMPT_PATH = PROMPTS_DIR / "risks.txt"
LLM_ISSUES_PROMPT_PATH = PROMPTS_DIR / "issues.txt"
LLM_TASKS_PROMPT_PATH = PROMPTS_DIR / "tasks.txt"
LLM_DEVELOPMENT_PROMPT_PATH = PROMPTS_DIR / "development.txt"
PANDOC_PDF_ENGINE = os.getenv("PANDOC_PDF_ENGINE", "xelatex")


@dataclass
class Meeting:
    book_id: int
    title: str
    path: Path
    meeting_date: dt.date
    tag: str


@dataclass
class Item:
    kind: str  # "grow" or "glow"
    summary: str
    owner: str
    meeting: Meeting
    due: Optional[str] = None
    behavior: Optional[str] = None


def parse_args() -> argparse.Namespace:
    today = dt.date.today()
    default_start = today
    parser = argparse.ArgumentParser(
        description="Scan meeting transcripts in Calibre and update Markdown logs."
    )
    parser.add_argument(
        "--calibre-root",
        type=Path,
        default=CALIBRE_ROOT_DEFAULT,
        help="Calibre library root containing metadata.db and full-text-search.db (defaults to CALIBRE_ROOT env var or the built-in path).",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=default_start.strftime(DATE_FMT),
        help="Inclusive start date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=today.strftime(DATE_FMT),
        help="Inclusive end date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "logs",
        help="Directory for Markdown logs.",
    )
    parser.add_argument(
        "--author",
        default="Tactiq",
        help="Only process meetings where at least one author matches this name (case sensitive). Use '' to disable.",
    )
    parser.add_argument(
        "--tag-prefix",
        action="append",
        dest="tag_prefixes",
        default=None,
        help="Tag prefix for meetings (repeat for multiple). Defaults to 'Meetings.' and 'Meeting.'.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without writing log files.",
    )
    parser.add_argument(
        "--llm",
        choices=["none", "openai"],
        default="openai",
        help="Use an LLM for development extraction (grow/glow) instead of keyword heuristics.",
    )
    parser.add_argument(
        "--llm-model",
        default="gpt-5.1",
        help="LLM model name (when --llm=openai).",
    )
    parser.add_argument(
        "--llm-max-chars",
        type=int,
        default=20000,
        help="Max characters from the transcript to send to the LLM (to control token costs). Use 0 for no limit.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include diagnostic output for each meeting.",
    )
    return parser.parse_args()


def as_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, DATE_FMT).date()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def load_system_prompt() -> str:
    """Load the LLM system prompt plus category prompts so they can be edited without code changes."""
    try:
        base = LLM_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
        development = LLM_DEVELOPMENT_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        missing = getattr(exc, "filename", str(exc))
        raise RuntimeError(f"Missing LLM prompt file: {missing}") from exc

    sections = [
        base,
        "\n[DEVELOPMENT]\n" + development,
    ]
    return "\n".join(sections)


def build_user_prompt(meeting: Meeting, transcript: str) -> str:
    """Load and format the user prompt for the LLM call."""
    try:
        template = LLM_USER_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing LLM user prompt file: {LLM_USER_PROMPT_PATH}") from exc
    try:
        return template.format(
            meeting_title=meeting.title,
            meeting_date=meeting.meeting_date,
            transcript=transcript,
        )
    except KeyError as exc:
        raise RuntimeError(f"LLM user prompt template is missing placeholder: {exc}") from exc


def load_meetings(
    metadata_db: Path,
    calibre_root: Path,
    start: dt.date,
    end: dt.date,
    tag_prefixes: Sequence[str],
    author_filter: Optional[str],
) -> List[Meeting]:
    conn = sqlite3.connect(metadata_db)
    cur = conn.cursor()
    like_clauses = " OR ".join("t.name LIKE ?" for _ in tag_prefixes)
    params: List[str] = [f"{p}%" for p in tag_prefixes]
    author_clause = ""
    if author_filter:
        author_clause = "AND a.name = ?"
        params.append(author_filter)
    rows = cur.execute(
        f"""
        SELECT DISTINCT b.id, b.title, b.path, t.name
        FROM books b
        JOIN books_tags_link btl ON b.id = btl.book
        JOIN tags t ON t.id = btl.tag
        LEFT JOIN books_authors_link bal ON b.id = bal.book
        LEFT JOIN authors a ON bal.author = a.id
        WHERE ({like_clauses}) {author_clause}
        """,
        params,
    ).fetchall()
    meetings: List[Meeting] = []
    for book_id, title, rel_path, tag in rows:
        tag_date = tag.split(".", 1)[-1]
        try:
            meeting_date = as_date(tag_date)
        except ValueError:
            continue
        if meeting_date < start or meeting_date > end:
            continue
        meeting_path = calibre_root / rel_path
        meetings.append(
            Meeting(
                book_id=int(book_id),
                title=title,
                path=meeting_path,
                meeting_date=meeting_date,
                tag=tag,
            )
        )
    conn.close()
    return meetings


def fetch_pdf_path(metadata_db: Path, calibre_root: Path, meeting: Meeting) -> Optional[Path]:
    conn = sqlite3.connect(metadata_db)
    cur = conn.cursor()
    row = cur.execute(
        "SELECT name, format FROM data WHERE book = ? AND format = 'PDF' LIMIT 1",
        (meeting.book_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    name, fmt = row
    filename = f"{name}.{fmt.lower()}"
    pdf_path = meeting.path / filename
    return pdf_path if pdf_path.exists() else None


def load_searchable_text(fts_db: Path, book_id: int) -> Optional[str]:
    conn = sqlite3.connect(fts_db)
    cur = conn.cursor()
    row = cur.execute(
        """
        SELECT searchable_text
        FROM books_text
        WHERE book = ? AND searchable_text IS NOT NULL AND searchable_text != ''
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (book_id,),
    ).fetchone()
    conn.close()
    if row:
        return row[0]
    return None


def extract_from_pdf(pdf_path: Path) -> Optional[str]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None
    try:
        reader = PdfReader(str(pdf_path))
        chunks = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text:
                chunks.append(text)
        return "\n".join(chunks) if chunks else None
    except Exception:
        return None


def llm_extract_items_openai(
    text: str,
    meeting: Meeting,
    model: str,
    max_chars: int,
    *,
    verbose: bool = False,
) -> List[Item]:
    """Call OpenAI to extract items. Requires OPENAI_API_KEY."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set; cannot use LLM extraction.")
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            f"openai import failed using interpreter {sys.executable}: {exc!r}"
        ) from exc

    if max_chars > 0:
        trimmed_text = text[:max_chars]
        if verbose and len(text) > max_chars:
            print(
                f"Trimming transcript for {meeting.title}: {len(text)} -> {max_chars} chars."
            )
    else:
        trimmed_text = text
    system_prompt = load_system_prompt()
    user_prompt = build_user_prompt(meeting, trimmed_text)
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_completion_tokens=800,
    )
    if not response.choices:
        raise RuntimeError("LLM returned no choices.")
    choice = response.choices[0]
    content = choice.message.content
    if content is None or not content.strip():
        finish_reason = getattr(choice, "finish_reason", "unknown")
        raise RuntimeError(f"LLM returned empty content (finish_reason={finish_reason}).")
    if verbose:
        response_id = getattr(response, "id", "unknown")
        finish_reason = getattr(choice, "finish_reason", "unknown")
        print(
            f"LLM response received for {meeting.title}: id={response_id}, "
            f"finish_reason={finish_reason}, chars={len(content)}."
        )

    def strip_code_fences(raw: str) -> str:
        fenced = raw.strip()
        if fenced.startswith("```") and fenced.endswith("```"):
            fenced = fenced.strip("`")
            # Drop optional language hint like ```json
            parts = fenced.split("\n", 1)
            if len(parts) == 2:
                return parts[1]
        return raw

    content = strip_code_fences(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM returned non-JSON content: {content}") from exc

    items: List[Item] = []
    if not isinstance(data, list):
        if verbose:
            print(
                f"LLM response for {meeting.title} is {type(data).__name__}, expected a list."
            )
        return items
    raw_count = len(data)
    skipped_non_dict = 0
    skipped_unknown_kind = 0
    skipped_missing_summary = 0
    for obj in data:
        if not isinstance(obj, dict):
            skipped_non_dict += 1
            continue
        kind = normalize_text(obj.get("type", ""))
        if kind not in {"grow", "glow"}:
            skipped_unknown_kind += 1
            continue
        summary = obj.get("summary", "")
        owner = obj.get("owner", "Unassigned") or "Unassigned"
        due = obj.get("due") or None
        behavior = obj.get("behavior") or None
        if not summary:
            skipped_missing_summary += 1
            continue
        items.append(
            Item(
                kind=kind,
                summary=summary.strip(),
                owner=owner.strip(),
                meeting=meeting,
                due=due.strip() if isinstance(due, str) else None,
                behavior=behavior.strip() if isinstance(behavior, str) else None,
            )
        )
    if verbose:
        print(
            f"LLM parsed {raw_count} item(s) for {meeting.title}: "
            f"accepted={len(items)}, skipped_non_dict={skipped_non_dict}, "
            f"skipped_unknown_kind={skipped_unknown_kind}, "
            f"skipped_missing_summary={skipped_missing_summary}."
        )
    return items


def append_development_csv_by_person(items: Sequence[Item], log_dir: Path) -> int:
    csv_dir = log_dir / "development_by_person"
    csv_dir.mkdir(parents=True, exist_ok=True)
    expected_headers = ["Person", "Date", "Meeting", "Kind", "Behavior", "Summary"]
    legacy_headers = ["Person", "Date", "Behavior", "Summary", "Meeting"]
    appended = 0
    for item in items:
        person = item.owner or "Unassigned"
        safe_name = re.sub(r"[^A-Za-z0-9]+", "_", person.strip()).strip("_") or "Unassigned"
        csv_path = csv_dir / f"{safe_name}.csv"

        def normalize_kind(value: str) -> str:
            lowered = value.strip().lower()
            if lowered == "grow":
                return "Grow"
            if lowered == "glow":
                return "Glow"
            return value.strip().title()

        def normalize_behavior(value: Optional[str]) -> str:
            if not value:
                return ""
            cleaned = value.strip()
            if not cleaned:
                return ""
            mapping = {
                "student": "Student",
                "teacher": "Teacher",
                "community": "Community",
                "company": "Company",
            }
            return mapping.get(cleaned.lower(), cleaned)

        def split_legacy_behavior(value: str) -> Tuple[str, str]:
            raw = value.strip()
            if not raw:
                return "", ""
            if ":" in raw:
                kind_part, behavior_part = raw.split(":", 1)
                return normalize_kind(kind_part), normalize_behavior(behavior_part)
            return normalize_kind(raw), ""

        existing_headers: Optional[List[str]] = None
        if csv_path.exists() and csv_path.stat().st_size > 0:
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                existing_headers = next(reader, [])
            if existing_headers == legacy_headers:
                legacy_rows: List[List[str]] = []
                with csv_path.open("r", encoding="utf-8", newline="") as handle:
                    reader = csv.reader(handle)
                    next(reader, None)
                    for row in reader:
                        if not row:
                            continue
                        padded = list(row) + [""] * (len(legacy_headers) - len(row))
                        if len(padded) < len(legacy_headers):
                            continue
                        person_val, date_val, legacy_behavior, summary, meeting = padded[:5]
                        kind_val, behavior_val = split_legacy_behavior(legacy_behavior)
                        legacy_rows.append(
                            [person_val, date_val, meeting, kind_val, behavior_val, summary]
                        )
                temp_path = csv_path.with_suffix(".csv.tmp")
                with temp_path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(expected_headers)
                    writer.writerows(legacy_rows)
                temp_path.replace(csv_path)
                existing_headers = expected_headers
            elif existing_headers != expected_headers:
                print(
                    f"Warning: {csv_path} has unexpected headers {existing_headers}; "
                    "appending with the new format."
                )

        write_header = not csv_path.exists() or csv_path.stat().st_size == 0
        if existing_headers == expected_headers:
            write_header = False

        kind_label = normalize_kind(item.kind)
        behavior_value = normalize_behavior(item.behavior)
        with csv_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            if write_header:
                writer.writerow(expected_headers)
            writer.writerow(
                [
                    person,
                    item.meeting.meeting_date.strftime(DATE_FMT),
                    item.meeting.title,
                    kind_label,
                    behavior_value,
                    item.summary,
                ]
            )
        appended += 1
    return appended


def load_log_table(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    if not path.exists():
        return [], []
    return load_log_table_from_lines(path.read_text(encoding="utf-8").splitlines())


def load_log_table_from_lines(lines: Sequence[str]) -> Tuple[List[str], List[Dict[str, str]]]:
    text = "\n".join(lines)
    if "<table" in text:
        return load_html_table(text)
    headers: List[str] = []
    rows: List[Dict[str, str]] = []
    for line in lines:
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not headers:
            headers = cells
            continue
        if set(cells) == {"---"}:
            continue
        if len(cells) != len(headers):
            continue
        rows.append(dict(zip(headers, cells)))
    return headers, rows


def load_html_table(text: str) -> Tuple[List[str], List[Dict[str, str]]]:
    headers: List[str] = []
    rows: List[Dict[str, str]] = []
    row_pat = re.compile(r"<tr>(.*?)</tr>", flags=re.DOTALL | re.IGNORECASE)
    cell_pat = re.compile(r"<t[hd]>(.*?)</t[hd]>", flags=re.DOTALL | re.IGNORECASE)

    def clean(cell: str) -> str:
        cell_no_tags = re.sub(r"<.*?>", "", cell)
        return unescape(cell_no_tags).strip()

    for idx, row_html in enumerate(row_pat.findall(text)):
        cells = [clean(c) for c in cell_pat.findall(row_html)]
        if not cells:
            continue
        if idx == 0:
            headers = cells
            continue
        rows.append(dict(zip(headers, cells)))
    return headers, rows


def next_id(rows: Sequence[Dict[str, str]], prefix: str, *, pad: bool = True) -> str:
    highest = 0
    for row in rows:
        ident = row.get("ID", "")
        if ident.startswith(f"{prefix}-"):
            try:
                val = int(ident.split("-", 1)[1])
                highest = max(highest, val)
            except ValueError:
                continue
    number = highest + 1
    return f"{prefix}-{number:04d}" if pad else f"{prefix}-{number}"


def normalize_development_table(
    headers: Sequence[str],
    rows: Sequence[Dict[str, str]],
    *,
    desc_field: str,
    meeting_field: str = "Meeting",
    date_field: str = "Date",
    prefix: str,
) -> Tuple[List[str], List[Dict[str, str]]]:
    """Transform legacy development tables to the simplified schema."""
    target_headers = ["ID", "Person", desc_field]
    normalized: List[Dict[str, str]] = []

    def pad_id(ident: str) -> str:
        if ident.startswith(f"{prefix}-"):
            suffix = ident.split("-", 1)[1]
            try:
                suffix_int = int(suffix)
                return f"{prefix}-{suffix_int:04d}"
            except ValueError:
                return ident
        return ident

    for row in rows:
        summary = row.get(desc_field, "")
        meeting = row.get(meeting_field, "")
        date = row.get(date_field, "")
        extra_parts = [part for part in (meeting, date) if part]
        if extra_parts:
            summary = f"{summary} ({', '.join(extra_parts)})"
        normalized.append(
            {
                "ID": pad_id(row.get("ID", "")),
                "Person": row.get("Person", ""),
                desc_field: summary,
            }
        )

    # Backfill IDs if missing (keeps any existing IDs unchanged).
    for row in normalized:
        if not row.get("ID"):
            row["ID"] = next_id(normalized, prefix, pad=True)

    return target_headers, normalized


def merge_development_items(
    rows: List[Dict[str, str]],
    items: List[Item],
    prefix: str,
    desc_field: str,
) -> List[Dict[str, str]]:
    """Merge grow/glow items into the simplified development tables."""
    if not rows:
        rows = []
    existing_index: Dict[Tuple[str, str], Dict[str, str]] = {}
    for row in rows:
        key = (
            normalize_text(row.get(desc_field, "")),
            normalize_text(row.get("Person", "")),
        )
        existing_index[key] = row

    for item in items:
        summary = item.summary
        append_parts = [item.meeting.title, item.meeting.meeting_date.strftime(DATE_FMT)]
        meeting_suffix = ", ".join(p for p in append_parts if p)
        if meeting_suffix:
            summary = f"{summary} ({meeting_suffix})"
        key = (normalize_text(summary), normalize_text(item.owner))
        if key in existing_index:
            row = existing_index[key]
            row["Person"] = item.owner
            row[desc_field] = summary
        else:
            new_row = {
                "ID": next_id(rows, prefix, pad=True),
                "Person": item.owner,
                desc_field: summary,
            }
            rows.append(new_row)
            existing_index[key] = new_row
    return rows




def render_html_table(
    headers: Sequence[str],
    rows: Sequence[Dict[str, str]],
    col_widths: Sequence[int],
) -> str:
    """Render an HTML table with explicit column widths."""
    col_elems = [
        f'<col style="width:{width}%">' for width in col_widths
    ]
    parts = [
        "<table>",
        "<colgroup>",
        *col_elems,
        "</colgroup>",
        "<thead>",
        "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>",
        "</thead>",
        "<tbody>",
    ]
    for row in rows:
        parts.append("<tr>" + "".join(f"<td>{row.get(h, '')}</td>" for h in headers) + "</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def latex_escape(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    escaped = value
    for src, repl in replacements.items():
        escaped = escaped.replace(src, repl)
    return escaped


def render_latex_table(
    headers: Sequence[str],
    rows: Sequence[Dict[str, str]],
    col_widths: Sequence[float],
) -> str:
    """Render a LaTeX tabularx with explicit column widths (fractions of linewidth)."""
    col_spec = "".join(f"p{{{width}\\linewidth}}" for width in col_widths)
    lines = [
        r"\begin{tabularx}{\linewidth}{" + col_spec + "}",
        " & ".join(r"\textbf{" + latex_escape(h) + "}" for h in headers) + r" \\",
        r"\hline",
    ]
    for row in rows:
        cells = [latex_escape(row.get(h, "")) for h in headers]
        lines.append(" & ".join(cells) + r" \\")
    lines.append(r"\end{tabularx}")
    return "\n".join(lines)


def merge_items(
    rows: List[Dict[str, str]],
    items: List[Item],
    prefix: str,
    headers: List[str],
    key_fields: Tuple[str, ...],
    owner_field: str = "Owner",
    desc_field: str = "Description",
    meeting_field: str = "Meeting",
) -> List[Dict[str, str]]:
    if not headers:
        return rows
    existing_index: Dict[Tuple[str, ...], Dict[str, str]] = {}
    for row in rows:
        key = tuple(normalize_text(row.get(field, "")) for field in key_fields)
        existing_index[key] = row

    for item in items:
        candidate = {
            "ID": "",
            "Date": item.meeting.meeting_date.strftime(DATE_FMT),
            meeting_field: item.meeting.title,
            owner_field: item.owner,
            desc_field: item.summary,
            "Status": "open",
            "Incidents": "1",
        }
        if item.kind == "task":
            candidate[meeting_field] = f"{item.meeting.title} ({item.meeting.tag})"
            if item.due and "Due" in headers:
                candidate["Due"] = item.due
        key = tuple(normalize_text(candidate.get(field, "")) for field in key_fields)
        if key in existing_index:
            row = existing_index[key]
            incidents = int(row.get("Incidents", "0") or "0")
            row["Incidents"] = str(incidents + 1)
            row["Date"] = candidate["Date"]
            row["Meeting"] = candidate["Meeting"]
        else:
            candidate["ID"] = next_id(rows + list(existing_index.values()), prefix)
            rows.append(candidate)
            existing_index[key] = candidate
    return rows


def write_log(path: Path, headers: Sequence[str], rows: Sequence[Dict[str, str]]) -> None:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        line = "| " + " | ".join(row.get(h, "") for h in headers) + " |"
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_pdf_from_markdown(
    md_path: Path,
    *,
    output_path: Optional[Path] = None,
    extra_args: Optional[Sequence[str]] = None,
    content: Optional[str] = None,
) -> None:
    if content is None and not md_path.exists():
        return
    pdf_path = output_path or md_path.with_suffix(".pdf")
    input_path = md_path
    temp_file: Optional[tempfile.NamedTemporaryFile] = None

    if content is not None:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".md")
        temp_file.write(content.encode("utf-8"))
        temp_file.flush()
        input_path = Path(temp_file.name)

    cmd = ["pandoc", str(input_path), "-o", str(pdf_path), f"--pdf-engine={PANDOC_PDF_ENGINE}"]
    if extra_args:
        cmd.extend(extra_args)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(f"Wrote PDF: {pdf_path}")
    except FileNotFoundError:
        print("Skipping PDF generation: pandoc not available.")
    except subprocess.CalledProcessError as exc:  # pragma: no cover - external tool
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        print(f"PDF generation failed for {pdf_path.name}: {stderr}")
    finally:
        if temp_file:
            try:
                Path(temp_file.name).unlink()
            except Exception:
                pass


def sort_by_person(rows: Sequence[Dict[str, str]], person_field: str = "Person", date_field: str = "Date") -> List[Dict[str, str]]:
    def parse_date(value: str) -> dt.date:
        try:
            return dt.datetime.strptime(value, DATE_FMT).date()
        except Exception:
            return dt.date.min

    return sorted(
        rows,
        key=lambda r: (
            normalize_text(r.get(person_field, "")),
            -parse_date(r.get(date_field, "")).toordinal(),
            r.get("ID", ""),
        ),
    )


def build_development_person_pages(
    grows_headers: Sequence[str],
    grows_rows: Sequence[Dict[str, str]],
    glows_headers: Sequence[str],
    glows_rows: Sequence[Dict[str, str]],
) -> str:
    # Gather unique people
    people = sorted(
        {normalize_text(row.get("Person", "")) for row in grows_rows + glows_rows if row.get("Person")}
    )
    header = "# Development by Person\n"
    pages: List[str] = [header]

    def build_table(headers: Sequence[str], rows: Sequence[Dict[str, str]]) -> List[str]:
        if not rows:
            return ["_None_"]
        return [render_latex_table(headers, rows, col_widths=(0.08, 0.18, 0.74))]

    for idx, person_norm in enumerate(people):
        person_label = next(
            (row.get("Person") for row in grows_rows + glows_rows if normalize_text(row.get("Person", "")) == person_norm),
            person_norm,
        )
        pages.append(f"## {person_label}")
        person_grows = [row for row in grows_rows if normalize_text(row.get("Person", "")) == person_norm]
        person_glows = [row for row in glows_rows if normalize_text(row.get("Person", "")) == person_norm]

        pages.append("### Grows")
        pages.extend(build_table(grows_headers, person_grows))
        pages.append("\n### Glows")
        pages.extend(build_table(glows_headers, person_glows))
        if idx < len(people) - 1:
            pages.append("\n\\newpage\n")
    return "\n".join(pages)


def load_development_tables(path: Path) -> Tuple[List[str], List[Dict[str, str]], List[str], List[Dict[str, str]]]:
    if not path.exists():
        return [], [], [], []
    lines = path.read_text(encoding="utf-8").splitlines()
    grows_headers: List[str] = []
    glows_headers: List[str] = []
    grows_rows: List[Dict[str, str]] = []
    glows_rows: List[Dict[str, str]] = []
    current_section: Optional[str] = None
    buffer: List[str] = []

    def flush_buffer(section: Optional[str], buf: List[str]) -> None:
        nonlocal grows_headers, glows_headers, grows_rows, glows_rows
        if not section or not buf:
            return
        text_block = "\n".join(buf)
        if "<table" in text_block:
            headers, rows = load_html_table(text_block)
        else:
            headers, rows = load_log_table_from_lines(buf)
        if section == "Grows":
            grows_headers, grows_rows = headers, rows
        elif section == "Glows":
            glows_headers, glows_rows = headers, rows

    for line in lines:
        if line.startswith("## "):
            flush_buffer(current_section, buffer)
            current_section = line.replace("##", "").strip()
            buffer = []
            continue
        if current_section in {"Grows", "Glows"}:
            buffer.append(line)
    flush_buffer(current_section, buffer)
    return grows_headers, grows_rows, glows_headers, glows_rows


def write_development_tables(
    path: Path,
    grows_headers: Sequence[str],
    grows_rows: Sequence[Dict[str, str]],
    glows_headers: Sequence[str],
    glows_rows: Sequence[Dict[str, str]],
) -> None:
    sections = []
    sections.append("# Development\n")
    sections.append("## Grows")
    sections.append(render_html_table(grows_headers, grows_rows, col_widths=(10, 20, 70)))

    sections.append("\n## Glows")
    sections.append(render_html_table(glows_headers, glows_rows, col_widths=(10, 20, 70)))

    path.write_text("\n".join(sections) + "\n", encoding="utf-8")


def update_development_log(path: Path, meetings: List[Meeting], dry_run: bool) -> None:
    headers, rows = load_log_table(path)
    if not headers:
        headers = ["Run Date", "Meetings Processed", "Notes"]
        rows = []
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    meeting_titles = ", ".join(m.title for m in meetings) if meetings else "None"
    note = f"Processed {len(meetings)} meeting(s)"
    rows.append({"Run Date": today, "Meetings Processed": meeting_titles, "Notes": note})
    if not dry_run:
        write_log(path, headers, rows)


def process(args: argparse.Namespace) -> None:
    if load_dotenv:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    calibre_root = args.calibre_root
    metadata_db = calibre_root / "metadata.db"
    fts_db = calibre_root / "full-text-search.db"
    log_dir = args.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    start_date = as_date(args.start)
    end_date = as_date(args.end)
    tag_prefixes = args.tag_prefixes or list(MEETING_TAG_PREFIXES_DEFAULT)
    prefix_label = ", ".join(tag_prefixes)
    author_label = args.author or "any"
    print(
        f"Processing meetings from {start_date} to {end_date} "
        f"(tags={prefix_label}, author={author_label})."
    )
    print(
        f"LLM mode: {args.llm} (model={args.llm_model}, max_chars={args.llm_max_chars}), "
        f"dry_run={args.dry_run}."
    )
    if args.verbose:
        print(f"Calibre root: {calibre_root}")
        print(f"Log dir: {log_dir}")

    meetings = load_meetings(
        metadata_db,
        calibre_root,
        start_date,
        end_date,
        tag_prefixes=tag_prefixes,
        author_filter=args.author,
    )
    if not meetings:
        print(f"No meetings tagged with prefixes ({prefix_label}) between {start_date} and {end_date}.")
        return
    print(f"Found {len(meetings)} meeting(s) matching filters.")

    development_items: List[Item] = []
    for meeting in meetings:
        text_source = "fts"
        pdf_path: Optional[Path] = None
        text = load_searchable_text(fts_db, meeting.book_id)
        if not text:
            pdf_path = fetch_pdf_path(metadata_db, calibre_root, meeting)
            if pdf_path:
                text = extract_from_pdf(pdf_path)
                text_source = "pdf" if text else "pdf-empty"
            else:
                text_source = "none"
        if not text:
            if text_source == "pdf-empty" and pdf_path:
                print(f"Skipping {meeting.title}: PDF found at {pdf_path} but no text extracted.")
            elif text_source == "none":
                print(f"Skipping {meeting.title}: no searchable text or PDF available.")
            else:
                print(f"Skipping {meeting.title}: no searchable text available.")
            continue
        text_len = len(text)
        if args.verbose:
            print(
                f"Meeting {meeting.title} ({meeting.meeting_date}, tag={meeting.tag}, "
                f"source={text_source}, chars={text_len})."
            )
        items: List[Item] = []
        if args.llm == "openai":
            try:
                items = llm_extract_items_openai(
                    text,
                    meeting,
                    args.llm_model,
                    args.llm_max_chars,
                    verbose=args.verbose,
                )
            except Exception as exc:
                print(
                    f"LLM extraction failed for {meeting.title} "
                    f"(source={text_source}, chars={text_len}): {exc}"
                )
                items = []
        elif args.verbose:
            print(f"LLM extraction disabled (llm={args.llm}); skipping {meeting.title}.")
        if not items:
            llm_note = (
                f"llm={args.llm_model}" if args.llm == "openai" else f"llm={args.llm}"
            )
            print(
                f"No development items found in {meeting.title} "
                f"(date={meeting.meeting_date}, source={text_source}, chars={text_len}, {llm_note})."
            )
            continue
        if args.verbose:
            kind_counts = {kind: 0 for kind in ("grow", "glow")}
            for item in items:
                kind_counts[item.kind] += 1
            counts_label = ", ".join(
                f"{kind}={count}" for kind, count in kind_counts.items() if count
            )
            if counts_label:
                print(
                    f"Extracted {len(items)} development item(s) from {meeting.title} ({counts_label})."
                )
            else:
                print(f"Extracted {len(items)} development item(s) from {meeting.title}.")
        development_items.extend(items)

    if not development_items:
        print("No development items identified.")
        return
    if args.dry_run:
        print(f"[dry-run] Would append {len(development_items)} development item(s).")
        return
    appended = append_development_csv_by_person(development_items, log_dir)
    csv_dir = log_dir / "development_by_person"
    print(
        f"Processed {len(meetings)} meeting(s): appended {appended} development item(s) "
        f"to {csv_dir}."
    )


def main() -> None:
    args = parse_args()
    try:
        process(args)
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
