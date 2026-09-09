"""Database engine, session, schema bootstrap, and idempotent upserts.

Upserts are keyed on natural ids so re-running a scrape updates existing rows
instead of duplicating them (needed for the quarterly cadence).
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .config import config

log = logging.getLogger(__name__)
from .models import (
    Base,
    BlockedProduct,
    Product,
    ProductStoreAvailability,
    ProductVariant,
    Review,
    Store,
)

def _make_engine():
    """Engine tuned to work against local Postgres OR Supabase.

    Supabase needs SSL, and its connection pooler (pgBouncer, transaction mode)
    breaks psycopg3's default prepared statements — so for any remote host we
    require SSL and disable prepared statements. pool_pre_ping recovers dropped
    connections on a long remote run.
    """
    url = config.db_url
    is_local = "localhost" in url or "127.0.0.1" in url
    connect_args: dict = {}
    if not is_local:
        connect_args["sslmode"] = "require"
        connect_args["prepare_threshold"] = None  # pgBouncer-safe
    return create_engine(
        url, future=True, pool_pre_ping=not is_local, connect_args=connect_args
    )


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, future=True)


def init_db() -> None:
    """Create the schema and all tables if they don't exist."""
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{config.db_schema}"'))
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


_T = TypeVar("_T")


def with_db_retry(fn: Callable[[], _T], *, attempts: int = 6,
                  base_delay: float = 5.0, max_delay: float = 60.0,
                  final_wait: float = 300.0) -> _T:
    """Run a DB unit of work, retrying transient connection loss.

    A long unattended run survives the laptop sleeping/waking: on wake the
    pooled connection is dead and DNS may briefly fail, so we dispose the pool
    (forcing a fresh connect + DNS lookup) and back off long enough for the
    network to recover, rather than crashing after hours of work. Upserts are
    idempotent, so re-running the unit of work is safe.

    Backoff is exponential (capped at max_delay), but the wait BEFORE the final
    attempt is `final_wait` (default 5 min) — one last long grace period for the
    internet to come back after a real outage before giving up. Re-raises if it
    still can't connect on the final attempt.
    """
    for i in range(attempts):
        try:
            return fn()
        except (OperationalError, DBAPIError) as e:
            if i == attempts - 1:
                raise
            engine.dispose()  # drop stale/dead pooled connections
            # last retry gets a long grace wait; earlier ones back off normally
            delay = final_wait if i == attempts - 2 else min(max_delay, base_delay * (2 ** i))
            log.warning("DB connection lost (%s); disposed pool, retrying in "
                        "%.0fs (attempt %d/%d) — likely a sleep/network blip",
                        type(e).__name__, delay, i + 1, attempts)
            time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


def existing_product_ids(source: str) -> set[str]:
    """product_ids already in the DB for a source — used to resume/skip."""
    with SessionLocal() as session:
        return {
            row[0]
            for row in session.query(Product.product_id).filter(Product.source == source)
        }


# Permanent classification exclusions — always skipped, never retried.
PERMANENT_EXCLUSIONS = ("nonalcohol", "out_of_scope")


def product_ids_with_variant(source: str, store_id: str) -> set[str]:
    """product_ids that already have a variant for this store — per-store resume
    (so scraping store B doesn't skip products already captured at store A)."""
    with SessionLocal() as session:
        return {
            row[0]
            for row in session.query(ProductVariant.product_id).filter(
                ProductVariant.source == source, ProductVariant.store_id == store_id
            )
        }


def product_ids_attempted_at(source: str, requested_store: str) -> set[str]:
    """product_ids already ATTEMPTED while pinned to this store (by requested
    store, not the store the price came back from). This is the resume key: if
    we tried a product at store X we don't retry it there — even if the price
    fell back to a neighbouring store — but a product only ever fetched at a
    DIFFERENT store is still fetched here."""
    with SessionLocal() as session:
        return {
            row[0]
            for row in session.query(ProductVariant.product_id).filter(
                ProductVariant.source == source,
                ProductVariant.requested_store_id == requested_store,
            )
        }


def existing_blocked_ids(source: str) -> set[str]:
    """PX-blocked product_ids for a source (excludes permanent exclusions) —
    skipped unless --retry-blocked."""
    with SessionLocal() as session:
        return {
            row[0]
            for row in session.query(BlockedProduct.product_id).filter(
                BlockedProduct.source == source,
                (BlockedProduct.last_reason.is_(None))
                | (BlockedProduct.last_reason.notin_(PERMANENT_EXCLUSIONS)),
            )
        }


def excluded_product_ids(source: str) -> set[str]:
    """product_ids permanently excluded by classification (non-alcohol /
    out-of-scope) — always skipped so they're never re-fetched."""
    with SessionLocal() as session:
        return {
            row[0]
            for row in session.query(BlockedProduct.product_id).filter(
                BlockedProduct.source == source,
                BlockedProduct.last_reason.in_(PERMANENT_EXCLUSIONS),
            )
        }


def record_blocked(session: Session, source: str, product_id: str,
                   url: str | None, reason: str = "PXBlocked") -> None:
    """Remember a blocked product; bump attempts if already recorded."""
    from .models import utcnow

    stmt = pg_insert(BlockedProduct).values(
        source=source, product_id=product_id, url=url,
        attempts=1, last_reason=reason,
    ).on_conflict_do_update(
        index_elements=["source", "product_id"],
        set_={"attempts": BlockedProduct.attempts + 1,
              "last_reason": reason, "last_attempt": utcnow()},
    )
    session.execute(stmt)


def clear_blocked(session: Session, source: str, product_id: str) -> None:
    """Remove a product from the blocked list once it's been fetched OK."""
    session.query(BlockedProduct).filter(
        BlockedProduct.source == source, BlockedProduct.product_id == product_id
    ).delete()


def upsert_products(session: Session, rows: list[dict]) -> int:
    """Upsert products on (source, product_id); refresh mutable fields."""
    if not rows:
        return 0
    stmt = pg_insert(Product).values(rows)
    update_cols = {
        c: stmt.excluded[c]
        for c in (
            "name",
            "brand",
            "category",
            "subcategory",
            "url",
            "ai_review_summary",
            "avg_rating",
            "review_count",
            "is_new",
            "attributes",
            "last_seen",
        )
        if c in rows[0]
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "product_id"], set_=update_cols)
    session.execute(stmt)
    return len(rows)


def insert_variants(session: Session, rows: list[dict]) -> int:
    """Variants are time-scoped (price history); ignore exact-duplicate snapshots."""
    if not rows:
        return 0
    stmt = pg_insert(ProductVariant).values(rows).on_conflict_do_nothing(
        index_elements=["source", "variant_id", "store_id", "captured_at"]
    )
    session.execute(stmt)
    return len(rows)


def upsert_reviews(session: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = pg_insert(Review).values(rows).on_conflict_do_nothing(
        index_elements=["source", "review_id", "product_id"]
    )
    session.execute(stmt)
    return len(rows)


def upsert_stores(session: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = pg_insert(Store).values(rows)
    cols = ("name", "address", "city", "state", "zip", "phone", "latitude", "longitude")
    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "store_id"],
        set_={c: stmt.excluded[c] for c in cols if c in rows[0]},
    )
    session.execute(stmt)
    return len(rows)


def insert_availability(session: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = pg_insert(ProductStoreAvailability).values(rows).on_conflict_do_nothing(
        index_elements=["source", "product_id", "store_id", "captured_at"]
    )
    session.execute(stmt)
    return len(rows)
