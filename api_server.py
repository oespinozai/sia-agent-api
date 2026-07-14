#!/usr/bin/env python3
"""SIA regulatory change-feed API for autonomous agents.

Serves the change-detection feed produced by the Wren monitor (main.py) over
HTTP as JSON. This is our own derived analysis of public GOV.UK guidance pages
(Crown copyright, OGL v3) — NOT a resale of the SIA register of licence holders,
which is personal data and deliberately built to prevent bulk reuse.

Payment model:
  - Free tier: rate-limited by IP, latest snapshot + source list.
  - Keyed tier: full change history, higher limits. Keys live in the api_keys
    table (issued out of band, e.g. by the Stripe fulfilment hook or manually).
  - x402 tier: when X402_ENABLED=1 and no valid key is presented, protected
    routes answer HTTP 402 with machine-readable payment instructions so an
    agent wallet can pay per call. Disabled until a Coinbase CDP / Base wallet
    address is configured via X402_PAY_TO.

Zero third-party dependencies: stdlib http.server + sqlite3 only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "state" / "sia_monitor.db"

FREE_TIER_LIMIT = int(os.environ.get("SIA_FREE_LIMIT", "30"))  # requests / window / IP
RATE_WINDOW_SECONDS = 3600
X402_ENABLED = os.environ.get("X402_ENABLED", "0") == "1"
X402_PAY_TO = os.environ.get("X402_PAY_TO", "")  # Base wallet address (Oscar's CDP)
X402_PRICE_USDC = os.environ.get("X402_PRICE_USDC", "0.05")

_hits: dict[str, list[float]] = {}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS api_keys (
            key TEXT PRIMARY KEY,
            label TEXT,
            created_at TEXT,
            active INTEGER DEFAULT 1
        )"""
    )
    return conn


def valid_key(conn: sqlite3.Connection, key: str | None) -> bool:
    if not key:
        return False
    row = conn.execute(
        "SELECT 1 FROM api_keys WHERE key = ? AND active = 1", (key,)
    ).fetchone()
    return row is not None


def rate_ok(ip: str) -> bool:
    now = time.time()
    window = _hits.setdefault(ip, [])
    window[:] = [t for t in window if now - t < RATE_WINDOW_SECONDS]
    if len(window) >= FREE_TIER_LIMIT:
        return False
    window.append(now)
    return True


def snapshot_rows(conn: sqlite3.Connection, since: str | None, limit: int) -> list[dict]:
    sql = "SELECT source_slug, fetched_at, title, updated_at, summary, tags_json, url, content_hash FROM snapshots"
    params: list = []
    if since:
        sql += " WHERE fetched_at >= ?"
        params.append(since)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in conn.execute(sql, params).fetchall():
        out.append(
            {
                "source": r["source_slug"],
                "detected_at": r["fetched_at"],
                "title": r["title"],
                "page_updated": r["updated_at"],
                "summary": r["summary"],
                "tags": json.loads(r["tags_json"]),
                "url": r["url"],
                "content_hash": r["content_hash"],
            }
        )
    return out


def distinct_sources(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT source_slug, title, url, MAX(fetched_at) AS last_seen
           FROM snapshots GROUP BY source_slug ORDER BY source_slug"""
    ).fetchall()
    return [
        {"source": r["source_slug"], "title": r["title"], "url": r["url"], "last_change": r["last_seen"]}
        for r in rows
    ]


class Handler(BaseHTTPRequestHandler):
    server_version = "sia-agent-api/0.1"

    def _send(self, code: int, payload: dict, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return None

    def _payment_required(self) -> None:
        # x402: machine-readable 402 an agent wallet can act on.
        self._send(
            402,
            {
                "error": "payment_required",
                "message": "Full change history requires payment or an API key.",
                "x402": {
                    "enabled": X402_ENABLED,
                    "scheme": "exact",
                    "network": "base",
                    "asset": "USDC",
                    "amount": X402_PRICE_USDC,
                    "pay_to": X402_PAY_TO or None,
                },
                "docs": "/",
            },
            extra_headers={"WWW-Authenticate": "Bearer"},
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        conn = db()
        try:
            if route == "/" or route == "/health":
                self._send(
                    200,
                    {
                        "service": "sia-agent-api",
                        "description": "UK private-security (SIA) regulatory change feed for AI agents.",
                        "data_basis": "Derived change detection over public GOV.UK guidance (OGL v3). Not the SIA register of licence holders.",
                        "routes": {
                            "GET /v1/sources": "Monitored GOV.UK sources and last change time (free).",
                            "GET /v1/latest": "Most recent change events, capped (free, rate-limited).",
                            "GET /v1/changes?since=ISO8601&limit=N": "Full change history (API key or x402).",
                        },
                        "pricing": {"unit": "per call", "usdc": X402_PRICE_USDC, "x402_enabled": X402_ENABLED},
                    },
                )
                return

            if route == "/llms.txt":
                path = ROOT / "llms.txt"
                if path.exists():
                    body = path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._send(404, {"error": "not_found", "path": route})
                return

            if route == "/v1/sources":
                if not rate_ok(ip):
                    self._send(429, {"error": "rate_limited", "retry_after_seconds": RATE_WINDOW_SECONDS})
                    return
                self._send(200, {"sources": distinct_sources(conn)})
                return

            if route == "/v1/latest":
                if not rate_ok(ip):
                    self._send(429, {"error": "rate_limited", "retry_after_seconds": RATE_WINDOW_SECONDS})
                    return
                self._send(200, {"changes": snapshot_rows(conn, since=None, limit=6), "tier": "free"})
                return

            if route == "/v1/changes":
                key = self._bearer()
                if not valid_key(conn, key):
                    self._payment_required()
                    return
                since = qs.get("since", [None])[0]
                try:
                    limit = min(int(qs.get("limit", ["100"])[0]), 500)
                except ValueError:
                    limit = 100
                self._send(200, {"changes": snapshot_rows(conn, since, limit), "tier": "keyed"})
                return

            self._send(404, {"error": "not_found", "path": route})
        finally:
            conn.close()

    def log_message(self, *_args) -> None:  # quiet default logging
        pass


def main() -> None:
    port = int(os.environ.get("PORT", "8402"))
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"sia-agent-api listening on http://{host}:{port} (x402_enabled={X402_ENABLED})")
    server.serve_forever()


if __name__ == "__main__":
    main()
