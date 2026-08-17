"""Local web dashboard for the scraped Total Wine data.

Reads the Postgres `totalwine` schema and renders an at-a-glance view: headline
counts, price coverage, category + rating breakdowns, recent scrape runs, and a
sample of top-reviewed products. Pure stdlib http.server + SQLAlchemy (no extra
deps); server-rendered with CSS bars so it works offline.

    python -m scraper.cli dashboard          # http://localhost:8000
    python -m scraper.cli dashboard --port 8080
"""

from __future__ import annotations

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from sqlalchemy import text

from .config import config
from .db import engine

S = config.db_schema


def _scalar(conn, sql, default=0):
    v = conn.execute(text(sql)).scalar()
    return v if v is not None else default


PREVIEW_PER_PAGE = 20


def gather(source: str | None = None, page: int = 1) -> dict:
    # Optional per-source filter: `pw`/`vw`/`rw` are WHERE/AND fragments applied
    # to product (p)/variant (v)/review (r) queries; `prm` carries the param.
    prm = {"src": source}
    pw = " AND p.source = :src" if source else ""
    vw = " AND v.source = :src" if source else ""
    p_only = " WHERE source = :src" if source else ""
    p_and = " AND source = :src" if source else ""

    with engine.connect() as c:
        def sc(sql):
            return c.execute(text(sql), prm).scalar() or 0

        d: dict = {"source": source}
        d["products"] = sc(f"SELECT count(*) FROM {S}.product WHERE 1=1{p_and}")
        d["variants"] = sc(f"SELECT count(*) FROM {S}.product_variant WHERE 1=1{p_and}")
        d["reviews"] = sc(f"SELECT count(*) FROM {S}.review WHERE 1=1{p_and}")
        d["stores"] = sc(f"SELECT count(*) FROM {S}.store WHERE 1=1{p_and}")
        d["priced"] = sc(f"SELECT count(*) FROM {S}.product_variant WHERE price IS NOT NULL{p_and}")
        d["avg_price"] = sc(f"SELECT round(avg(price)::numeric,2) FROM {S}.product_variant WHERE price IS NOT NULL{p_and}")
        d["with_rating"] = sc(f"SELECT count(*) FROM {S}.product WHERE review_count > 0{p_and}")
        d["new"] = sc(f"SELECT count(*) FROM {S}.product WHERE is_new = true{p_and}")
        d["on_deal"] = sc(f"SELECT count(DISTINCT product_id) FROM {S}.product_variant "
                          f"WHERE on_deal = true{p_and}")
        d["subcats"] = c.execute(text(
            f"SELECT coalesce(subcategory,'(none)'), count(*) n FROM {S}.product "
            f"WHERE subcategory IS NOT NULL{p_and} GROUP BY 1 ORDER BY n DESC LIMIT 12"), prm).all()
        # sources list is always global (drives the tab bar)
        d["sources"] = c.execute(text(
            f"SELECT source, count(*) n FROM {S}.product GROUP BY 1 ORDER BY n DESC")).all()
        d["categories"] = c.execute(text(
            f"SELECT coalesce(category,'(none)'), count(*) n FROM {S}.product "
            f"WHERE 1=1{p_and} GROUP BY 1 ORDER BY n DESC LIMIT 12"), prm).all()
        d["ratings"] = c.execute(text(
            f"SELECT floor(avg_rating)::int b, count(*) n FROM {S}.product "
            f"WHERE avg_rating > 0{p_and} GROUP BY 1 ORDER BY 1"), prm).all()
        d["runs"] = c.execute(text(
            f"SELECT id, source, started_at, finished_at, records_ingested, error_count, notes "
            f"FROM {S}.scrape_run WHERE 1=1{p_and} ORDER BY id DESC LIMIT 8"), prm).all()
        # On-deal products: newest deal variant per product, with was/now price.
        d["deals"] = c.execute(text(
            f"SELECT DISTINCT ON (v.product_id) v.product_id, v.source, p.name, p.category, "
            f"       v.size, v.price, v.list_price, v.store_id "
            f"FROM {S}.product_variant v JOIN {S}.product p "
            f"  ON p.source=v.source AND p.product_id=v.product_id "
            f"WHERE v.on_deal = true{vw} "
            f"ORDER BY v.product_id, v.captured_at DESC"), prm).all()
        # Order by ACTUAL stored review rows so every listed product has reviews.
        d["top"] = c.execute(text(
            f"SELECT p.source, p.product_id, p.name, p.category, v.size, v.price, p.avg_rating, "
            f"       count(r.*) AS review_count "
            f"FROM {S}.product p "
            f"LEFT JOIN {S}.product_variant v ON v.source=p.source AND v.product_id=p.product_id "
            f"JOIN {S}.review r ON r.source=p.source AND r.product_id=p.product_id "
            f"WHERE 1=1{pw} "
            f"GROUP BY p.source, p.product_id, p.name, p.category, v.size, v.price, p.avg_rating "
            f"ORDER BY review_count DESC LIMIT 15"), prm).all()
        d["latest_reviews"] = c.execute(text(
            f"SELECT r.source, r.product_id, p.name, r.rating, r.title, r.body, r.author, r.review_date "
            f"FROM {S}.review r JOIN {S}.product p ON p.source=r.source AND p.product_id=r.product_id "
            f"WHERE 1=1{pw} "
            f"ORDER BY r.review_date DESC NULLS LAST LIMIT 12"), prm).all()
        # Raw preview: paginated products with every column (one variant each),
        # joined to the store table for store name/city/state/zip. Total distinct
        # products == d["products"], so reuse it to compute the page count.
        total = d["products"]
        pages = max(1, (total + PREVIEW_PER_PAGE - 1) // PREVIEW_PER_PAGE)
        page = min(max(1, page), pages)
        d["preview_page"] = page
        d["preview_pages"] = pages
        d["preview_total"] = total
        pv_prm = {**prm, "lim": PREVIEW_PER_PAGE, "off": (page - 1) * PREVIEW_PER_PAGE}
        d["preview"] = c.execute(text(
            f"SELECT DISTINCT ON (p.product_id) p.source, p.product_id, p.name, p.brand, "
            f"p.category, p.subcategory, v.size, v.price, v.list_price, v.on_deal, v.store_id, "
            f"st.name AS store_name, st.city AS store_city, st.state AS store_state, "
            f"st.zip AS store_zip, v.stock, v.in_stock, "
            f"p.avg_rating, p.review_count, p.is_new, p.attributes, p.ai_review_summary, p.url "
            f"FROM {S}.product p "
            f"LEFT JOIN {S}.product_variant v ON v.source=p.source AND v.product_id=p.product_id "
            f"LEFT JOIN {S}.store st ON st.source=p.source AND st.store_id=v.store_id "
            f"WHERE 1=1{pw} ORDER BY p.product_id LIMIT :lim OFFSET :off"),
            pv_prm).mappings().all()
        return d


def gather_product(source: str, pid: str) -> dict | None:
    with engine.connect() as c:
        prod = c.execute(text(
            f"SELECT source, product_id, name, brand, category, subcategory, url, "
            f"avg_rating, review_count, ai_review_summary, attributes, is_new FROM {S}.product "
            f"WHERE source = :s AND product_id = :p"),
            {"s": source, "p": pid}).mappings().first()
        if not prod:
            return None
        variants = c.execute(text(
            f"SELECT size, price, list_price, on_deal, in_stock, stock, store_id "
            f"FROM {S}.product_variant "
            f"WHERE source = :s AND product_id = :p ORDER BY price NULLS LAST"),
            {"s": source, "p": pid}).all()
        reviews = c.execute(text(
            f"SELECT rating, title, body, author, review_date, helpful_count "
            f"FROM {S}.review WHERE source = :s AND product_id = :p "
            f"ORDER BY helpful_count DESC NULLS LAST"), {"s": source, "p": pid}).all()
        return {"product": prod, "variants": variants, "reviews": reviews}


def gather_stores() -> dict:
    with engine.connect() as c:
        d: dict = {}
        d["total"] = c.execute(text(f"SELECT count(*) FROM {S}.store")).scalar() or 0
        d["with_zip"] = c.execute(text(
            f"SELECT count(*) FROM {S}.store WHERE zip IS NOT NULL")).scalar() or 0
        d["states"] = c.execute(text(
            f"SELECT count(DISTINCT state) FROM {S}.store WHERE state IS NOT NULL")).scalar() or 0
        d["by_state"] = c.execute(text(
            f"SELECT coalesce(state,'?') s, count(*) n FROM {S}.store "
            f"GROUP BY 1 ORDER BY n DESC")).all()
        d["rows"] = c.execute(text(
            f"SELECT source, store_id, name, address, city, state, zip, phone "
            f"FROM {S}.store ORDER BY state NULLS LAST, city NULLS LAST")).all()
        return d


_PREVIEW_COLS = ["source", "product_id", "name", "brand", "category", "subcategory",
                 "size", "price", "list_price", "on_deal", "store_id", "store_name",
                 "store_city", "store_state", "store_zip", "stock", "in_stock",
                 "avg_rating", "review_count", "is_new", "attributes",
                 "ai_review_summary", "url"]


def _cell(col, val) -> str:
    if val is None:
        return ""
    if col == "attributes":
        val = json.dumps(val)
    s = str(val)
    if len(s) > 80:
        s = s[:80] + "…"
    return html.escape(s)


def _preview_table(rows) -> str:
    if not rows:
        return '<div class="sub">no products yet — run the scraper</div>'
    head = "".join(f"<th>{c}</th>" for c in _PREVIEW_COLS)
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell(c, r.get(c))}</td>" for c in _PREVIEW_COLS) + "</tr>"
        for r in rows
    )
    return f'<div class="scroll"><table class="wide"><tr>{head}</tr>{body}</table></div>'


