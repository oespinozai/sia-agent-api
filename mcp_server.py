#!/usr/bin/env python3
"""MCP server exposing the SIA regulatory change feed to AI agents.

Run:  pip install "mcp[cli]"  &&  python3 mcp_server.py
Registers as an MCP stdio server with three tools that mirror the HTTP API.
Reads the same SQLite the Wren monitor writes; no network calls of its own.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency. Install with: pip install 'mcp[cli]'"
    ) from exc

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "state" / "sia_monitor.db"

mcp = FastMCP("sia-agent-api")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@mcp.tool()
def list_sources() -> str:
    """List the UK GOV.UK / SIA sources monitored for private-security compliance changes."""
    conn = _conn()
    rows = conn.execute(
        """SELECT source_slug, title, url, MAX(fetched_at) AS last_change
           FROM snapshots GROUP BY source_slug ORDER BY source_slug"""
    ).fetchall()
    conn.close()
    return json.dumps(
        [
            {"source": r["source_slug"], "title": r["title"], "url": r["url"], "last_change": r["last_change"]}
            for r in rows
        ],
        indent=2,
    )


@mcp.tool()
def recent_changes(limit: int = 10) -> str:
    """Return the most recent detected changes to UK private-security (SIA) regulatory guidance.

    Each item is our own change-detection analysis of a public GOV.UK page:
    source, when we detected the change, the page's own 'last updated' date,
    a summary, topical tags, and the source URL.
    """
    limit = max(1, min(limit, 100))
    conn = _conn()
    rows = conn.execute(
        """SELECT source_slug, fetched_at, title, updated_at, summary, tags_json, url
           FROM snapshots ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return json.dumps(
        [
            {
                "source": r["source_slug"],
                "detected_at": r["fetched_at"],
                "title": r["title"],
                "page_updated": r["updated_at"],
                "summary": r["summary"],
                "tags": json.loads(r["tags_json"]),
                "url": r["url"],
            }
            for r in rows
        ],
        indent=2,
    )


@mcp.tool()
def changes_since(iso_date: str, limit: int = 50) -> str:
    """Return SIA regulatory changes detected on or after an ISO-8601 date (e.g. 2026-06-01)."""
    limit = max(1, min(limit, 200))
    conn = _conn()
    rows = conn.execute(
        """SELECT source_slug, fetched_at, title, updated_at, summary, url
           FROM snapshots WHERE fetched_at >= ? ORDER BY id DESC LIMIT ?""",
        (iso_date, limit),
    ).fetchall()
    conn.close()
    return json.dumps(
        [
            {
                "source": r["source_slug"],
                "detected_at": r["fetched_at"],
                "title": r["title"],
                "page_updated": r["updated_at"],
                "summary": r["summary"],
                "url": r["url"],
            }
            for r in rows
        ],
        indent=2,
    )


if __name__ == "__main__":
    mcp.run()
