"""Database engine, session, schema bootstrap, and idempotent upserts.

Upserts are keyed on natural ids so re-running a scrape updates existing rows
instead of duplicating them (needed for the quarterly cadence).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from .config import config
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
