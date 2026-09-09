"""Pipeline orchestration.

    sync_stores()  -> curl the store sitemap -> upsert `store`
    run(limit)     -> enumerate product URLs from sitemaps
                      -> warm browser session fetches each product page
                      -> intercept getProduct / reviews / summary
                      -> validate (Pydantic) -> upsert Postgres
                      -> track progress in `scrape_run`

Each product is committed as it's scraped, so a mid-run crash keeps everything
gathered so far and `scrape_run` reflects real progress.
"""

from __future__ import annotations

import logging
import random
import re
import time

from .browser import PXBlocked, TotalWineSession
from .db import (
    clear_blocked,
    excluded_product_ids,
    existing_blocked_ids,
    existing_product_ids,
    insert_variants,
    product_ids_attempted_at,
    record_blocked,
    session_scope,
    upsert_products,
    upsert_reviews,
    upsert_stores,
    with_db_retry,
)
from .models import ScrapeRun, utcnow
from .products import in_scope, parse_product
from .reviews import parse_reviews, parse_summary
from .sitemaps import (
    iter_product_urls,
    iter_stores,
    store_api_url,
    store_from_json,
    store_info_urls,
)

log = logging.getLogger("scraper.pipeline")

_CODE = re.compile(r"/p/(\d+)")


def _url_code(url: str) -> str | None:
    """The product code in a /p/<code> URL == the DB product_id."""
    m = _CODE.search(url)
    return m.group(1) if m else None


DEFAULT_SOURCE = "totalwine"


def _stamp(rows: list[dict], source: str) -> list[dict]:
    """Add the source key to each row before persisting."""
    for r in rows:
        r["source"] = source
    return rows


def warm(source: str = DEFAULT_SOURCE) -> dict:
    """Interactively warm the browser profile: open the site, let YOU solve the
    first Press & Hold once, then verify by fetching a product. After this the
    persistent profile carries the PerimeterX token so a later `run --patient`
    can proceed unattended (an unattended run can't solve that first challenge).
    """
    urls = list(iter_product_urls(limit=5))
    with TotalWineSession() as sess:
        sess.rewarm()  # load homepage so any challenge appears
        print("\n>>> A Chrome window is open. If a 'Press & Hold' challenge shows,")
        print(">>> complete it now (hold until it passes).")
        input(">>> Then press Enter here to verify the profile is warm...\n")
        for url in urls:
            try:
                data = sess.fetch(url)
            except PXBlocked:
                continue
            if data.get("product"):
                _persist_one(data, source)
                print("✓ profile is WARM — verified by fetching:", data["product"].get("name"))
                return {"warm": True}
        print("✗ still blocked after solving — the IP may be hard-flagged; "
              "rest it a few hours or switch IP, then warm again.")
        return {"warm": False}


def sync_stores(source: str = DEFAULT_SOURCE, resume: bool = True) -> dict:
    """Load the store directory and enrich each store with full detail
    (address, zip, phone, lat/long) from the store-locator API, fetched through
    the warm PX-cleared browser (curl gets rate-limited after ~a few dozen).
    Resumable: skips stores already enriched (zip present)."""
    from .db import SessionLocal
    from sqlalchemy import text as _text
    from .config import config as _cfg

    stores = list(iter_stores())               # 217 basic records (id/city/state)
    with SessionLocal() as s:
        done = {r[0] for r in s.execute(_text(
            f"SELECT store_id FROM {_cfg.db_schema}.store "
            f"WHERE source=:src AND zip IS NOT NULL"), {"src": source})} if resume else set()

    # Only touch stores not already enriched — DON'T blanket-seed, which would
    # overwrite already-enriched zip/address with the basic record's nulls.
    todo = [s for s in stores if s.store_id not in done]
    log.info("store enrichment: %d already enriched, %d to fetch", len(done), len(todo))
    enriched = 0
    with TotalWineSession() as sess:
        sess.warm_up(60)
        for i, s in enumerate(todo, 1):
            det = store_from_json(s.store_id, sess.get_json(store_api_url(s.store_id)))
            rec = det or s  # full detail, else the basic record so the store exists
            with session_scope() as session:
                upsert_stores(session, _stamp([rec.model_dump()], source))
            if det:
                enriched += 1
            if i % 25 == 0:
                log.info("store enrichment: %d/%d (%d ok)", i, len(todo), enriched)
            time.sleep(0.5)
    summary = {"total": len(stores), "newly_enriched": enriched, "already_done": len(done)}
    log.info("sync-stores complete: %s", summary)
    return summary


