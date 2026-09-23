"""Parse the product-reviews API payload into validated Review records.

Total Wine proxies Bazaarvoice-shaped review data. Confirmed shape
(spike_out/network/resp_028.json):

    {"results": [{"Id": "...", "ProductId": "...", "Rating": 5,
                  "Title": "...", "ReviewText": "...", "UserNickname": "...",
                  "SubmissionTime": "2025-...", "TotalPositiveFeedbackCount": 3,
                  ...}],
     "totalResults": 21, "limit": 10, "offset": 0, "hasMore": true}

The AI review summary comes from a separate endpoint
(.../reviews/summary?formatType=paragraph) with shape:
    {"summary": "...", "title": "...", "disclaimer": "...", "createdAt": "..."}
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import ReviewIn


def parse_reviews(payload: dict, *, product_id: str) -> list[ReviewIn]:
    out: list[ReviewIn] = []
    for r in payload.get("results", []):
        rid = str(r.get("Id") or "").strip()
        if not rid:
            continue
        try:
            out.append(
                ReviewIn(
                    review_id=rid,
                    product_id=product_id,
                    rating=r.get("Rating"),
                    title=r.get("Title"),
                    body=r.get("ReviewText"),
                    author=r.get("UserNickname"),
                    review_date=_parse_date(r.get("SubmissionTime")),
                    helpful_count=r.get("TotalPositiveFeedbackCount"),
                )
            )
        except Exception:
            continue
    return out


def parse_summary(payload: dict) -> str | None:
    """Extract the AI review summary text (empty string -> None)."""
    text = (payload or {}).get("summary") or ""
    return text.strip() or None


def _parse_date(val) -> datetime | None:
    if not val:
        return None
    if isinstance(val, (int, float)):
        ts = val / 1000 if val > 1e12 else val
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    # Total Wine's SubmissionTime is ISO-8601 with a tz offset, e.g.
    # "2024-09-08T20:02:55.000+00:00". fromisoformat handles the offset (and 'Z'
    # on 3.11+); assume UTC if the value carries no timezone.
    s = str(val).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
