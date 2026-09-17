"""Environment-driven configuration.

Loads from a local .env if present. Everything the scraper needs to run lives
here so the rest of the code never touches os.environ directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Config:
    db_url: str = os.getenv(
        "DB_URL",
        "postgresql+psycopg://scraper:scraper@localhost:5433/totalwine",
    )
    db_schema: str = os.getenv("DB_SCHEMA", "retail_product_data")
    store_id: str = os.getenv("STORE_ID", "")
    headless: bool = _bool("HEADLESS", True)
    proxy_urls: list[str] = field(default_factory=lambda: _list("PROXY_URLS"))
    request_delay_seconds: float = float(os.getenv("REQUEST_DELAY_SECONDS", "1.0"))
    max_concurrency: int = int(os.getenv("MAX_CONCURRENCY", "4"))

    base_url: str = "https://www.totalwine.com"


config = Config()