def _persist_one(data: dict, source: str = DEFAULT_SOURCE,
                 requested_store: str | None = None) -> bool:
    """Validate + persist one product's payloads. Returns True on success.

    requested_store is the store we PINNED for this fetch (may differ from the
    variant's store_id when Total Wine falls back to a neighbour) — stamped so
    resume knows we already tried this product at that store."""
    product_json = data.get("product")
    if not product_json:
        return False
    summary = parse_summary(data.get("summary") or {})
    product, variant = parse_product(product_json, ai_review_summary=summary)
    if product is None:
        return False
    if variant is not None and requested_store:
        variant.requested_store_id = requested_store

    pid = product.product_id
    reviews = parse_reviews(data.get("reviews") or {}, product_id=pid)

    def _persist():
        with session_scope() as session:
            upsert_products(session, _stamp([product.model_dump()], source))
            if variant is not None:
                insert_variants(session, _stamp([variant.model_dump()], source))
            if reviews:
                upsert_reviews(session, _stamp([r.model_dump() for r in reviews], source))

    with_db_retry(_persist)  # survive a sleep/network blip instead of crashing
    return True


# Retried variants of the small in-loop DB writes, so a sleep/network blip on
# any of them re-connects instead of crashing a long run (see with_db_retry).
def _record_blocked(source: str, code: str, url: str, reason: str = "PXBlocked") -> None:
    def go():
        with session_scope() as session:
            record_blocked(session, source, code, url, reason=reason)
    with_db_retry(go)


def _clear_blocked(source: str, code: str) -> None:
    def go():
        with session_scope() as session:
            clear_blocked(session, source, code)
    with_db_retry(go)


def _upsert_stores(rows: list[dict]) -> None:
    def go():
        with session_scope() as session:
            upsert_stores(session, rows)
    with_db_retry(go)


class _Throttle:
    """Adaptive pacing that ramps up when PerimeterX blocks and decays slowly.

    Unlike a per-request cooldown that resets on every success, the penalty
    persists across items, so a session that PX has started challenging slows
    down and *stays* slow for a while — which is what actually lowers the bot
    score and breaks the block-every-1-2-items loop.
    """

    def __init__(self, base: float, cap: float = 45.0):
        self.base = base
        self.cap = cap
        self.penalty = 0.0
        self.consec = 0

    def on_block(self) -> float:
        self.consec += 1
        # escalate the penalty; long recovery cooldown scales with the streak
        self.penalty = min(self.cap, max(self.penalty * 1.7, 4.0) + 3.0)
        return min(self.cap, 5.0 * self.consec)

    def on_success(self) -> None:
        self.consec = 0
        self.penalty *= 0.7          # decay slowly, not instantly

    def pace(self) -> None:
        wait = self.base + self.penalty
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.4 * wait + 0.3))  # jitter


def _fetch_with_rewarm(sess, url: str, thr: "_Throttle",
                       interactive: bool = False, solve_seconds: int = 90) -> dict | None:
    """Fetch a URL; on a PX block, recover.

    interactive: keep the challenge on the product page and wait for the USER to
    solve the Press & Hold (no navigating away). Otherwise fall back to an
    automatic homepage re-warm + retry.
    """
    try:
        return sess.fetch(url)
    except PXBlocked:
        pass

    if interactive and sess.challenge_visible():
        log.warning(">>> PerimeterX 'Press & Hold' on a product — SOLVE it in the "
                    "browser window now (waiting up to %ds)...", solve_seconds)
        data = sess.wait_for_solve(solve_seconds)
        if data and data.get("product"):
            log.info("challenge solved — continuing")
            return data

    cooldown = thr.on_block()
    log.info("transient PX block (%d in a row); %ds cooldown + re-warm, then retry "
             "(this usually recovers — let it run)", thr.consec, int(cooldown))
    time.sleep(cooldown)
    sess.rewarm()
    try:
        return sess.fetch(url)
    except PXBlocked:
        return None


