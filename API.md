# SIA Agent API

Machine-payable UK private-security (SIA) regulatory **change feed** for AI agents,
built on the Wren monitor (`main.py`).

## Why a change feed, not licence lookups
The SIA public register of licence holders is **personal data**, and GOV.UK
deliberately gates it: a name search needs a date of birth, a licence search
needs the exact 16-digit number. It cannot be enumerated and is not licensed for
bulk commercial reuse. So this product sells our **own change-detection analysis**
of public GOV.UK guidance pages (OGL v3) — legal, defensible, and the thing Wren
already produces.

## Run locally
```bash
python3 main.py run          # populate state/sia_monitor.db from GOV.UK
python3 api_server.py        # HTTP API on :8402 (stdlib only)
pip install 'mcp[cli]' && python3 mcp_server.py   # MCP stdio server
```

## HTTP routes
| Route | Tier | Notes |
|---|---|---|
| `GET /` | free | Service description + pricing |
| `GET /v1/sources` | free | Monitored sources + last change time |
| `GET /v1/latest` | free | Latest 6 change events (rate-limited by IP) |
| `GET /v1/changes?since=ISO&limit=N` | keyed / x402 | Full history; 402 if unpaid |

Auth: `Authorization: Bearer <key>` (keys in the `api_keys` table). When
`X402_ENABLED=1` and no key is presented, `/v1/changes` returns HTTP 402 with
machine-readable x402 instructions (USDC on Base, `pay_to = $X402_PAY_TO`).

## Env
| Var | Default | Purpose |
|---|---|---|
| `PORT` / `HOST` | 8402 / 127.0.0.1 | bind |
| `SIA_FREE_LIMIT` | 30 | free requests / hour / IP |
| `X402_ENABLED` | 0 | turn on machine payments |
| `X402_PAY_TO` | "" | Base wallet (Oscar's Coinbase CDP) — **required to monetize** |
| `X402_PRICE_USDC` | 0.05 | price per keyed call |

## Go-live checklist (pending)
1. Oscar creates Coinbase CDP + USDC-on-Base wallet; set `X402_PAY_TO`, `X402_ENABLED=1`.
2. Deploy (openclaw host behind existing `alvento-ops` cloudflared tunnel, or Vercel + Neon).
3. Schedule `main.py run` every 6h (cron) to keep the feed fresh.
4. List in MCP registries (registry.modelcontextprotocol.io, Smithery, mcp.so) and x402 directories.
5. Serve `llms.txt` at the service root.

## Kill date
2026-09-30. If no organic paid/x402 calls by then, archive.
