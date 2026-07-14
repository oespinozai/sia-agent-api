#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
OUTPUT_DIR = ROOT / "out"
DB_PATH = STATE_DIR / "sia_monitor.db"
SOURCES_PATH = ROOT / "sources.json"
USER_AGENT = "Mozilla/5.0 (compatible; UKSIAIntelMVP/0.2; +https://gov.uk)"
TIMEOUT = 30


@dataclass(frozen=True)
class Source:
    slug: str
    name: str
    url: str
    tags: list[str]
    buyer_relevance: list[str]


@dataclass
class Snapshot:
    fetched_at: str
    title: str
    meta_description: str
    updated_at: str | None
    content_hash: str
    summary: str
    headline_items: list[str]
    tags: list[str]
    url: str


class GovUkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.meta_description = ""
        self.main_parts: list[str] = []
        self.in_title = False
        self.in_main = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag == "main":
            self.in_main = True
        elif tag == "meta" and attrs_dict.get("name") == "description":
            self.meta_description = attrs_dict.get("content", "") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag == "main":
            self.in_main = False

    def handle_data(self, data: str) -> None:
        cleaned = normalize_whitespace(data)
        if not cleaned:
            return
        if self.in_title:
            self.title_parts.append(cleaned)
        if self.in_main:
            self.main_parts.append(cleaned)


KEYWORD_TAGS = {
    "renew": "renewal",
    "renewal": "renewal",
    "licence": "licensing",
    "license": "licensing",
    "training": "training",
    "qualification": "training",
    "check security staff have a licence": "licence-check",
    "close protection": "close-protection",
    "apply": "application",
    "changes": "change-notice",
    "private security": "private-security",
    "refresher": "refresher",
    "licensing decisions": "policy-change",
}

DEFAULT_PROFILE = {
    "profile_name": "uk-private-security-operator",
    "entity_name": "Example Security Ltd",
    "priority_tags": [
        "renewal",
        "training",
        "licensing",
        "close-protection",
        "change-notice",
        "refresher",
        "policy-change",
    ],
    "keywords": [
        "renew",
        "refresher",
        "training",
        "licence",
        "close protection",
        "application",
        "licensing decisions",
    ],
}


def normalize_whitespace(text: str) -> str:
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_sources() -> list[Source]:
    data = json.loads(SOURCES_PATH.read_text())
    return [Source(**item) for item in data]


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_slug TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            title TEXT NOT NULL,
            meta_description TEXT,
            updated_at TEXT,
            content_hash TEXT NOT NULL,
            summary TEXT NOT NULL,
            headline_items_json TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            url TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_source_slug ON snapshots(source_slug, fetched_at DESC);
        """
    )
    return conn


def fetch_html(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def extract_updated_at(raw_html: str, main_text: str) -> str | None:
    patterns = [
        r'Updated:\s*</[^>]+>\s*<time[^>]*>([^<]+)</time>',
        r'Updated:\s*([^<\n]+)',
        r'Last updated\s*([^<\n]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_html, flags=re.IGNORECASE)
        if match:
            return normalize_whitespace(match.group(1))
    match = re.search(r"Updated:\s*([^\n]+)", main_text, flags=re.IGNORECASE)
    if match:
        return normalize_whitespace(match.group(1))
    return None


def extract_headline_items(main_text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", main_text)
    cleaned = []
    for sentence in sentences:
        sentence = normalize_whitespace(sentence)
        if 30 <= len(sentence) <= 240:
            cleaned.append(sentence)
        if len(cleaned) == 6:
            break
    return cleaned


def classify_tags(source: Source, title: str, summary: str, headline_items: Iterable[str]) -> list[str]:
    corpus = " ".join([title, summary, *headline_items]).lower()
    tags = set(source.tags)
    for needle, tag in KEYWORD_TAGS.items():
        if needle in corpus:
            tags.add(tag)
    return sorted(tags)


def build_summary(meta_description: str, main_text: str) -> str:
    if meta_description:
        return normalize_whitespace(meta_description)
    trimmed = normalize_whitespace(main_text)
    return trimmed[:280] + ("..." if len(trimmed) > 280 else "")


def parse_snapshot(source: Source, raw_html: str) -> Snapshot:
    parser = GovUkParser()
    parser.feed(raw_html)
    title = normalize_whitespace(" ".join(parser.title_parts)).replace(" - GOV.UK", "")
    main_text = normalize_whitespace(" ".join(parser.main_parts))
    summary = build_summary(parser.meta_description, main_text)
    headline_items = extract_headline_items(main_text)
    updated_at = extract_updated_at(raw_html, main_text)
    tags = classify_tags(source, title, summary, headline_items)
    content_basis = json.dumps(
        {
            "title": title,
            "summary": summary,
            "updated_at": updated_at,
            "headline_items": headline_items,
            "tags": tags,
        },
        sort_keys=True,
    )
    content_hash = hashlib.sha256(content_basis.encode()).hexdigest()
    return Snapshot(
        fetched_at=now_iso(),
        title=title or source.name,
        meta_description=parser.meta_description,
        updated_at=updated_at,
        content_hash=content_hash,
        summary=summary,
        headline_items=headline_items,
        tags=tags,
        url=source.url,
    )


def get_latest_snapshot(conn: sqlite3.Connection, source_slug: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshots WHERE source_slug = ? ORDER BY id DESC LIMIT 1",
        (source_slug,),
    ).fetchone()


def store_snapshot(conn: sqlite3.Connection, source: Source, snapshot: Snapshot) -> None:
    conn.execute(
        """
        INSERT INTO snapshots (
            source_slug, fetched_at, title, meta_description, updated_at,
            content_hash, summary, headline_items_json, tags_json, url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source.slug,
            snapshot.fetched_at,
            snapshot.title,
            snapshot.meta_description,
            snapshot.updated_at,
            snapshot.content_hash,
            snapshot.summary,
            json.dumps(snapshot.headline_items),
            json.dumps(snapshot.tags),
            snapshot.url,
        ),
    )
    conn.commit()


