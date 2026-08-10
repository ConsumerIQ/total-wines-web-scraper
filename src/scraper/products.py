"""Parse the getProduct API payload into validated Product + Variant records.

Shape confirmed from a live capture (spike_out/network/resp_004.json):

    {
      "id": 140521750, "skuId": "140521750-1",
      "name": "...", "brand": {"name": "Jim Beam", ...},
      "productUrl": "/spirits/.../p/140521750",
      "department": "c0030",
      "categories": [{"name": "American Whiskey", "type": "PRODUCT_TYPE"},
                     {"name": "Whiskey", "type": "VARIETAL_TYPE"}, ...],
      "price": [{"price": 21.19, "type": "EDLP"}],
      "customerAverageRating": 4.52, "customerReviewsCount": 21,
      "packageDescription": "750ml Bottle",
      "skus": [{"skuId": "140521750-1",
                "options": [{"type": "SIZE", "value": "750ml"}, ...]}],
      "stockLevel": [{"stock": 8, "purchaseLimit": 8}],
      "unavailableAtStore": false,
      "alcoholPercentage": 75.5
    }
"""

from __future__ import annotations

from .config import config
from .models import ProductIn, VariantIn


def _category_of(categories: list[dict], type_: str) -> str | None:
    for c in categories or []:
        if c.get("type") == type_:
            return c.get("name")
    return None


def _hierarchy(payload: dict) -> tuple[str | None, str | None]:
    """(category, subcategory) from breadcrumbs: Home > <beverage type> >
    <product type> > ... So category = beverage type (Spirits/Wine/Beer/RTD/
    THC/Non-Alcoholic), subcategory = product type (e.g. American Whiskey)."""
    crumbs = [c for c in payload.get("breadCrumbs", [])
              if c.get("url") and c.get("name") and c.get("name") != "Home"]
    cat = crumbs[0]["name"] if crumbs else None
    sub = crumbs[1]["name"] if len(crumbs) > 1 else None
    return cat, sub


# Top-level categories that aren't beverage alcohol (Gauri's scope excludes
# gifts/accessories/cigars). Non-Alcoholic IS in scope, so it's not here.
_OUT_OF_SCOPE_CATEGORIES = {
    "gifts", "gifts & accessories", "misc", "accessories", "barware",
    "glassware", "bar accessories", "food", "cigars",
}


def in_scope(payload: dict) -> bool:
    """True if the product is a beverage (wine/spirits/beer/RTD/THC/non-alc) —
    excludes gifts, cigars (Misc), and accessories."""
    cat, _ = _hierarchy(payload)
    return (cat or "").strip().lower() not in _OUT_OF_SCOPE_CATEGORIES


def _stock(product: dict) -> int | None:
    levels = product.get("stockLevel")
    if isinstance(levels, list) and levels:
        try:
            return int(levels[0].get("stock"))
        except (TypeError, ValueError):
            return None
    return None


def _size(product: dict) -> str | None:
    # packageDescription is the authoritative page-level descriptor tied to the
    # priced skuId (e.g. "750ml Bottle", and for beer it includes the pack
    # count). Prefer it. Fall back to the SIZE option of the MATCHING sku — not
    # skus[0], which for multi-SKU products can pair a wrong size with the price.
    pkg = product.get("packageDescription")
    if pkg:
        return pkg
    page_sku = product.get("skuId")
    skus = product.get("skus", [])
    match = next((s for s in skus if s.get("skuId") == page_sku), None)
    for sku in ([match] if match else skus):
        for opt in (sku or {}).get("options", []):
            if opt.get("type") == "SIZE":
                return opt.get("value")
    return None


def _prices(product: dict) -> tuple[float | None, float | None, bool]:
    """Parse the typed price array -> (effective, list_price, on_deal).

    Entries look like [{"price":39.99,"type":"EDLP"}, {"price":29.99,"type":"LTSP"}]
    where EDLP is the regular price and a non-EDLP type (e.g. LTSP = Limited-Time
    Sale Price) is the deal. Effective = sale if present, else regular.
    """
    by_type: dict[str, float] = {}
    for e in product.get("price") or []:
        t, p = e.get("type"), e.get("price")
        if t and p is not None:
            try:
                by_type[t] = float(p)
            except (TypeError, ValueError):
                pass
    if not by_type:
        return None, None, False
    regular = by_type.get("EDLP")
    sale = next((v for t, v in by_type.items() if t != "EDLP"), None)
    effective = sale if sale is not None else regular
    if regular is None:  # only a sale type present
        regular = effective
    return effective, regular, sale is not None


def _attributes(product: dict) -> dict | None:
    """The PDP "Product Details" panel — a per-product set of attributes
    (Country, Spirits Type, Taste, Varietal, Region, ...). Keys vary by product,
    so store them as a flexible dict. Comes from itemCharacteristics, plus ABV.
    """
    attrs: dict = {}
    for ch in product.get("itemCharacteristics") or []:
        name = ch.get("attributeName")
        val = ch.get("value")
        if name and val is not None:
            attrs[name] = val
    abv = product.get("alcoholPercentage")
    if abv is not None:
        attrs["alcoholPercentage"] = abv
    # Promotion strategy label (e.g. {"name":"Winery Direct","type":"WD"}) when
    # present — the on_deal price signal lives on the variant.
    ss = product.get("salesStrategy")
    if isinstance(ss, dict) and ss:
        attrs["salesStrategy"] = ss
    return attrs or None


def _in_stock(product: dict) -> bool | None:
    if product.get("unavailableAtStore") is True:
        return False
    levels = product.get("stockLevel")
    if isinstance(levels, list) and levels:
        try:
            return int(levels[0].get("stock", 0)) > 0
        except (TypeError, ValueError):
            return None
    return None


def parse_product(
    payload: dict, *, ai_review_summary: str | None = None
) -> tuple[ProductIn | None, VariantIn | None]:
    """getProduct JSON -> (ProductIn, VariantIn). Returns (None, None) if invalid."""
    pid = str(payload.get("id") or "").strip()
    sku_id = str(payload.get("skuId") or "").strip()
    if not pid or not sku_id:
        return None, None

    categories = payload.get("categories", [])
    url = payload.get("productUrl")
    if url and not url.startswith("http"):
        url = f"{config.base_url}{url}"

    cat, sub = _hierarchy(payload)
    try:
        product = ProductIn(
            product_id=pid,
            name=payload.get("name", ""),
            brand=(payload.get("brand") or {}).get("name"),
            category=cat or payload.get("department"),
            subcategory=sub or _category_of(categories, "PRODUCT_TYPE"),
            url=url,
            ai_review_summary=ai_review_summary or None,
            avg_rating=payload.get("customerAverageRating"),
            review_count=payload.get("customerReviewsCount"),
            is_new=payload.get("itemNew"),
            attributes=_attributes(payload),
        )
    except Exception:
        return None, None

    try:
        eff, listp, on_deal = _prices(payload)
        variant = VariantIn(
            variant_id=sku_id,
            product_id=pid,
            store_id=str(payload.get("storeId") or ""),
            size=_size(payload),
            price=eff,
            list_price=listp,
            on_deal=on_deal,
            in_stock=_in_stock(payload),
            stock=_stock(payload),
        )
    except Exception:
        variant = None

    return product, variant
