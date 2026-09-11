# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Japan→North America Amazon arbitrage research tool: find products cheap on Amazon.co.jp that
resell at a meaningful markup on Amazon.com, using the Keepa API for pricing/rank data. The
codebase has two generations living side by side:

- **Legacy pipeline** (`main.py`, `us_amazon.py`, `jp_amazon.py`, `fetch_keepa.py`,
  `parse_*.py`): scrapes US Amazon search results, cross-references JP price by scraping
  Amazon.co.jp. Fragile (HTML scraping, IP-block risk) and effectively superseded — treat as
  reference/legacy, not the primary system to extend.
- **Current system** (`keepa_mcp/` + `daily_scan.py` + `ops_finance.py` +
  `send_daily_digest.py` + `keepa-csv-dashboard/`): Keepa-API-only, no scraping. This is what's
  actively developed and deployed. New work should go here unless explicitly asked otherwise.

## Commands

Root Python environment:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in KEEPA_API_KEY etc.
```

Tests (root-level `unittest`/`pytest` suite — `test_fba_agent.py`, `test_notify.py`):
```bash
make test                       # creates .venv, installs requirements-dev.txt, runs pytest
.venv/bin/python -m pytest -q   # if the venv/deps already exist
.venv/bin/python -m pytest test_notify.py -q          # single file
.venv/bin/python -m pytest test_fba_agent.py::TestMapKeepaProduct -q   # single class
```
There is no lint config in this repo (no ruff/flake8/eslint config committed) — don't invent one.

Current agent pipeline (the thing actually worth running):
```bash
.venv/bin/python daily_scan.py                     # one scan cycle: pool keyword -> Keepa search -> real-margin filter -> save (no immediate notify)
.venv/bin/python daily_scan.py --seed-from-favorites   # pull keyword candidates from favorites (free, no Keepa tokens)
.venv/bin/python daily_scan.py --expand "HARIO"        # expand one keyword via Keepa category tree (costs tokens)
.venv/bin/python daily_scan.py --list-keywords         # inspect the keyword pool
./scripts/run_all_day.sh                            # loop daily_scan.py continuously, paced to the token refill rate
.venv/bin/python send_daily_digest.py --dry-run     # preview the LINE digest without sending
```
Note the `mcp` package requires `.venv/bin/python`, not bare `python3` — the system interpreter
doesn't have it installed, so anything importing `keepa_mcp` (`daily_scan.py`,
`send_daily_digest.py`) must run through the venv.

Dashboard (React + Vite, dev only — talks to `sqlite_api_server.py` on :8001 via the Vite proxy):
```bash
cd keepa-csv-dashboard && npm install && npm run dev
python3 sqlite_api_server.py     # separate terminal; stdlib http.server, no deps beyond stdlib
```
Production build (`npm run build`) is served directly by `sqlite_api_server.py` itself if
`keepa-csv-dashboard/dist/` exists — see "Deployment" below. `sqlite_api_server.py` deliberately
avoids `python-dotenv` (parses `.env` itself) since it's meant to run under plain system
`python3` where dev-only deps may be absent.

MCP server standalone (mostly for debugging — normally Claude Code loads it automatically via
`.mcp.json`):
```bash
.venv/bin/python -m keepa_mcp.server
```

## Architecture

### `keepa_mcp/` — the Keepa API layer, exposed as both an MCP server and a plain library
- `keepa_client.py`: raw Keepa HTTP calls + token-cost estimators. Notable landmines already
  paid for so you don't have to re-discover them: Product Finder needs `perPage>=50` or Keepa
  400s; `offers=0` 400s (omit the param entirely instead); `buybox=1` is **5 tokens/product
  instead of 1** (only used narrowly in `find_seller_for_candidate`, never in the main pipeline).
- `cached_ops.py`: per-key SQLite-cached wrappers (`keepa_mcp/cache.sqlite3`, gitignored) around
  every `keepa_client` call, with distinct TTLs per data kind (`config.py`: product/finder
  6h, category 30d, seller 24h). Every wrapper returns `(data, cache_info)` so callers can tell
  fresh from cached.
- `analysis.py`: pure functions turning a raw Keepa product dict into the metrics the pipeline
  cares about (price, volatility, rank, weight, fees, seller id from `buyBoxSellerIdHistory`).
- `server.py`: the MCP tools (`check_token_balance`, `search_category`, `expand_keyword`,
  `find_candidates`, `find_arbitrage_candidates`, `find_seller_for_candidate`,
  `expand_from_seller`, etc.). `find_arbitrage_candidates` (US Finder search -> per-ASIN detail
  -> JP cross-check by ASIN -> price-gap filter) and `expand_from_seller` (a seller's storefront
  ASINs -> same evaluation, skipping the Finder step) share their per-ASIN evaluation logic via
  `_fetch_sell_products`/`_evaluate_sell_products` — extend both by editing those, not by
  duplicating the loop again.
- Cross-domain product matching is **by ASIN**, not UPC/EAN, per explicit product decision — ASINs
  aren't guaranteed to be shared across marketplaces, so a real fraction of candidates get
  correctly dropped as "not found in JP catalog." This is known and intentional, not a bug to fix.
- `wait_for_tokens=True` (used by `daily_scan.py`/cron, not interactive use) polls the real
  balance and blocks rather than failing on 429 — necessary because the token bucket refill rate
  can be as low as 1/min with a 60-token cap, so a single search can outrun the whole bucket.

### `ops_finance.py` — the real-money layer on top of raw Keepa data
Owns `keepa-csv-dashboard/keepa_imports.sqlite3` (shared with the dashboard backend — see below)
and turns `keepa_mcp` output into decisions:
- `evaluate_mcp_candidates()`: applies FBA/referral fees + international shipping to get real
  unit profit, not just the gross Keepa price gap; also computes real profit for coarse-filter
  rejects when both prices were already fetched (not for reasons where they weren't — that's a
  cost/completeness tradeoff, not an oversight).
- Keyword pool (`keyword_pool` table): `pick_next_keyword()`/`record_keyword_used()` drive
  `daily_scan.py`'s keyword selection (least-recently-used). Seeded from favorites' brand/category
  fields, filtered against Amazon's own top-level department names and Japanese-script text
  (`_is_searchable_keyword`) — both are useless as Keepa title-search terms and were verified live
  to return zero Finder results / never match.
- `agent_candidates`/`agent_runs`: what the dashboard's Agent page reads. A run's `status` column
  (`running`/`completed`/`failed`) is written *before* the Keepa calls start, then updated — this
  is how the dashboard shows a live "search in progress" state.
- Digest (`digest_state` table + `build_daily_digest_message`): `daily_scan.py` never sends a
  notification itself; `send_daily_digest.py` (cron, twice daily) sends everything accumulated
  since the last digest in one LINE message. This split was an explicit decision — don't
  reintroduce per-run notifications.

### `keepa-csv-dashboard/` — React dashboard, stdlib Python backend
Frontend (`src/*.jsx`) talks only to `sqlite_api_server.py`'s `/api/*` routes (`src/db.js`), never
to Keepa directly. Four pages: CSV import analysis (`App.jsx`), manual Keepa Finder search
(`KeepaFinderPage.jsx`), the automated agent's findings (`AgentPage.jsx` — human reviews and
manually promotes to favorites, no auto-favoriting by design), and the keyword pool
(`KeywordPoolPage.jsx`). `sqlite_api_server.py` is the single source of truth for the DB schema
on the dashboard side and duplicates `ops_finance.py`'s table definitions/migrations
independently (same tables, same `keepa_imports.sqlite3` file, two Python entry points that don't
import each other) — when changing a shared table's schema, update both places.

### Deployment
Runs on an AWS Lightsail instance (small, ~412MB RAM — mind memory when adding always-on
processes) via systemd: `fba-dashboard.service` (`sqlite_api_server.py`, also serves
`keepa-csv-dashboard/dist/` directly when present, so no separate Node/nginx process is needed
for the SPA itself — nginx in front is just for Basic Auth) and `fba-scan-loop.service`
(`scripts/run_all_day.sh`, loops `daily_scan.py` paced to the Keepa refill rate). `send_daily_digest.py`
runs from cron, not systemd. The scan loop's "stop" is soft by design: a flag file
(`.scan_loop_stop_requested`, checked between cycles) prevents the *next* cycle rather than
killing an in-flight one, so tokens already spent aren't wasted — the dashboard's stop/resume
buttons and `control_scan_loop()` in `sqlite_api_server.py` implement this, not `systemctl stop`.