def _pager(page: int, pages: int, source: str | None) -> str:
    """Prev/next + numbered page links for the preview, preserving ?source."""
    if pages <= 1:
        return ""
    base = f"?source={html.escape(source)}&" if source else "?"

    def link(p, label, *, disabled=False, active=False):
        if disabled:
            return f'<span class="pg disabled">{label}</span>'
        cls = "pg active" if active else "pg"
        return f'<a class="{cls}" href="{base}page={p}#preview">{label}</a>'

    # windowed page numbers around the current page
    lo, hi = max(1, page - 2), min(pages, page + 2)
    nums = []
    if lo > 1:
        nums.append(link(1, "1"))
        if lo > 2:
            nums.append('<span class="pg ell">…</span>')
    for p in range(lo, hi + 1):
        nums.append(link(p, str(p), active=(p == page)))
    if hi < pages:
        if hi < pages - 1:
            nums.append('<span class="pg ell">…</span>')
        nums.append(link(pages, str(pages)))

    prev = link(page - 1, "‹ prev", disabled=(page <= 1))
    nxt = link(page + 1, "next ›", disabled=(page >= pages))
    return (f'<div class="pager">{prev}{"".join(nums)}{nxt}'
            f'<span class="pg-info">page {page} of {pages}</span></div>')