def _fetch_patient(sess, url: str, thr: "_Throttle",
                   pause_seconds: float, block_pauses: int,
                   interactive: bool = False, solve_seconds: int = 90) -> dict | None:
    """Fetch with recovery. interactive => let the user solve the Press & Hold
    on the product page. Otherwise patient mode takes escalating multi-minute
    pauses to let PerimeterX's per-IP score decay and retries the same url.
    """
    data = _fetch_with_rewarm(sess, url, thr, interactive, solve_seconds)
    pauses = 0
    while data is None and pauses < block_pauses:
        pauses += 1
        wait = pause_seconds * pauses
        log.warning("still blocked — patient cooldown %.0fs (%d/%d) to let PX decay",
                    wait, pauses, block_pauses)
        time.sleep(wait)
        sess.rewarm()
        try:
            data = sess.fetch(url)
        except PXBlocked:
            data = None
    if data is None and block_pauses:
        log.warning("gave up on this product after %d recovery pauses — if this keeps "
                    "happening the IP is genuinely flagged (rest it or switch networks)",
                    block_pauses)
    return data


def backfill_reviews(*, source: str = DEFAULT_SOURCE, limit: int | None = None,
                     delay_s: float = 1.0) -> dict:
    """Re-fetch products that have a review count but no stored reviews.

    Early fast runs sometimes returned before the reviews XHR fired; this
    revisits just those products (using each one's stored URL — no re-crawl)
    and captures the reviews the scroll+grace now triggers.
    """
    from sqlalchemy import text
    from .config import config as _cfg

    with session_scope() as session:
        rows = session.execute(text(
            f"SELECT product_id, url FROM {_cfg.db_schema}.product p "
            f"WHERE source = :src AND review_count > 0 AND url IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {_cfg.db_schema}.review r "
            f"               WHERE r.source = p.source AND r.product_id = p.product_id) "
            f"ORDER BY review_count DESC"
            + (f" LIMIT {int(limit)}" if limit else "")), {"src": source}).all()

    targets = [(r[0], r[1]) for r in rows]
    log.info("backfill: %d products missing reviews", len(targets))
    fixed = still_empty = blocked = 0
    thr = _Throttle(base=delay_s)

    with TotalWineSession() as sess:
        sess.rewarm()  # homepage first so PX establishes a token in this context
        for pid, url in targets:
            try:
                data = _fetch_with_rewarm(sess, url, thr)
            except Exception as e:  # noqa: BLE001
                log.warning("backfill error %s: %s", url, e)
                thr.pace()
                continue
            if data is None:
                blocked += 1
                thr.pace()
                continue
            thr.on_success()
            reviews = parse_reviews(data.get("reviews") or {}, product_id=pid)
            if reviews:
                with session_scope() as session:
                    upsert_reviews(session, _stamp([r.model_dump() for r in reviews], source))
                fixed += 1
            else:
                still_empty += 1
            if (fixed + still_empty) % 25 == 0:
                log.info("backfill: fixed=%d still_empty=%d blocked=%d", fixed, still_empty, blocked)
            thr.pace()

    summary = {"fixed": fixed, "still_empty": still_empty, "blocked": blocked}
    log.info("backfill complete: %s", summary)
    return summary


