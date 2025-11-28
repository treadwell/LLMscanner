#!/usr/bin/env python3
"""Extract meeting transcripts from a Calibre library and update Markdown logs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

CALIBRE_ROOT_DEFAULT = Path("/Users/kbrooks/Dropbox/Books/calibreGPT_test_lg")
MEETING_TAG_PREFIXES_DEFAULT = ("Meetings.", "Meeting.")
DATE_FMT = "%Y-%m-%d"


@dataclass
class Meeting:
    book_id: int
    title: str
    path: Path
    meeting_date: dt.date
    tag: str


@dataclass
class Item:
    kind: str  # "risk", "issue", "task", "development"
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
        help="Calibre library root containing metadata.db and full-text-search.db.",
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
        default="none",
        help="Use an LLM for extraction instead of keyword heuristics.",
    )
    parser.add_argument(
        "--llm-model",
        default="gpt-4o-mini",
        help="LLM model name (when --llm=openai).",
    )
    parser.add_argument(
        "--llm-max-chars",
        type=int,
        default=12000,
        help="Max characters from the transcript to send to the LLM (to control token costs).",
    )
    return parser.parse_args()


def as_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, DATE_FMT).date()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


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


def capture_owner(text: str) -> str:
    owner_patterns = [
        r"(?:owner|assignee|lead)[:\-]\s*([A-Z][A-Za-z]+\s*[A-Za-z]*)",
        r"^([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s*:\s",
        r"@([A-Z][A-Za-z]+)",
    ]
    for pat in owner_patterns:
        match = re.search(pat, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return "Unassigned"


def split_sentences(text: str) -> List[str]:
    lines = []
    for block in text.splitlines():
        block = block.strip()
        if not block:
            continue
        lines.extend(re.split(r"(?<=[.!?])\s+|\s*[\r\n]+", block))
    sentences = [ln.strip() for ln in lines if len(ln.strip()) > 12]
    return sentences


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
    system_prompt = (
        "Extract actionable items from the provided meeting transcript. "
        "Return JSON ONLY: an array of objects with fields: "
        "`type` (risk|issue|task|development), `summary`, `owner` (person or 'Unassigned'), "
        "`due` (optional date or empty string). Keep summaries concise and concrete."
    )
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
        if kind not in {"risk", "issue", "task", "development"}:
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


def classify_sentences(sentences: Iterable[str], meeting: Meeting) -> List[Item]:
    task_kw = ("action", "follow up", "follow-up", "todo", "task", "next step", "next steps", "takeaway")
    risk_kw = ("risk", "concern", "blocker", "dependency", "exposure", "mitigation")
    issue_kw = ("issue", "problem", "bug", "error", "failing", "outage")
    dev_kw = ("coaching", "training", "mentorship", "feedback", "growth")
    items: List[Item] = []
    seen: set[Tuple[str, str]] = set()

    for sentence in sentences:
        s_norm = normalize_text(sentence)
        label = None
        if any(k in s_norm for k in risk_kw):
            label = "risk"
        elif any(k in s_norm for k in issue_kw):
            label = "issue"
        elif any(k in s_norm for k in task_kw):
            label = "task"
        elif any(k in s_norm for k in dev_kw):
            label = "development"
        if not label:
            continue
        key = (label, s_norm)
        if key in seen:
            continue
        seen.add(key)
        owner = capture_owner(sentence)
        summary = sentence.strip()
        items.append(Item(kind=label, summary=summary, owner=owner, meeting=meeting))
    return items


def load_log_table(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    if not path.exists():
        return [], []
    lines = path.read_text(encoding="utf-8").splitlines()
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


def next_id(rows: Sequence[Dict[str, str]], prefix: str) -> str:
    highest = 0
    for row in rows:
        ident = row.get("ID", "")
        if ident.startswith(f"{prefix}-"):
            try:
                val = int(ident.split("-", 1)[1])
                highest = max(highest, val)
            except ValueError:
                continue
    return f"{prefix}-{highest + 1:04d}"


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
        print(f"No meetings tagged {MEETING_TAG_PREFIX} between {start_date} and {end_date}.")
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
        sentences = split_sentences(text)
        if args.llm == "openai":
            try:
                items = llm_extract_items_openai(text, meeting, args.llm_model, args.llm_max_chars)
            except Exception as exc:
                print(f"LLM extraction failed for {meeting.title}: {exc}")
                items = []
        else:
            items = classify_sentences(sentences, meeting)
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
    devs = [i for i in all_items if i.kind == "development"]

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

    dev_headers, dev_rows = load_log_table(log_dir / "development_opportunities.md")
    if not dev_headers:
        dev_headers = ["ID", "Date", "Person", "Area", "Meeting", "Status", "Incidents"]
        dev_rows = []
    dev_rows = merge_items(
        dev_rows,
        devs,
        "D",
        dev_headers,
        ("Area",),
        owner_field="Person",
        desc_field="Area",
        meeting_field="Meeting",
    )

    if args.dry_run:
        print(f"[dry-run] Would write {len(risk_rows)} risks, {len(issue_rows)} issues, {len(task_rows)} tasks.")
    else:
        write_log(log_dir / "risks.md", risk_headers, risk_rows)
        write_log(log_dir / "issues.md", issue_headers, issue_rows)
        write_log(log_dir / "tasks.md", task_headers, task_rows)
        write_log(log_dir / "development_opportunities.md", dev_headers, dev_rows)

    update_development_log(log_dir / "development.md", meetings, args.dry_run)
    print(
        f"Processed {len(meetings)} meeting(s): {len(risks)} risks, "
        f"{len(issues)} issues, {len(tasks)} tasks, {len(devs)} development items."
    )


def main() -> None:
    args = parse_args()
    try:
        process(args)
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
