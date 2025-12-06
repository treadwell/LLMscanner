#!/usr/bin/env python3
"""Extract meeting transcripts from a Calibre library and update Markdown logs."""

from __future__ import annotations

import argparse
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


@dataclass
class Meeting:
    book_id: int
    title: str
    path: Path
    meeting_date: dt.date
    tag: str


@dataclass
class Item:
    kind: str  # "risk", "issue", "task", "grow", "glow"
    summary: str
    owner: str
    meeting: Meeting
    due: Optional[str] = None


def parse_args() -> argparse.Namespace:
    today = dt.date.today()
    default_start = today - dt.timedelta(days=7)
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
        help="Inclusive start date (YYYY-MM-DD). Defaults to 7 days ago.",
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
        help="Use an LLM for extraction instead of keyword heuristics.",
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
        help="Max characters from the transcript to send to the LLM (to control token costs).",
    )
    return parser.parse_args()


def as_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, DATE_FMT).date()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def load_system_prompt() -> str:
    """Load the LLM system prompt from disk so it can be edited without code changes."""
    try:
        return LLM_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing LLM system prompt file: {LLM_SYSTEM_PROMPT_PATH}") from exc


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
    text: str, meeting: Meeting, model: str, max_chars: int
) -> List[Item]:
    """Call OpenAI to extract items. Requires OPENAI_API_KEY."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set; cannot use LLM extraction.")
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:
        raise RuntimeError("openai package not installed") from exc

    trimmed_text = text[:max_chars]
    system_prompt = load_system_prompt()
    user_prompt = f"Meeting: {meeting.title} ({meeting.meeting_date})\nTranscript:\n{trimmed_text}"
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=800,
    )
    content = response.choices[0].message.content or "[]"

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
        return items
    for obj in data:
        if not isinstance(obj, dict):
            continue
        kind = normalize_text(obj.get("type", ""))
        if kind not in {"risk", "issue", "task", "grow", "glow"}:
            continue
        summary = obj.get("summary", "")
        owner = obj.get("owner", "Unassigned") or "Unassigned"
        due = obj.get("due") or None
        if not summary:
            continue
        items.append(
            Item(
                kind=kind,
                summary=summary.strip(),
                owner=owner.strip(),
                meeting=meeting,
                due=due.strip() if isinstance(due, str) else None,
            )
        )
    return items


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

    cmd = ["pandoc", str(input_path), "-o", str(pdf_path), "--pdf-engine=pdflatex"]
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
    meetings = load_meetings(
        metadata_db,
        calibre_root,
        start_date,
        end_date,
        tag_prefixes=tag_prefixes,
        author_filter=args.author,
    )
    if not meetings:
        prefix_label = ", ".join(tag_prefixes)
        print(f"No meetings tagged with prefixes ({prefix_label}) between {start_date} and {end_date}.")
        update_development_log(log_dir / "development.md", [], args.dry_run)
        return

    all_items: List[Item] = []
    for meeting in meetings:
        text = load_searchable_text(fts_db, meeting.book_id)
        if not text:
            pdf_path = fetch_pdf_path(metadata_db, calibre_root, meeting)
            if pdf_path:
                text = extract_from_pdf(pdf_path)
        if not text:
            print(f"Skipping {meeting.title}: no searchable text available.")
            continue
        if args.llm == "openai":
            try:
                items = llm_extract_items_openai(text, meeting, args.llm_model, args.llm_max_chars)
            except Exception as exc:
                print(f"LLM extraction failed for {meeting.title}: {exc}")
                items = []
        if not items:
            print(f"No actionable items found in {meeting.title}.")
            continue
        all_items.extend(items)

    if not all_items:
        print("No risks/issues/tasks identified.")
        update_development_log(log_dir / "development.md", meetings, args.dry_run)
        return

    # Separate items by kind
    risks = [i for i in all_items if i.kind == "risk"]
    issues = [i for i in all_items if i.kind == "issue"]
    tasks = [i for i in all_items if i.kind == "task"]
    grows = [i for i in all_items if i.kind == "grow"]
    glows = [i for i in all_items if i.kind == "glow"]

    # Load and merge risks
    risk_headers, risk_rows = load_log_table(log_dir / "risks.md")
    if not risk_headers:
        risk_headers = ["ID", "Date", "Meeting", "Owner", "Description", "Status", "Incidents"]
        risk_rows = []
    risk_rows = merge_items(risk_rows, risks, "R", risk_headers, ("Description",))

    issue_headers, issue_rows = load_log_table(log_dir / "issues.md")
    if not issue_headers:
        issue_headers = ["ID", "Date", "Meeting", "Owner", "Description", "Status", "Incidents"]
        issue_rows = []
    issue_rows = merge_items(issue_rows, issues, "I", issue_headers, ("Description",))

    task_headers, task_rows = load_log_table(log_dir / "tasks.md")
    if not task_headers:
        task_headers = ["ID", "Owner", "Description", "Meeting", "Due", "Status", "Incidents"]
        task_rows = []
    task_rows = merge_items(task_rows, tasks, "T", task_headers, ("Owner", "Description"))

    grows_headers, grows_rows, glows_headers, glows_rows = load_development_tables(log_dir / "development.md")
    if not grows_headers:
        grows_headers = ["ID", "Person", "Area"]
        grows_rows = []
    else:
        grows_headers, grows_rows = normalize_development_table(
            grows_headers,
            grows_rows,
            desc_field="Area",
            prefix="G",
        )
    if not glows_headers:
        glows_headers = ["ID", "Person", "Note"]
        glows_rows = []
    else:
        glows_headers, glows_rows = normalize_development_table(
            glows_headers,
            glows_rows,
            desc_field="Note",
            prefix="GL",
        )

    grows_rows = merge_development_items(
        grows_rows,
        grows,
        "G",
        "Area",
    )
    glows_rows = merge_development_items(
        glows_rows,
        glows,
        "GL",
        "Note",
    )

    grows_rows = sort_by_person(grows_rows, person_field="Person", date_field="Date")
    glows_rows = sort_by_person(glows_rows, person_field="Person", date_field="Date")

    if args.dry_run:
        print(
            f"[dry-run] Would write {len(risk_rows)} risks, {len(issue_rows)} issues, "
            f"{len(task_rows)} tasks, {len(grows_rows)} grows, {len(glows_rows)} glows."
        )
        update_development_log(log_dir / "development_runs.md", meetings, args.dry_run)
    else:
        write_log(log_dir / "risks.md", risk_headers, risk_rows)
        write_log(log_dir / "issues.md", issue_headers, issue_rows)
        write_log(log_dir / "tasks.md", task_headers, task_rows)
        write_development_tables(log_dir / "development.md", grows_headers, grows_rows, glows_headers, glows_rows)
        update_development_log(log_dir / "development_runs.md", meetings, args.dry_run)

        pdf_args = [
            "-V",
            "geometry=landscape",
            "--from=markdown+raw_tex",
            "-V",
            r"header-includes=\usepackage{tabularx}",
        ]
        for md_name in ("risks.md", "issues.md", "tasks.md", "development_runs.md"):
            render_pdf_from_markdown(log_dir / md_name, extra_args=pdf_args)

        # Development PDF: one page per person in landscape
        dev_person_pages = build_development_person_pages(grows_headers, grows_rows, glows_headers, glows_rows)
        render_pdf_from_markdown(
            log_dir / "development.md",
            extra_args=pdf_args,
            content=dev_person_pages,
        )
    print(
        f"Processed {len(meetings)} meeting(s): {len(risks)} risks, "
        f"{len(issues)} issues, {len(tasks)} tasks, {len(grows)} grows, {len(glows)} glows."
    )


def main() -> None:
    args = parse_args()
    try:
        process(args)
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
