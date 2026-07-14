# SIA Agent API

UK SIA private-security regulatory change feed for AI agents. Serves
structured JSON over an HTTP API and as an MCP stdio server, both
sourced from official GOV.UK open-data pages (Open Government Licence v3).

Live HTTP API: https://sia.alvento.uk

## What it does

- Monitors a curated set of GOV.UK and SIA-related sources
- Fetches page title, update date, summary and tags
- Stores snapshots in SQLite and diffs against the last stored snapshot
- Exposes the change feed as JSON over HTTP and as MCP tools

## Files

- `main.py` - runner, fetcher, parser, diff engine
- `api_server.py` - HTTP API on `:8402` (Python stdlib only)
- `mcp_server.py` - MCP stdio server (see below)
- `watchdog.py` - silent entrypoint for cron-style automation
- `sources.json` - monitored sources
- `state/` - SQLite snapshots (gitignored, created on first run)

## MCP server

`mcp_server.py` is a stdio MCP server that mirrors the HTTP API. It
reads the same SQLite database the runner writes; no extra network calls.

Install the SDK and run:

```bash
pip install 'mcp[cli]>=1.2.0'
python3 mcp_server.py
```

Three tools are exposed:

- `list_sources` - the GOV.UK and SIA sources monitored for private-security
  compliance changes, plus the last time a change was detected on each.
- `recent_changes(limit=10)` - the most recent detected changes. Each item
  has source, detection time, page updated time, summary, tags and source URL.
- `changes_since(iso_date, limit=50)` - changes detected on or after an
  ISO-8601 date (e.g. `2026-06-01`).

All tools return JSON strings. Schema is defined by the tool docstrings.

## Run locally

```bash
python3 main.py run            # populate state/sia_monitor.db from GOV.UK
python3 api_server.py          # HTTP API on :8402 (stdlib only)
python3 watchdog.py            # silent; prints nothing if no material changes
```

## Env

| Var | Default | Purpose |
|---|---|---|
| `PORT` / `HOST` | 8402 / 127.0.0.1 | bind |
| `SIA_FREE_LIMIT` | 30 | free requests per hour per IP |
| `X402_ENABLED` | 0 | turn on machine payments |
| `X402_PAY_TO` | (empty) | Base wallet (required to monetize) |
| `X402_PRICE_USDC` | 0.05 | price per keyed call |

## Auth

API keys live in the `api_keys` table. Send as `Authorization: Bearer <KEY>`. When `X402_ENABLED=1` and no key is presented, `/v1/changes` returns HTTP 402 with a machine-readable x402 envelope (USDC on Base, pay-to address read from `X402_PAY_TO`).

## Data sources

Curated UK GOV.UK and SIA pages. See `sources.json` for the current list.
Each entry carries source slug, source URL, page updated date, summary and
tags. Open data licence: Open Government Licence v3.0.
