"""Command-line entrypoint.

    python -m scraper.cli init-db                 # create schema + tables
    python -m scraper.cli sync-stores             # load store directory (curl)
    python -m scraper.cli run --limit 200         # scrape product data (browser)
    python -m scraper.cli run                      # full catalog (~85k)
"""

from __future__ import annotations

import argparse
import logging


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="scraper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create schema + tables")

    p_ss = sub.add_parser("sync-stores", help="load the store directory from the sitemap")
    p_ss.add_argument("--source", default="totalwine")

    p_warm = sub.add_parser("warm",
                            help="interactively solve the first Press & Hold to warm the profile "
                                 "(do this once before an unattended --patient run)")
    p_warm.add_argument("--source", default="totalwine")

    p_dash = sub.add_parser("dashboard", help="serve the ingestion dashboard")
    p_dash.add_argument("--port", type=int, default=8000)

    p_bf = sub.add_parser("backfill-reviews",
                          help="re-fetch products that have a review count but no stored reviews")
    p_bf.add_argument("--source", default="totalwine")
    p_bf.add_argument("--limit", type=int, default=None)
    p_bf.add_argument("--delay-s", type=float, default=1.0)

    p_run = sub.add_parser("run", help="scrape product data from the sitemaps")
    p_run.add_argument("--source", default="totalwine",
                       help="retailer label stamped on every row")
    p_run.add_argument("--limit", type=int, default=None, help="max products")
    p_run.add_argument("--max-sitemaps", type=int, default=None,
                       help="cap number of product sitemaps scanned")
    p_run.add_argument("--max-wait-ms", type=int, default=10000,
                       help="max ms to wait for getProduct before giving up on a page")
    p_run.add_argument("--delay-s", type=float, default=1.0,
                       help="base polite pause between products (adaptive; ramps up on blocks)")
    p_run.add_argument("--fast", action="store_true",
                       help="max speed: no pacing + block images/css/fonts (higher PX-block risk)")
    p_run.add_argument("--no-resume", action="store_true",
                       help="do not skip products already in the DB")
    p_run.add_argument("--retry-blocked", action="store_true",
                       help="also retry products that were blocked on earlier runs "
                            "(by default those are skipped)")
    p_run.add_argument("--store", default=None,
                       help="pin a single store id (totalwine: per-store pricing; "
                            "walmart: assortmentStoreId)")
    p_run.add_argument("--states", default=None,
                       help="totalwine multi-store: comma-separated state codes (e.g. TX,NJ,PA); "
                            "scrapes per-store pricing for stores in those states")
    p_run.add_argument("--stores-per-state", type=int, default=1,
                       help="how many stores per state to scrape with --states (default 1)")
    p_run.add_argument("--patient", action="store_true",
                       help="slower pace + proactive periodic breaks so PerimeterX escalates "
                            "less; blocked products are skipped (retried on a later resume run)")
    p_run.add_argument("--pause-every", type=int, default=None,
                       help="take a break every N products (default 60 with --patient)")
    p_run.add_argument("--pause-seconds", type=float, default=None,
                       help="length of each break in seconds (default 240 with --patient)")
    p_run.add_argument("--warm-wait", type=int, default=60,
                       help="seconds to keep the homepage up at start so you can solve a "
                            "Press & Hold (returns early if PX passes invisibly)")
    p_run.add_argument("--interactive", action="store_true",
                       help="when a product gets PX-blocked mid-run, keep the challenge on "
                            "screen and wait for YOU to solve it (attended runs)")
    p_run.add_argument("--solve-wait", type=int, default=90,
                       help="seconds to wait for you to solve a mid-run challenge (--interactive)")

    p_par = sub.add_parser("run-parallel",
                           help="scrape with N browser workers (needs 1 proxy/IP each)")
    p_par.add_argument("--limit", type=int, default=None, help="max products (total)")
    p_par.add_argument("--workers", type=int, default=1,
                       help="concurrent browsers (use 1 without proxies; PX gates by IP)")
    p_par.add_argument("--proxies", nargs="*", default=None,
                       help="one proxy URL per worker, e.g. http://user:pass@ip:port ...")
    p_par.add_argument("--delay-s", type=float, default=0.0,
                       help="per-worker pause between products")

    args = ap.parse_args()

    if args.cmd == "init-db":
        from .db import init_db

        init_db()
        print("schema + tables created")
    elif args.cmd == "sync-stores":
        from .pipeline import sync_stores

        print({"stores": sync_stores(args.source)})
    elif args.cmd == "warm":
        from .pipeline import warm

        print(warm(args.source))
    elif args.cmd == "dashboard":
        from .dashboard import serve

        serve(args.port)
    elif args.cmd == "backfill-reviews":
        from .pipeline import backfill_reviews

        print(backfill_reviews(source=args.source, limit=args.limit, delay_s=args.delay_s))
    elif args.cmd == "run" and args.source == "walmart":
        from .walmart_pipeline import run_walmart

        pause_every = (args.pause_every if args.pause_every is not None
                       else (60 if args.patient else 0))
        pause_seconds = (args.pause_seconds if args.pause_seconds is not None
                         else (240.0 if args.patient else 180.0))
        print(run_walmart(limit=args.limit,
                          delay_s=(2.0 if args.patient else args.delay_s),
                          resume=not args.no_resume,
                          pause_every=pause_every, pause_seconds=pause_seconds,
                          warm_seconds=args.warm_wait, retry_blocked=args.retry_blocked,
                          store_id=args.store))
    elif args.cmd == "run":
        from .pipeline import run

        # Patient mode: slower pace + proactive periodic breaks so PX doesn't
        # escalate. Blocked products are SKIPPED (like normal mode) and picked
        # up on a later resume run — we don't stall retrying one stuck product.
        delay = 0.0 if args.fast else args.delay_s
        block_pauses = 0
        if args.patient:
            delay = max(delay, 2.0)
            pause_every = args.pause_every if args.pause_every is not None else 60
            pause_seconds = args.pause_seconds if args.pause_seconds is not None else 240.0
        else:
            pause_every = args.pause_every or 0
            pause_seconds = args.pause_seconds or 180.0

        common = dict(source=args.source, limit=args.limit, max_sitemaps=args.max_sitemaps,
                      max_wait_ms=args.max_wait_ms, delay_s=delay, block_resources=args.fast,
                      resume=not args.no_resume, pause_every=pause_every,
                      pause_seconds=pause_seconds, block_pauses=block_pauses,
                      warm_seconds=args.warm_wait, interactive=args.interactive,
                      solve_seconds=args.solve_wait, retry_blocked=args.retry_blocked)
        if args.states:
            from .pipeline import run_stores, stores_for_states
            states = [s.strip().upper() for s in args.states.split(",") if s.strip()]
            store_ids = stores_for_states(states, args.stores_per_state, args.source)
            print(f"multi-store: {len(store_ids)} stores across {states} -> {store_ids}")
            print(run_stores(store_ids, **common))
        elif args.store:
            print(run(store_id=args.store, **common))
        else:
            print(run(**common))
    elif args.cmd == "run-parallel":
        from .parallel import run_parallel

        print(run_parallel(limit=args.limit, workers=args.workers,
                           delay_s=args.delay_s, proxies=args.proxies))


if __name__ == "__main__":
    main()