def _bar(label, n, total, color="#7c3aed"):
    pct = (n / total * 100) if total else 0
    return (
        f'<div class="row"><div class="lbl">{html.escape(str(label))}</div>'
        f'<div class="track"><div class="fill" style="width:{pct:.1f}%;background:{color}">'
        f'</div></div><div class="val">{n}</div></div>'
    )


def _stars(rating) -> str:
    try:
        r = int(round(float(rating)))
    except (TypeError, ValueError):
        return ""
    return '<span class="stars">' + "★" * r + "☆" * (5 - r) + "</span>"


def _review_card(r, *, show_product=False) -> str:
    head = ""
    if show_product:
        pid = getattr(r, "product_id", "")
        src = getattr(r, "source", "")
        head = (f'<a class="rp" href="/product?source={html.escape(str(src))}'
                f'&id={html.escape(str(pid))}">{html.escape(r.name or "")}</a>')
    return (
        f'<div class="rev">{head}'
        f'<div class="rh">{_stars(r.rating)} '
        f'<strong>{html.escape(r.title or "")}</strong> '
        f'<span class="by">{html.escape(r.author or "anon")}'
        f'{" · " + str(r.review_date)[:10] if getattr(r, "review_date", None) else ""}</span></div>'
        f'<div class="rb">{html.escape((r.body or "")[:400])}</div></div>'
    )


