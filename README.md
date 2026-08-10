# Beverage-Alcohol Web Scraper

Scrapes retail beverage-alcohol data (product, per-store pricing, reviews, store
locator) as a replacement for the paid **Bright Data** feed. Two retailers live:
**Total Wine** (deep catalog) and **Walmart** (alcohol only). Data lands in a
Postgres/Supabase schema `web_scraping`, tagged by `source`.

## How it works

Both sites are protected by **PerimeterX / HUMAN**. Plain requests get a 403
challenge, so we drive a **real Chrome via `patchright`** (stealth), which clears
PX, then read the JSON the page serves itself:

- **Total Wine** — enumerate SKUs from open sitemaps → load each product page →
  intercept `getProduct` / `reviews` / `reviews/summary`. Pricing/stock are
  per-store; the store is pinned via "Set As My Store".
- **Walmart** — enumerate the alcohol browse categories → read each product
  page's `__NEXT_DATA__`; reviews from `/reviews/product/<id>`.

Data is normalized: `product` (+ JSONB `attributes`), `product_variant` (price
per **store**, in the key), `review`, `store` (address/zip/geo), `scrape_run`,
`blocked_product`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
patchright install chromium          # stealth browser runtime
cp .env.example .env                 # set DB_URL (local docker or Supabase) + DB_SCHEMA=web_scraping
```
Needs **Google Chrome** installed (session uses `channel="chrome"`). Run
**headed** (PX blocks headless); on a headless server use `xvfb-run`.

## Commands

| Command | What it does |
|---|---|
| `init-db` | Create schema `web_scraping` + all tables (idempotent; adds missing tables) |
| `sync-stores [--source]` | Load + enrich the store locator (address/zip/phone/geo) via the browser |
| `warm [--source]` | Open the browser so you can solve the first Press & Hold once (warms the profile) |
| `run …` | Scrape products (see flags below) |
| `backfill-reviews [--source --limit --delay-s]` | Re-fetch products that have a review count but no stored reviews |
| `dashboard [--port 8000]` | Serve the local dashboard (All / per-source / Stores tabs) at localhost |
| `run-parallel …` | Multi-worker scrape (needs one proxy/IP per worker) |

## `run` flags

| Flag | Default | Meaning |
|---|---|---|
| `--source <name>` | `totalwine` | Retailer: `totalwine` or `walmart`. Stamped on every row |
| `--limit N` | none | Max products to ingest this run |
| `--no-resume` | off | Don't skip products already in the DB |
| `--retry-blocked` | off | Also retry products PX-blocked on earlier runs (not permanent exclusions) |
| `--patient` | off | Slower pace + periodic breaks so PX escalates less (unattended long runs) |
| `--interactive` | off | On a mid-run block, keep the challenge on screen and wait for **you** to solve it |
| `--fast` | off | Max speed: no pacing + block images/css/fonts (small tests; higher block risk) |
| `--delay-s S` | `1.0` | Base pause between products (adaptive; ramps up after blocks) |
| `--pause-every N` | 60 w/ `--patient` | Take a break every N products |
| `--pause-seconds S` | 240 w/ `--patient` | Length of each break |
| `--warm-wait S` | `60` | Seconds to keep the homepage up at start so you can solve a Press & Hold |
| `--solve-wait S` | `90` | Seconds to wait for a manual solve with `--interactive` |
| `--max-wait-ms MS` | `10000` | Max wait for `getProduct` before giving up on a page |
| `--max-sitemaps N` | all | Cap product sitemaps scanned (totalwine) |
| `--store <id>` | none | Pin one store — totalwine: per-store pricing; walmart: assortmentStoreId |
| `--states TX,NJ` | none | **totalwine multi-store**: scrape per-store pricing for stores in these states |
| `--stores-per-state N` | `1` | How many stores per state with `--states` |

`run-parallel`: `--limit`, `--workers`, `--proxies <url…>` (one IP per worker), `--delay-s`.

## Typical usage

```bash
docker compose up -d                              # local Postgres (or use Supabase DB_URL)
python -m scraper.cli init-db
python -m scraper.cli sync-stores                 # store locator (address/zip)

python -m scraper.cli warm                         # solve the first Press & Hold once
python -m scraper.cli run --source totalwine --patient          # long unattended run
python -m scraper.cli run --source walmart --limit 500          # walmart alcohol

# per-store pricing across states (Texas first)
python -m scraper.cli run --source totalwine --states TX,NJ,PA,CA --stores-per-state 1

python -m scraper.cli dashboard                    # http://localhost:8000
```

**Resilience:** every `run` is resumable (skips what's already captured — for
`--store`, per-store), self-heals transient PX blocks (re-warm + backoff), and
permanently skips out-of-scope (gifts/cigars/accessories) and known non-alcohol
products. Blocked ones are retried with `--retry-blocked`.

## Scaling

PerimeterX gates by IP, so one machine/IP tops out at a few hundred products
before challenges; sustained volume (the ~85k catalog × stores) needs **distinct
IPs** — either `run-parallel --proxies` (one residential IP per worker) or an
EC2 fleet (one worker per instance, run headed under `xvfb-run`).

## Layout

| Path | Purpose |
|---|---|
| `sitemaps.py` | Total Wine catalog + store enumeration, store-locator API |
| `browser.py` | `TotalWineSession`: patchright warm, navigate+intercept, `set_store`, `get_json` |
| `products.py` / `reviews.py` | Total Wine parsers (getProduct, reviews, `attributes`, `in_scope`) |
| `walmart_session.py` / `walmart_browse.py` / `walmart_parse.py` / `walmart_pipeline.py` | Walmart scraper |
| `models.py` / `db.py` | schema + Pydantic validation + idempotent upserts |
| `pipeline.py` | orchestration: run / run_stores / sync_stores / backfill_reviews |
| `dashboard.py` | local web dashboard |
| `cli.py` | entrypoint for all commands |
| `recon.py`, `probe_*.py`, `walmart_probe.py` | recon scripts (how feasibility/mechanisms were established) |

## Data / DB

`DB_URL` + `DB_SCHEMA` (in `.env`) point at local Postgres or Supabase. For
Supabase use the Session-pooler connection string with `postgresql+psycopg://`;
SSL is added automatically. Quick checks:
```sql
SELECT source, count(*) FROM web_scraping.product GROUP BY 1;
SELECT p.name, v.store_id, v.price, s.state, s.zip
FROM web_scraping.product_variant v
JOIN web_scraping.product p USING (source, product_id)
LEFT JOIN web_scraping.store s USING (source, store_id) LIMIT 20;
```

## Known gaps / open items
- Full **sale price (was/now) + "Bestseller" badges** need Total Wine's search
  API (deferred); `salesStrategy`/`on_deal` captured when present.
- **Reviews**: top ~10 per product (pagination not implemented).
- **Amazon**: no scrapable alcohol catalog (marketplace shut down 2017) — skipped.
- **Scale**: needs the residential-IP / EC2 decision for the full catalog.
