"""Data model: SQLAlchemy ORM tables + Pydantic validation schemas.

Two layers on purpose:
- Pydantic `*In` models validate/clean every record scraped from a site BEFORE
  it touches the DB (the "data validation" step Ashray flagged).
- SQLAlchemy models are the persisted shape in Postgres (schema=`web_scraping`).

Multi-source: every table carries a `source` column (e.g. 'totalwine',
'walmart', 'amazon') and it's part of the primary key, so the same schema holds
all retailers without id collisions and you can compare a product across sites.

Prices/availability are store- and time-scoped, so variant prices and
availability keep a `captured_at` history rather than overwriting in place.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    # Bind every table to the configured schema (e.g. `web_scraping`).
    metadata = MetaData(schema=config.db_schema)


# --------------------------------------------------------------------------- #
# ORM tables  (source is part of every natural key)
# --------------------------------------------------------------------------- #
class Product(Base):
    __tablename__ = "product"

    source: Mapped[str] = mapped_column(String, primary_key=True)
    product_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    brand: Mapped[str | None] = mapped_column(String)
    category: Mapped[str | None] = mapped_column(String)
    subcategory: Mapped[str | None] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(String)
    ai_review_summary: Mapped[str | None] = mapped_column(Text)
    avg_rating: Mapped[float | None] = mapped_column(Float)
    review_count: Mapped[int | None] = mapped_column(Integer)
    is_new: Mapped[bool | None] = mapped_column(Boolean)  # newly-listed (no reviews yet)
    # Per-product "Product Details" attributes (Country, Spirits Type, Taste,
    # Varietal, Region, ABV, ...) — keys vary by product, so keep them flexible.
    attributes: Mapped[dict | None] = mapped_column(JSONB)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProductVariant(Base):
    __tablename__ = "product_variant"
    __table_args__ = (Index("ix_variant_source_product", "source", "product_id"),)

    source: Mapped[str] = mapped_column(String, primary_key=True)
    variant_id: Mapped[str] = mapped_column(String, primary_key=True)
    # the store the price/availability was captured at ('' if not store-scoped)
    # — part of the key so the same product holds a price row per store.
    store_id: Mapped[str] = mapped_column(String, primary_key=True, default="")
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, primary_key=True
    )
    product_id: Mapped[str] = mapped_column(String)
    # The store we PINNED when fetching. store_id above is where the price
    # actually came from — Total Wine falls back to a nearby store when the
    # pinned store doesn't carry the product, so the two can differ. Resume
    # keys on requested_store_id ("did we already try this product at store X?").
    requested_store_id: Mapped[str | None] = mapped_column(String)
    size: Mapped[str | None] = mapped_column(String)
    price: Mapped[float | None] = mapped_column(Float)       # effective (sale if on deal)
    list_price: Mapped[float | None] = mapped_column(Float)  # regular (EDLP) price
    on_deal: Mapped[bool | None] = mapped_column(Boolean)    # sale price present
    in_stock: Mapped[bool | None] = mapped_column(Boolean)
    stock: Mapped[int | None] = mapped_column(Integer)  # quantity available at store


class Review(Base):
    __tablename__ = "review"
    __table_args__ = (Index("ix_review_source_product", "source", "product_id"),)

    # Composite PK: the same review is shared across a product family (all SKUs
    # of one product return the same review ids), so key on source+id+product
    # to let each product row keep its own copy.
    source: Mapped[str] = mapped_column(String, primary_key=True)
    review_id: Mapped[str] = mapped_column(String, primary_key=True)
    product_id: Mapped[str] = mapped_column(String, primary_key=True)
    rating: Mapped[float | None] = mapped_column(Float)
    title: Mapped[str | None] = mapped_column(String)
    body: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String)
    review_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    helpful_count: Mapped[int | None] = mapped_column(Integer)


class Store(Base):
    __tablename__ = "store"

    source: Mapped[str] = mapped_column(String, primary_key=True)
    store_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    address: Mapped[str | None] = mapped_column(String)
    city: Mapped[str | None] = mapped_column(String)
    state: Mapped[str | None] = mapped_column(String)
    zip: Mapped[str | None] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)


class ProductStoreAvailability(Base):
    __tablename__ = "product_store_availability"

    source: Mapped[str] = mapped_column(String, primary_key=True)
    product_id: Mapped[str] = mapped_column(String, primary_key=True)
    store_id: Mapped[str] = mapped_column(String, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, primary_key=True
    )
    pickup_available: Mapped[bool | None] = mapped_column(Boolean)


class BlockedProduct(Base):
    """Products that hit a PerimeterX block and never got fetched. Skipped on
    later runs by default (so resume doesn't retry them first and ramp the
    throttle); retried only with --retry-blocked. Cleared once fetched OK."""

    __tablename__ = "blocked_product"

    source: Mapped[str] = mapped_column(String, primary_key=True)
    product_id: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str | None] = mapped_column(String)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    last_reason: Mapped[str | None] = mapped_column(String)
    last_attempt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ScrapeRun(Base):
    __tablename__ = "scrape_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str | None] = mapped_column(String)
    category: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records_ingested: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- #
# Pydantic validation schemas (validate before persist)
# `source` is stamped by the pipeline, not scraped, so it's not required here.
# --------------------------------------------------------------------------- #
class ProductIn(BaseModel):
    product_id: str
    name: str
    brand: str | None = None
    category: str | None = None
    subcategory: str | None = None
    url: str | None = None
    ai_review_summary: str | None = None
    avg_rating: float | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    is_new: bool | None = None
    attributes: dict | None = None

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("product name is empty")
        return v


class VariantIn(BaseModel):
    variant_id: str
    product_id: str
    store_id: str = ""
    requested_store_id: str | None = None
    size: str | None = None
    price: float | None = Field(default=None, ge=0)
    list_price: float | None = Field(default=None, ge=0)
    on_deal: bool | None = None
    in_stock: bool | None = None
    stock: int | None = Field(default=None, ge=0)


class ReviewIn(BaseModel):
    review_id: str
    product_id: str
    rating: float | None = Field(default=None, ge=0, le=5)
    title: str | None = None
    body: str | None = None
    author: str | None = None
    review_date: datetime | None = None
    helpful_count: int | None = Field(default=None, ge=0)


class StoreIn(BaseModel):
    store_id: str
    name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    phone: str | None = None
    latitude: float | None = None
    longitude: float | None = None