def snapshot_from_row(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        fetched_at=row["fetched_at"],
        title=row["title"],
        meta_description=row["meta_description"] or "",
        updated_at=row["updated_at"],
        content_hash=row["content_hash"],
        summary=row["summary"],
        headline_items=json.loads(row["headline_items_json"]),
        tags=json.loads(row["tags_json"]),
        url=row["url"],
    )


def diff_snapshot(old: Snapshot | None, new: Snapshot) -> dict:
    if old is None:
        return {
            "status": "new-source",
            "reason": "First snapshot stored",
            "changed_fields": ["baseline"],
        }
    changed_fields = []
    if old.updated_at != new.updated_at:
        changed_fields.append("updated_at")
    if old.title != new.title:
        changed_fields.append("title")
    if old.summary != new.summary:
        changed_fields.append("summary")
    if old.content_hash != new.content_hash:
        changed_fields.append("content_hash")
    if old.tags != new.tags:
        changed_fields.append("tags")
    status = "changed" if changed_fields else "unchanged"
    return {
        "status": status,
        "reason": ", ".join(changed_fields) if changed_fields else "No material change detected",
        "changed_fields": changed_fields,
    }


def score_priority(tags: Iterable[str], profile: dict) -> tuple[int, list[str]]:
    matched = sorted(set(tags).intersection(profile["priority_tags"]))
    score = len(matched)
    return score, matched


def build_events() -> tuple[str, dict, list[dict]]:
    profile = DEFAULT_PROFILE
    sources = load_sources()
    conn = connect_db()
    events: list[dict] = []
    for source in sources:
        raw_html = fetch_html(source.url)
        new_snapshot = parse_snapshot(source, raw_html)
        old_row = get_latest_snapshot(conn, source.slug)
        old_snapshot = snapshot_from_row(old_row) if old_row else None
        diff = diff_snapshot(old_snapshot, new_snapshot)
        if diff["status"] != "unchanged":
            store_snapshot(conn, source, new_snapshot)
        score, matched = score_priority(new_snapshot.tags, profile)
        events.append(
            {
                "source": source,
                "snapshot": new_snapshot,
                "diff": diff,
                "priority_score": score,
                "matched_priority_tags": matched,
            }
        )
    run_at = now_iso()
    return run_at, profile, events


def render_markdown_report(run_at: str, events: list[dict], profile: dict, changed_only: bool = False) -> str:
    material_events = [event for event in events if event["diff"]["status"] != "unchanged"]
    displayed = material_events if changed_only else events
    lines = [
        "# UK SIA Intelligence Report",
        "",
        f"Generated: {run_at}",
        f"Profile: {profile['profile_name']}",
        "",
        f"Monitored sources: {len(events)}",
        f"Material events: {len(material_events)}",
        "",
    ]
    if changed_only and not material_events:
        lines.append("No material changes detected.")
        lines.append("")
        return "\n".join(lines)
    for event in sorted(displayed, key=lambda item: (-item["priority_score"], item["source"].name)):
        snapshot = event["snapshot"]
        diff = event["diff"]
        lines.extend(
            [
                f"## {event['source'].name}",
                f"- URL: {event['source'].url}",
                f"- Status: {diff['status']}",
                f"- Reason: {diff['reason']}",
                f"- Updated on page: {snapshot.updated_at or 'unknown'}",
                f"- Tags: {', '.join(snapshot.tags)}",
                f"- Priority score: {event['priority_score']}",
                f"- Matched priority tags: {', '.join(event['matched_priority_tags']) or 'none'}",
                f"- Summary: {snapshot.summary}",
            ]
        )
        if snapshot.headline_items:
            lines.append("- Key extracted lines:")
            for item in snapshot.headline_items:
                lines.append(f"  - {item}")
        lines.append("")
    if material_events:
        lines.extend(["## Action queue", ""])
        for event in sorted(material_events, key=lambda item: -item["priority_score"]):
            lines.append(
                f"- [{event['diff']['status']}] {event['source'].name}: {event['diff']['reason']}"
            )
        lines.append("")
    return "\n".join(lines)