def run(*, source: str = DEFAULT_SOURCE, limit: int | None = None,
        max_sitemaps: int | None = None, max_wait_ms: int = 10000,
        delay_s: float = 1.0, block_resources: bool = False, resume: bool = True,
        pause_every: int = 0, pause_seconds: float = 180.0, block_pauses: int = 0,
        warm_seconds: int = 60, interactive: bool = False, solve_seconds: int = 90,
        retry_blocked: bool = False, store_id: str | None = None,
        log_every: int = 25) -> dict:
    """Scrape product data for up to `limit` products from the sitemaps.

    Resumable: with resume=True, products already in the DB (for this source)
    are skipped, so a re-run continues where a crash/stop left off (and retries
    anything that was blocked, since blocked URLs never made it into the DB).

    Defaults are tuned for PerimeterX-friendliness on a long run: a 1s base
    pace (with jitter), resources NOT blocked (real browsers load assets), and
    adaptive throttling that ramps up after blocks. For a quick small test you
    can pass delay_s=0 / block_resources=True to go faster.

    Patient mode (for an unattended single-IP grind): `pause_every` products,
    take a `pause_seconds` break so the PX score decays before it escalates to
    Press & Hold; `block_pauses` gives blocked products escalating multi-minute
    retries instead of skipping. This trades speed for not needing a human.
    """
    ingested = skipped = errors = blocked = out_of_scope = 0
    thr = _Throttle(base=delay_s)
    # Resume keys on the REQUESTED (pinned) store, not where the price came from:
    # if we already tried a product at store X we don't retry it there — even if
    # the price fell back to a neighbour — but a product only ever fetched at a
    # DIFFERENT store is still attempted here. `pinned` is the store we set (and
    # stamp on each variant). With --store we know it now; for a default run we
    # resolve the profile's pinned store after warm-up (see resume_after_warm).
    done: set[str] = set()
    resume_after_warm = False
    pinned = store_id  # for the default run, resolved after warm-up below
    if resume and store_id:
        done = product_ids_attempted_at(source, store_id)
        log.info("resume: %d products already attempted at store %s will be skipped",
                 len(done), store_id)
    elif resume:
        resume_after_warm = True  # skip-set depends on the profile's pinned store
    # Skip previously-blocked products by default so resume doesn't retry them
    # first and ramp the throttle; --retry-blocked forces another attempt.
    blocked_ids = set() if retry_blocked else existing_blocked_ids(source)
    if blocked_ids:
        log.info("skipping %d previously-blocked products (use --retry-blocked to retry)",
                 len(blocked_ids))
    # Permanently-excluded (out-of-scope: gifts/cigars/accessories) — never re-fetch.
    excluded_ids = excluded_product_ids(source)
    if excluded_ids:
        log.info("skipping %d out-of-scope products", len(excluded_ids))
    # Known store ids — to auto-enrich any store a product references but that
    # the store-locator sitemap didn't include.
    from sqlalchemy import text as _t
    from .config import config as _cfg
    from .db import SessionLocal as _SL
    with _SL() as _s:
        known_stores = {r[0] for r in _s.execute(_t(
            f"SELECT store_id FROM {_cfg.db_schema}.store WHERE source=:s"), {"s": source})}

    store_info_url = store_info_urls().get(store_id) if store_id else None
    if store_id and not store_info_url:
        log.warning("no store-info URL for store %s — can't pin; aborting", store_id)
        return {"ingested": 0, "error": f"unknown store {store_id}"}

    with session_scope() as session:
        run_row = ScrapeRun(source=source,
                            category=f"store:{store_id}" if store_id else "sitemap")
        session.add(run_row)
        session.flush()
        run_id = run_row.id

    try:
        with TotalWineSession(
            max_wait_ms=max_wait_ms, block_resources=block_resources
        ) as sess:
            # Land on the homepage first so the PerimeterX sensor establishes a
            # token in this fresh context — a real user (and `warm`) does this.
            # Jumping straight to a product page otherwise trips PX immediately.
            # Give the user up to warm_seconds to solve a Press & Hold if shown.
            log.info("warming up on homepage — if a 'Press & Hold' appears, "
                     "solve it (waiting up to %ds)...", warm_seconds)
            if sess.warm_up(max_seconds=warm_seconds):
                log.info("warm-up OK — starting product fetches")
            else:
                log.warning("warm-up still blocked after %ds; the IP may be hot. "
                            "Continuing, but expect blocks.", warm_seconds)
            if store_info_url:
                if sess.set_store(store_info_url, store_id):
                    log.info("pinned store %s", store_id)
                else:
                    log.warning("could not pin store %s — prices may be from another "
                                "store; continuing", store_id)
            # Default run (no --store): the pinned store is the profile default.
            # Resume against products already ATTEMPTED at that store, so we don't
            # skip one just because a fallback price was recorded under a
            # different store — and we don't re-fetch one we already tried here.
            if resume_after_warm:
                eff = sess.current_store_id()
                if eff:
                    pinned = eff
                    done = product_ids_attempted_at(source, eff)
                    log.info("resume: %d products already attempted at store %s "
                             "(profile default) will be skipped", len(done), eff)
                else:
                    log.warning("could not detect the profile's pinned store; resuming "
                                "by product across all stores")
                    done = existing_product_ids(source)
            for url in iter_product_urls(limit=limit, max_sitemaps=max_sitemaps):
                code = _url_code(url)
                if code and (code in done or code in blocked_ids or code in excluded_ids):
                    skipped += 1
                    continue

                try:
                    data = _fetch_patient(sess, url, thr, pause_seconds, block_pauses,
                                          interactive, solve_seconds)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    log.warning("fetch/parse error %s: %s", url, e)
                    continue
                if data is None:
                    blocked += 1
                    if code:
                        _record_blocked(source, code, url)
                    thr.pace()
                    continue
                thr.on_success()

                pj = data.get("product") or {}
                # Out of scope (gifts / cigars / accessories) — remember + skip.
                if pj and not in_scope(pj):
                    out_of_scope += 1
                    if code:
                        excluded_ids.add(code)
                        _record_blocked(source, code, url, reason="out_of_scope")
                    thr.pace()
                    continue

                if _persist_one(data, source, requested_store=pinned):
                    ingested += 1
                    if code:
                        done.add(code)   # avoid re-fetching within this run too
                        if retry_blocked:
                            _clear_blocked(source, code)
                    # Auto-enrich a store we haven't catalogued yet.
                    sid = str(pj.get("storeId") or "")
                    if sid and sid not in known_stores:
                        det = store_from_json(sid, sess.get_json(store_api_url(sid)))
                        if det:
                            _upsert_stores(_stamp([det.model_dump()], source))
                        known_stores.add(sid)  # add regardless so we don't retry
                else:
                    errors += 1

                if ingested and ingested % log_every == 0:
                    log.info("ingested=%d skipped=%d out_of_scope=%d errors=%d blocked=%d penalty=%.1fs",
                             ingested, skipped, out_of_scope, errors, blocked, thr.penalty)

                # Patient mode: periodic proactive break so PX's score decays
                # before it escalates to a Press & Hold.
                if pause_every and ingested and ingested % pause_every == 0:
                    log.info("patient pause: %.0fs after %d products (let PX cool off)",
                             pause_seconds, ingested)
                    time.sleep(pause_seconds)
                thr.pace()
    finally:
        def _finalize():
            with session_scope() as session:
                row = session.get(ScrapeRun, run_id)
                row.finished_at = utcnow()
                row.records_ingested = ingested
                row.error_count = errors + blocked
                row.notes = f"skipped={skipped} out_of_scope={out_of_scope} blocked={blocked}"
        with_db_retry(_finalize)

    summary = {"ingested": ingested, "skipped": skipped, "out_of_scope": out_of_scope,
               "errors": errors, "blocked": blocked, "store": store_id, "run_id": run_id}
    log.info("run complete: %s", summary)
    return summary


