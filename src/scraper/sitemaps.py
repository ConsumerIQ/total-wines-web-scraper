"""Catalog enumeration via Total Wine's public sitemaps.

Sitemaps are NOT behind PerimeterX — plain curl_cffi fetches them fine. This is
how we get the full list of product URLs (~5k per file x 17 files) and the store
directory, without touching a blocked browse page.

    sitemap.xml (index)
      -> Product-en-USD-0.xml .. Product-en-USD-16.xml   (product page URLs)
      -> Store-en-USD.xml                                 (store-info URLs)

Product URL: https://www.totalwine.com/<path>/p/<code>   (code = base product id)
Store  URL:  https://www.totalwine.com/store-info/<state>-<city>/<store_id>
"""

from __future__ import annotations

import re
from typing import Iterator

from curl_cffi import requests as cffi

from .config import config
from .fetch import DEFAULT_HEADERS
from .models import StoreIn

INDEX_URL = f"{config.base_url}/sitemap.xml"
_STORE_API = config.base_url + "/search/api/store/storelocator/v1/store/{sid}"
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_PRODUCT_URL = re.compile(r"^https://www\.totalwine\.com/.+/p/\d+$")
_STORE_URL = re.compile(r"^https://www\.totalwine\.com/store-info/([^/]+)/(\d+)$")


def _get(url: str) -> str:
    r = cffi.get(url, headers=DEFAULT_HEADERS, impersonate="chrome124", timeout=45)
    if r.status_code != 200:
        raise RuntimeError(f"sitemap fetch {url} -> {r.status_code}")
    return r.text


def _locs(xml: str) -> list[str]:
    return [m.strip() for m in _LOC.findall(xml)]


def product_sitemap_urls() -> list[str]:
    """Child sitemaps whose <loc>s are product pages (the Product-en-USD-*.xml)."""
    return [u for u in _locs(_get(INDEX_URL)) if "Product-en-USD" in u]


def iter_product_urls(
    *, max_sitemaps: int | None = None, limit: int | None = None
) -> Iterator[str]:
    """Yield product page URLs across the product sitemaps (deduped)."""
    seen: set[str] = set()
    count = 0
    for i, sm in enumerate(product_sitemap_urls()):
        if max_sitemaps is not None and i >= max_sitemaps:
            break
        for url in _locs(_get(sm)):
            if not _PRODUCT_URL.match(url) or url in seen:
                continue
            seen.add(url)
            yield url
            count += 1
            if limit is not None and count >= limit:
                return


def store_api_url(store_id: str) -> str:
    return _STORE_API.format(sid=store_id)


def store_info_urls() -> dict[str, str]:
    """Map store_id -> store-info page URL (/store-info/<slug>/<id>) from the
    store sitemap — used to pin a store via "Set As My Store"."""
    store_sm = next((u for u in _locs(_get(INDEX_URL)) if "Store-en-USD" in u), None)
    out: dict[str, str] = {}
    if store_sm:
        for url in _locs(_get(store_sm)):
            m = _STORE_URL.match(url)
            if m:
                out[m.group(2)] = url
    return out


def store_from_json(store_id: str, j: dict | None) -> StoreIn | None:
    """Map the store-locator API JSON to a StoreIn. None if no usable data."""
    if not isinstance(j, dict) or not j.get("city"):
        return None
    street = ", ".join(x for x in (j.get("address1"), j.get("address2")) if x)
    try:
        return StoreIn(
            store_id=str(store_id),
            name=j.get("name"),
            address=street or None,
            city=j.get("city"),
            state=j.get("stateShort") or j.get("state"),
            zip=str(j.get("zip") or "") or None,
            phone=j.get("phoneFormatted") or j.get("phone"),
            latitude=j.get("latitude"),
            longitude=j.get("longitude"),
        )
    except Exception:
        return None


def fetch_store_detail(store_id: str) -> StoreIn | None:
    """Store detail via plain curl. Works until PerimeterX rate-limits the IP
    (~a few dozen calls); for the full set use the browser path in sync_stores."""
    try:
        r = cffi.get(store_api_url(store_id), headers=DEFAULT_HEADERS,
                     impersonate="chrome124", timeout=30)
        if r.status_code != 200:
            return None
        return store_from_json(store_id, r.json())
    except Exception:
        return None


# Full state/territory name -> USPS code, for turning the store-info slug
# (`<state-words>-<city-words>`) into a 2-letter state like the enriched path.
_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}


def _slug_state_city(slug: str) -> tuple[str | None, str | None]:
    """Split a `<state-words>-<city-words>` slug into (2-letter state, city).

    The state can be multiple words (`new-york-westbury`), so match the longest
    known state-name prefix (up to 3 words for 'district of columbia') rather
    than assuming the first token is the state. Returns a USPS code to match the
    enriched store path; unknown prefixes yield (None, whole-slug-as-city)."""
    words = slug.split("-")
    for n in (3, 2, 1):
        name = " ".join(words[:n])
        if name in _US_STATES:
            city = " ".join(words[n:]).title() or None
            return _US_STATES[name], city
    return None, slug.replace("-", " ").title() or None


def iter_stores() -> Iterator[StoreIn]:
    """Yield StoreIn parsed from the store sitemap URLs.

    Only id + slug-derived state/city are available here; richer fields would
    need a store-detail call. Slug format is `<state-words>-<city-words>`.
    """
    store_sm = next((u for u in _locs(_get(INDEX_URL)) if "Store-en-USD" in u), None)
    if not store_sm:
        return
    for url in _locs(_get(store_sm)):
        m = _STORE_URL.match(url)
        if not m:
            continue
        slug, store_id = m.group(1), m.group(2)
        state, city = _slug_state_city(slug)
        try:
            yield StoreIn(
                store_id=store_id,
                name=slug.replace("-", " ").title(),
                city=city,
                state=state,
            )
        except Exception:
            continue