def render_telegram_alert(run_at: str, events: list[dict], profile: dict) -> str:
    material_events = [event for event in events if event["diff"]["status"] != "unchanged"]
    if not material_events:
        return ""
    lines = [
        "## UK SIA monitor alert",
        f"Generated: {run_at}",
        f"Profile: {profile['profile_name']}",
        "",
    ]
    for event in sorted(material_events, key=lambda item: (-item["priority_score"], item["source"].name)):
        snapshot = event["snapshot"]
        lines.extend(
            [
                f"- **{event['source'].name}**",
                f"  - Status: {event['diff']['status']}",
                f"  - Reason: {event['diff']['reason']}",
                f"  - Page updated: {snapshot.updated_at or 'unknown'}",
                f"  - Priority: {event['priority_score']}",
                f"  - Summary: {snapshot.summary}",
                f"  - URL: {event['source'].url}",
            ]
        )
    return "\n".join(lines)


def save_outputs(run_at: str, events: list[dict], profile: dict) -> None:
    ensure_dirs()
    report = render_markdown_report(run_at, events, profile, changed_only=False)
    changed_report = render_markdown_report(run_at, events, profile, changed_only=True)
    alert_text = render_telegram_alert(run_at, events, profile)
    material_events = [event for event in events if event["diff"]["status"] != "unchanged"]
    (OUTPUT_DIR / "latest_report.md").write_text(report)
    (OUTPUT_DIR / "latest_changes.md").write_text(changed_report)
    (OUTPUT_DIR / "latest_alert.txt").write_text(alert_text)
    payload = {
        "generated_at": run_at,
        "profile": profile,
        "material_event_count": len(material_events),
        "events": [
            {
                "source": event["source"].__dict__,
                "priority_score": event["priority_score"],
                "matched_priority_tags": event["matched_priority_tags"],
                "diff": event["diff"],
                "snapshot": event["snapshot"].__dict__,
            }
            for event in events
        ],
    }
    (OUTPUT_DIR / "latest_report.json").write_text(json.dumps(payload, indent=2))


def cmd_run(args: argparse.Namespace) -> int:
    run_at, profile, events = build_events()
    save_outputs(run_at, events, profile)
    print(render_markdown_report(run_at, events, profile, changed_only=args.changed_only))
    return 0


def cmd_watch(_: argparse.Namespace) -> int:
    run_at, profile, events = build_events()
    save_outputs(run_at, events, profile)
    alert_text = render_telegram_alert(run_at, events, profile)
    if alert_text:
        print(alert_text)
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    conn = connect_db()
    rows = conn.execute(
        "SELECT source_slug, title, updated_at, fetched_at FROM snapshots ORDER BY source_slug, id DESC"
    ).fetchall()
    if not rows:
        print("No snapshots stored yet. Run `python3 main.py run` first.")
        return 0
    print("Stored snapshots:\n")
    for row in rows:
        print(
            f"- {row['source_slug']}: {row['title']} | page updated={row['updated_at'] or 'unknown'} | fetched={row['fetched_at']}"
        )
    return 0


def cmd_sources(_: argparse.Namespace) -> int:
    for source in load_sources():
        print(f"- {source.name}\n  slug: {source.slug}\n  url: {source.url}\n  tags: {', '.join(source.tags)}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="UK SIA/private security intelligence MVP monitor"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Fetch sources, diff them, and emit a report")
    run_parser.add_argument(
        "--changed-only",
        action="store_true",
        help="Only print sources with material changes",
    )
    run_parser.set_defaults(func=cmd_run)

    watch_parser = subparsers.add_parser(
        "watch", help="Silent on no change; prints an alert only when material changes are detected"
    )
    watch_parser.set_defaults(func=cmd_watch)

    status_parser = subparsers.add_parser("status", help="Show stored snapshot status")
    status_parser.set_defaults(func=cmd_status)

    sources_parser = subparsers.add_parser("sources", help="List configured monitored sources")
    sources_parser.set_defaults(func=cmd_sources)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except urllib.error.URLError as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Unhandled error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