def stores_for_states(states: list[str], per_state: int = 1,
                      source: str = DEFAULT_SOURCE) -> list[str]:
    """Pick up to `per_state` store_ids per state from the (enriched) store
    table. States match on the 2-letter code (e.g. TX, NJ)."""
    from sqlalchemy import text as _t
    from .config import config as _cfg
    from .db import SessionLocal as _SL
    picks: list[str] = []
    with _SL() as s:
        for st in states:
            rows = s.execute(_t(
                f"SELECT store_id FROM {_cfg.db_schema}.store "
                f"WHERE source=:src AND upper(state)=upper(:st) AND zip IS NOT NULL "
                f"ORDER BY store_id LIMIT :n"),
                {"src": source, "st": st, "n": per_state}).all()
            picks += [r[0] for r in rows]
    return picks


def run_stores(store_ids: list[str], *, source: str = DEFAULT_SOURCE,
               limit: int | None = None, **run_kwargs) -> list[dict]:
    """Scrape the catalog once per pinned store (per-store pricing). Each store
    is an independent, resumable run — a new browser session per store."""
    results = []
    for i, sid in enumerate(store_ids, 1):
        log.info("=== store %d/%d: %s ===", i, len(store_ids), sid)
        results.append(run(source=source, store_id=sid, limit=limit, **run_kwargs))
    return results