def _tabs(active: str | None, sources: list) -> str:
    items = [("All", "/")]
    items += [(s, f"/?source={s}") for s, _ in sources]
    items.append(("Stores", "/stores"))
    out = []
    for label, href in items:
        is_active = (active == label) or (active is None and label == "All")
        cls = "tab active" if is_active else "tab"
        out.append(f'<a class="{cls}" href="{href}">{html.escape(label)}</a>')
    return '<nav class="tabs">' + "".join(out) + "</nav>"


def render(d: dict) -> str:
    cov = (d["priced"] / d["variants"] * 100) if d["variants"] else 0
    cat_max = max([n for _, n in d["categories"]], default=1)
    rat_max = max([n for _, n in d["ratings"]], default=1)
    active = d.get("source")
    tabs = _tabs(active, d["sources"])

    cards = "".join(
        f'<div class="card"><div class="num">{v}</div><div class="cap">{k}</div></div>'
        for k, v in [
            ("Products", d["products"]), ("Variants", d["variants"]),
            ("Reviews", d["reviews"]), ("Stores", d["stores"]),
            ("Priced %", f"{cov:.0f}%"), ("Avg price", f"${d['avg_price']}"),
            ("With reviews", d["with_rating"]), ("On deal", d["on_deal"]),
            ("New", d["new"]),
        ]
    )
    cats = "".join(_bar(lbl, n, cat_max) for lbl, n in d["categories"])
    sub_max = max([n for _, n in d["subcats"]], default=1)
    subs = "".join(_bar(lbl, n, sub_max, "#3b82f6") for lbl, n in d["subcats"]) \
        or '<div class="sub">no data yet</div>'
    rats = "".join(_bar(f"{b}★", n, rat_max, "#f59e0b") for b, n in d["ratings"])
    src_max = max([n for _, n in d["sources"]], default=1)
    srcs = "".join(_bar(s, n, src_max, "#10b981") for s, n in d["sources"]) \
        or '<div class="sub">no data yet</div>'

    runs = "".join(
        f"<tr><td>{r.id}</td><td>{str(r.started_at)[:19]}</td>"
        f"<td>{str(r.finished_at)[:19] if r.finished_at else '…'}</td>"
        f"<td>{r.records_ingested}</td><td>{r.error_count}</td>"
        f"<td>{html.escape(r.notes or '')}</td></tr>"
        for r in d["runs"]
    )
    top = "".join(
        f'<tr><td><a href="/product?source={html.escape(str(r.source))}'
        f'&id={html.escape(str(r.product_id))}">{html.escape(r.name or "")}</a></td>'
        f"<td>{html.escape(r.category or '')}</td>"
        f"<td>{html.escape(r.size or '')}</td>"
        f"<td>{('$'+str(r.price)) if r.price is not None else '—'}</td>"
        f"<td>{round(r.avg_rating,2) if r.avg_rating else ''}</td>"
        f"<td>{r.review_count or 0}</td></tr>"
        for r in d["top"]
    )
    latest = "".join(_review_card(r, show_product=True) for r in d["latest_reviews"]) \
        or '<div class="sub">no reviews yet</div>'
    preview = _preview_table(d["preview"])

    def _pct_off(now, was):
        if now is None or not was:
            return ""
        try:
            return f"-{round((1 - now / was) * 100)}%"
        except (TypeError, ZeroDivisionError):
            return ""
    deals = "".join(
        f'<tr><td><a href="/product?source={html.escape(str(r.source))}'
        f'&id={html.escape(str(r.product_id))}">{html.escape(r.name or "")}</a></td>'
        f"<td>{html.escape(r.category or '')}</td>"
        f"<td>{html.escape(r.size or '')}</td>"
        f'<td class="now">${r.price}</td>'
        f'<td class="was">${r.list_price}</td>'
        f'<td class="off">{_pct_off(r.price, r.list_price)}</td>'
        f"<td>{html.escape(r.store_id or '')}</td></tr>"
        for r in d["deals"]
    )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Total Wine Scraper — Dashboard</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin:0; background:#0f0f14; color:#e7e7ea; }}
  header {{ padding:20px 28px; border-bottom:1px solid #26262e; }}
  h1 {{ font-size:18px; margin:0; }} .sub {{ color:#8a8a95; font-size:13px; margin-top:4px; }}
  main {{ padding:24px 28px; max-width:1100px; margin:0 auto; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:14px; margin-bottom:28px; }}
  .card {{ background:#17171f; border:1px solid #26262e; border-radius:12px; padding:16px 20px; min-width:120px; }}
  .num {{ font-size:26px; font-weight:700; }} .cap {{ color:#8a8a95; font-size:12px; margin-top:2px; text-transform:uppercase; letter-spacing:.04em; }}
  section {{ background:#17171f; border:1px solid #26262e; border-radius:12px; padding:18px 20px; margin-bottom:22px; }}
  h2 {{ font-size:14px; margin:0 0 14px; color:#c7c7d0; text-transform:uppercase; letter-spacing:.05em; }}
  .row {{ display:flex; align-items:center; gap:12px; margin:6px 0; }}
  .lbl {{ width:190px; font-size:13px; color:#c7c7d0; }} .val {{ width:44px; text-align:right; font-variant-numeric:tabular-nums; font-size:13px; }}
  .track {{ flex:1; background:#26262e; border-radius:6px; height:16px; overflow:hidden; }}
  .fill {{ height:100%; border-radius:6px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid #26262e; }}
  th {{ color:#8a8a95; font-weight:600; text-transform:uppercase; font-size:11px; letter-spacing:.04em; }}
  td {{ font-variant-numeric:tabular-nums; }}
  a {{ color:#a78bfa; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
  .rev {{ border-bottom:1px solid #26262e; padding:12px 0; }}
  .rev:last-child {{ border-bottom:0; }}
  .rp {{ display:block; font-weight:600; margin-bottom:4px; }}
  .rh {{ font-size:13px; margin-bottom:4px; }} .stars {{ color:#f59e0b; letter-spacing:1px; }}
  .by {{ color:#8a8a95; }} .rb {{ font-size:13px; color:#c7c7d0; line-height:1.5; }}
  .back {{ font-size:13px; }}
  .tabs {{ display:flex; gap:8px; margin-top:14px; flex-wrap:wrap; }}
  .tab {{ padding:6px 14px; border:1px solid #26262e; border-radius:999px; font-size:13px;
         color:#c7c7d0; background:#17171f; }}
  .tab.active {{ background:#7c3aed; border-color:#7c3aed; color:#fff; }}
  .tab:hover {{ text-decoration:none; border-color:#7c3aed; }}
  .scroll {{ overflow-x:auto; }}
  .pager {{ display:flex; gap:6px; align-items:center; margin-top:14px; flex-wrap:wrap; }}
  .pg {{ padding:5px 11px; border:1px solid #26262e; border-radius:8px; font-size:13px;
        color:#c7c7d0; background:#17171f; }}
  .pg.active {{ background:#7c3aed; border-color:#7c3aed; color:#fff; }}
  .pg:hover {{ text-decoration:none; border-color:#7c3aed; }}
  .pg.disabled {{ color:#4a4a52; }} .pg.ell {{ border:0; background:none; }}
  .pg-info {{ color:#8a8a95; font-size:12px; margin-left:8px; }}
  td.now {{ color:#f87171; font-weight:700; }}
  td.was {{ color:#8a8a95; text-decoration:line-through; }}
  td.off {{ color:#34d399; font-weight:600; }}
  table.wide {{ font-size:12px; }}
  table.wide th, table.wide td {{ white-space:nowrap; max-width:300px; overflow:hidden; text-overflow:ellipsis; }}
</style></head><body>
<header><h1>\U0001f377 Web Scraper — Ingestion Dashboard</h1>
<div class="sub">schema <code>{S}</code> &middot; {("source: <b>"+html.escape(active)+"</b>") if active else "all sources"} &middot; <a href="javascript:location.reload()">↻ reload</a></div>
{tabs}</header>
<main>
  <div class="cards">{cards}</div>
  {f'''<section id="deals"><h2>On deal — {len(d["deals"])} products <span class="sub">(sale price / was)</span></h2>
    <table><tr><th>name</th><th>category</th><th>size</th><th>now</th><th>was</th><th>off</th><th>store</th></tr>{deals}</table>
  </section>''' if d["deals"] else ""}
  <section id="preview"><h2>Data preview — {d.get('preview_total', 0)} products, all columns</h2>
    {preview}{_pager(d.get('preview_page', 1), d.get('preview_pages', 1), active)}</section>
  {"" if active else f'<section><h2>Products by source</h2>{srcs}</section>'}
  <section><h2>Products by category</h2>{cats or '<div class="sub">no data yet</div>'}</section>
  <section><h2>Products by subcategory</h2>{subs}</section>
  <section><h2>Rating distribution</h2>{rats or '<div class="sub">no ratings yet</div>'}</section>
  <section><h2>Top-reviewed products <span class="sub">(click a name for its reviews)</span></h2>
    <table><tr><th>name</th><th>category</th><th>size</th><th>price</th><th>rating</th><th>reviews</th></tr>{top}</table>
  </section>
  <section><h2>Latest reviews</h2>{latest}</section>
  <section><h2>Recent scrape runs</h2>
    <table><tr><th>#</th><th>started</th><th>finished</th><th>ingested</th><th>errors</th><th>notes</th></tr>{runs}</table>
  </section>
</main></body></html>"""


_STYLE = """
  * { box-sizing: border-box; }
  body { font-family:-apple-system,system-ui,sans-serif; margin:0; background:#0f0f14; color:#e7e7ea; }
  header { padding:20px 28px; border-bottom:1px solid #26262e; }
  h1 { font-size:18px; margin:0; } .sub { color:#8a8a95; font-size:13px; margin-top:4px; }
  main { padding:24px 28px; max-width:1100px; margin:0 auto; }
  .cards { display:flex; flex-wrap:wrap; gap:14px; margin-bottom:28px; }
  .card { background:#17171f; border:1px solid #26262e; border-radius:12px; padding:16px 20px; min-width:120px; }
  .num { font-size:26px; font-weight:700; } .cap { color:#8a8a95; font-size:12px; margin-top:2px; text-transform:uppercase; letter-spacing:.04em; }
  section { background:#17171f; border:1px solid #26262e; border-radius:12px; padding:18px 20px; margin-bottom:22px; }
  h2 { font-size:14px; margin:0 0 14px; color:#c7c7d0; text-transform:uppercase; letter-spacing:.05em; }
  .row { display:flex; align-items:center; gap:12px; margin:6px 0; }
  .lbl { width:190px; font-size:13px; color:#c7c7d0; } .val { width:44px; text-align:right; font-variant-numeric:tabular-nums; font-size:13px; }
  .track { flex:1; background:#26262e; border-radius:6px; height:16px; overflow:hidden; }
  .fill { height:100%; border-radius:6px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:7px 10px; border-bottom:1px solid #26262e; }
  th { color:#8a8a95; font-weight:600; text-transform:uppercase; font-size:11px; letter-spacing:.04em; }
  a { color:#a78bfa; text-decoration:none; } a:hover { text-decoration:underline; }
  .tabs { display:flex; gap:8px; margin-top:14px; flex-wrap:wrap; }
  .tab { padding:6px 14px; border:1px solid #26262e; border-radius:999px; font-size:13px; color:#c7c7d0; background:#17171f; }
  .tab.active { background:#7c3aed; border-color:#7c3aed; color:#fff; }
  .tab:hover { text-decoration:none; border-color:#7c3aed; }
"""


def render_stores(d: dict, sources: list) -> str:
    st_max = max([n for _, n in d["by_state"]], default=1)
    cards = "".join(
        f'<div class="card"><div class="num">{v}</div><div class="cap">{k}</div></div>'
        for k, v in [("Stores", d["total"]), ("With address/zip", d["with_zip"]),
                     ("States", d["states"])]
    )
    bars = "".join(_bar(s, n, st_max, "#10b981") for s, n in d["by_state"]) \
        or '<div class="sub">no stores yet — run sync-stores</div>'
    table = "".join(
        f"<tr><td>{html.escape(r.state or '')}</td><td>{html.escape(r.name or '')}</td>"
        f"<td>{html.escape(r.address or '')}</td><td>{html.escape(r.city or '')}</td>"
        f"<td>{html.escape(r.zip or '')}</td><td>{html.escape(r.phone or '')}</td>"
        f"<td>{html.escape(str(r.store_id))}</td></tr>"
        for r in d["rows"]
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Web Scraper — Stores</title>
<style>{_STYLE}</style></head><body>
<header><h1>\U0001f377 Web Scraper — Stores</h1>
<div class="sub">schema <code>{S}</code> &middot; store locator dataset</div>
{_tabs("Stores", sources)}</header>
<main>
  <div class="cards">{cards}</div>
  <section><h2>Stores by state</h2>{bars}</section>
  <section><h2>All stores ({d['total']})</h2>
    <table><tr><th>state</th><th>name</th><th>address</th><th>city</th><th>zip</th><th>phone</th><th>id</th></tr>{table}</table>
  </section>
</main></body></html>"""


def render_product(pv: dict) -> str:
    p = pv["product"]
    variants = "".join(
        f"<tr><td>{html.escape(v.size or '')}</td>"
        f"<td>{('$'+str(v.price)) if v.price is not None else '—'}</td>"
        f"<td>{('$'+str(v.list_price)) if (v.on_deal and v.list_price is not None) else ''}</td>"
        f"<td>{'in stock' if v.in_stock else ('out' if v.in_stock is False else '?')}</td>"
        f"<td>{v.stock if v.stock is not None else ''}</td>"
        f"<td>{html.escape(v.store_id or '')}</td></tr>"
        for v in pv["variants"]
    ) or "<tr><td colspan=6 class='sub'>no variants</td></tr>"
    reviews = "".join(_review_card(r) for r in pv["reviews"]) \
        or '<div class="sub">no reviews stored for this product</div>'
    summ = (f'<section><h2>AI review summary</h2><div class="rb">'
            f'{html.escape(p["ai_review_summary"])}</div></section>') if p["ai_review_summary"] else ""
    attrs = p.get("attributes") or {}
    details = ("<table>" + "".join(
        f"<tr><th style='width:180px'>{html.escape(str(k))}</th>"
        f"<td>{html.escape(str(v))}</td></tr>" for k, v in attrs.items()) + "</table>"
    ) if attrs else '<div class="sub">no product details</div>'
    src = (f'<a href="{html.escape(p["url"])}" target="_blank">view on totalwine.com ↗</a>'
           if p["url"] else "")
    badges = ""
    if p.get("is_new"):
        badges += ' <span class="badge new">NEW</span>'
    if any(getattr(v, "on_deal", None) for v in pv["variants"]):
        badges += ' <span class="badge deal">DEAL</span>'

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(p['name'] or 'product')} — reviews</title>
<style>
  body {{ font-family:-apple-system,system-ui,sans-serif; margin:0; background:#0f0f14; color:#e7e7ea; }}
  header {{ padding:20px 28px; border-bottom:1px solid #26262e; }}
  h1 {{ font-size:20px; margin:0 0 4px; }} .sub {{ color:#8a8a95; font-size:13px; }}
  main {{ padding:24px 28px; max-width:820px; margin:0 auto; }}
  section {{ background:#17171f; border:1px solid #26262e; border-radius:12px; padding:18px 20px; margin-bottom:22px; }}
  h2 {{ font-size:14px; margin:0 0 14px; color:#c7c7d0; text-transform:uppercase; letter-spacing:.05em; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid #26262e; }}
  a {{ color:#a78bfa; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
  .stars {{ color:#f59e0b; letter-spacing:1px; }} .by {{ color:#8a8a95; }}
  .rev {{ border-bottom:1px solid #26262e; padding:12px 0; }} .rev:last-child {{ border-bottom:0; }}
  .rh {{ font-size:13px; margin-bottom:4px; }} .rb {{ font-size:13px; color:#c7c7d0; line-height:1.5; }}
  .badge {{ font-size:11px; font-weight:700; padding:2px 8px; border-radius:999px; vertical-align:middle; }}
  .badge.new {{ background:#2563eb; color:#fff; }} .badge.deal {{ background:#dc2626; color:#fff; }}
</style></head><body>
<header>
  <div class="back"><a href="/">← dashboard</a></div>
  <h1>{html.escape(p['name'] or '')}{badges}</h1>
  <div class="sub">{html.escape(p['brand'] or '')} &middot; {html.escape(p['category'] or '')}
    {'/ ' + html.escape(p['subcategory']) if p['subcategory'] else ''} &middot;
    {_stars(p['avg_rating'])} {p['avg_rating'] or ''} ({p['review_count'] or 0} reviews) &middot; {src}</div>
</header>
<main>
  <section><h2>Sizes &amp; price</h2><table>
    <tr><th>size</th><th>price</th><th>was</th><th>stock</th><th>qty</th><th>store</th></tr>{variants}</table></section>
  <section><h2>Product details</h2>{details}</section>
  {summ}
  <section><h2>Reviews ({len(pv['reviews'])})</h2>{reviews}</section>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                q = parse_qs(parsed.query)
                src = (q.get("source") or [None])[0]
                try:
                    page = int((q.get("page") or ["1"])[0])
                except ValueError:
                    page = 1
                body = render(gather(src, page))
            elif parsed.path == "/stores":
                body = render_stores(gather_stores(), gather()["sources"])
            elif parsed.path == "/product":
                q = parse_qs(parsed.query)
                pid = (q.get("id") or [""])[0]
                src = (q.get("source") or ["totalwine"])[0]
                pv = gather_product(src, pid)
                body = render_product(pv) if pv else "<pre>product not found</pre>"
            else:
                self.send_error(404)
                return
        except Exception as e:  # noqa: BLE001
            body = f"<pre>error: {html.escape(str(e))}</pre>"
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # silence per-request logging
        pass


def serve(port: int = 8000) -> None:
    print(f"Dashboard on http://localhost:{port}  (Ctrl-C to stop)")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    serve(ap.parse_args().port)


if __name__ == "__main__":
    main()
