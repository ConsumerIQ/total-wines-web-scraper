"""Warm patchright/Chrome session that fetches product data by navigating the
product page and intercepting the JSON the SPA fires itself.

Why this and not direct API calls: totalwine.com uses PerimeterX first-party
mode, which signs the page's own XHRs. Hand-issued requests (curl,
context.request, in-page fetch) all 403. Loading the page like a human and
capturing getProduct / reviews / summary responses is the reliable path
(verified in the Phase-0 spike).

One long-lived session handles many products; the persistent profile keeps the
PerimeterX token warm so the Press & Hold challenge is rare after the first solve.

Usage:
    with TotalWineSession() as s:
        data = s.fetch("https://www.totalwine.com/.../p/140521750")
        # data = {"product": {...}|None, "reviews": {...}|None, "summary": {...}|None}

Note: run HEADED (default). PerimeterX blocks headless; on a headless server use
a virtual display (xvfb-run) rather than headless=True.
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path

DEFAULT_PROFILE = Path("spike_out/px_profile_stealth")
HOMEPAGE = "https://www.totalwine.com/"


class PXBlocked(Exception):
    """Raised when a product page could not clear PerimeterX."""


class TotalWineSession:
    # Resource types we never need (we only want the JSON XHRs). Blocking these
    # roughly halves page-load time and bandwidth.
    _BLOCK_TYPES = {"image", "media", "font", "stylesheet"}

    def __init__(
        self,
        *,
        profile_dir: Path | str = DEFAULT_PROFILE,
        channel: str = "chrome",
        headless: bool = False,
        max_wait_ms: int = 10000,
        capture_grace_ms: int = 3000,
        delay_s: float = 0.0,
        block_resources: bool = True,
        proxy: str | None = None,
        human: bool = True,
        rewarm_ms: int = 8000,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.channel = channel
        self.headless = headless
        self.proxy = proxy
        self.human = human            # small mouse/scroll to look less robotic
        self.rewarm_ms = rewarm_ms    # settle time on a recovery homepage load
        self.max_wait_ms = max_wait_ms          # max wait for getProduct to fire
        self.capture_grace_ms = capture_grace_ms  # extra wait for reviews/summary
        self.delay_s = delay_s                   # polite pacing between products
        self.block_resources = block_resources
        self._pw = None
        self._ctx = None
        self._page = None
        self._cap: dict[str, dict] = {}

    # -- lifecycle ---------------------------------------------------------- #
    def __enter__(self) -> "TotalWineSession":
        from patchright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        launch_kwargs: dict = dict(
            user_data_dir=str(self.profile_dir),
            channel=self.channel,
            headless=self.headless,
            no_viewport=True,
        )
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}
        self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        if self.block_resources:
            self._ctx.route("**/*", self._route)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.on("response", self._on_response)
        return self

    def _route(self, route) -> None:
        try:
            if route.request.resource_type in self._BLOCK_TYPES:
                return route.abort()
            return route.continue_()
        except Exception:
            try:
                return route.continue_()
            except Exception:
                return None

    def __exit__(self, *exc) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    # -- interception ------------------------------------------------------- #
    def _on_response(self, resp) -> None:
        u = resp.url
        if resp.status != 200:
            return
        try:
            if "/getProduct/" in u:
                self._cap["product"] = resp.json()
            elif "/product-reviews/v1/products/" in u and "/reviews?" in u:
                self._cap["reviews"] = resp.json()
            elif "/reviews/summary" in u:
                self._cap["summary"] = resp.json()
        except Exception:
            pass

    # -- fetch -------------------------------------------------------------- #
    def _wait_for(self, key: str, timeout_ms: int, poll_ms: int = 200) -> bool:
        """Poll until `key` is captured or the timeout elapses."""
        waited = 0
        while key not in self._cap and waited < timeout_ms:
            self._page.wait_for_timeout(poll_ms)
            waited += poll_ms
        return key in self._cap

    def _fidget(self) -> None:
        """A little human-like motion so the behavioural score stays low."""
        if not self.human:
            return
        try:
            self._page.mouse.move(random.randint(120, 900), random.randint(120, 600))
            self._page.wait_for_timeout(random.randint(150, 500))
            self._page.mouse.wheel(0, random.randint(400, 1100))
        except Exception:
            pass

    def warm_up(self, max_seconds: int = 60) -> bool:
        """Open the homepage so the PerimeterX sensor can establish the session
        token before we hit product pages, and give the user a window to solve a
        Press & Hold if one appears.

        IMPORTANT: even when the homepage shows no challenge, the PX token isn't
        set instantly — the sensor JS needs a few seconds to run and POST to the
        collector. So we ALWAYS wait `rewarm_ms` first; otherwise product pages
        hard-403 for lack of a token. Then, only if a Press & Hold is actually
        visible, keep polling up to `max_seconds` for the user to complete it.
        """
        try:
            self._page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            pass
        self._fidget()
        self._page.wait_for_timeout(self.rewarm_ms)   # let the sensor set the token
        end = time.time() + max_seconds
        while True:
            if not self.challenge_visible():
                return True
            if time.time() >= end:
                return False
            self._page.wait_for_timeout(2000)

    def set_store(self, store_info_url: str, store_id: str | None = None) -> bool:
        """Pin a store by clicking "Set As My Store" on its store-info page.

        This is the only reliable pin (fires CHANGE_LOCATION server-side); the
        cookie alone isn't honored. Returns True if the store cookie reflects
        the target afterwards.
        """
        try:
            self._page.goto(store_info_url, wait_until="domcontentloaded", timeout=60_000)
            self._page.wait_for_timeout(2500)
            self._fidget()
            self._page.get_by_text(re.compile("set as my store", re.I)).first.click(timeout=8000)
            self._page.wait_for_timeout(3000)
        except Exception:
            return False
        info = ""
        try:
            info = {c["name"]: c["value"] for c in self._ctx.cookies()}.get(
                "twm-userStoreInformation", "")
        except Exception:
            pass
        return f"ispStore~{store_id}" in info if store_id else bool(info)

    def current_store_id(self) -> str | None:
        """The store the session is currently pinned to (from the store cookie).

        For a run with no explicit --store, the profile's default store is what
        getProduct stamps on variants — so resume should scope to this id, not
        skip the product globally."""
        try:
            info = {c["name"]: c["value"] for c in self._ctx.cookies()}.get(
                "twm-userStoreInformation", "")
        except Exception:
            return None
        m = re.search(r"ispStore~(\d+)", info)
        return m.group(1) if m else None

    def get_json(self, url: str) -> dict | None:
        """Navigate to a JSON API endpoint (PX-gated to curl) and parse the
        body — works because the browser context is PX-cleared."""
        import json as _json
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            self._page.wait_for_timeout(600)
            return _json.loads(self._page.evaluate("document.body.innerText"))
        except Exception:
            return None

    def fetch_reviews(self, product_id: str, department: str | None = None,
                      limit: int = 30) -> dict | None:
        """Fetch the reviews LIST directly through the warm context.

        Total Wine stopped firing the reviews-list XHR on scroll (only
        summary/images fire now), so interception no longer catches reviews.
        The PX-cleared context still returns the JSON when we request the
        endpoint ourselves. Shape matches what parse_reviews expects
        ({"results": [...], "totalResults": N})."""
        if not product_id:
            return None
        # Mirror the page's own request exactly (Include comma URL-encoded,
        # department before Sort) so the PX-fronted endpoint accepts it.
        dept = f"&department={department}" if department else ""
        url = ("https://www.totalwine.com/product/api/product/product-reviews/v1/"
               f"products/{product_id}/reviews?limit={limit}&offset=0"
               "&FilteredStats=Reviews&Include=Products%2CAuthors&Stats=Reviews"
               f"{dept}&Sort=Helpfulness:desc")
        return self.get_json(url)

    def rewarm(self, wait_ms: int | None = None) -> bool:
        """Recover a cold/blocked session: browse the homepage like a human and
        let the PX sensor re-run (patchright usually clears the invisible
        challenge). Returns True if the homepage came back un-blocked.
        """
        wait_ms = self.rewarm_ms if wait_ms is None else wait_ms
        try:
            self._page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=60_000)
            self._page.wait_for_timeout(wait_ms)
            self._fidget()
            html = self._page.content()
            return '"appId": "PXFF0j69T5"' not in html and "Press & Hold" not in html
        except Exception:
            return False

    def challenge_visible(self) -> bool:
        """True if a solvable Press & Hold / captcha is actually on screen (vs a
        hard 403 deny, which offers nothing to solve)."""
        try:
            html = self._page.content()
        except Exception:
            return False
        return "Press & Hold" in html or "px-captcha" in html

    def wait_for_solve(self, seconds: int) -> dict | None:
        """After a product blocked, keep the challenge page on screen and wait
        for the USER to complete the Press & Hold. No navigation happens while
        waiting, so the challenge the user sees stays put. Once they solve it the
        getProduct XHR fires and we capture it; then (only if the product has
        reviews) we fetch the reviews list, which does navigate away — that's
        fine, the challenge is already cleared.
        """
        if self._wait_for("product", seconds * 1000):
            prod = self._cap.get("product") or {}
            if (prod.get("customerReviewsCount") or 0) > 0:
                self._wait_for("summary", self.capture_grace_ms)
                if "reviews" not in self._cap:
                    revs = self.fetch_reviews(str(prod.get("id") or ""), prod.get("department"))
                    if revs:
                        self._cap["reviews"] = revs
            return {
                "product": self._cap.get("product"),
                "reviews": self._cap.get("reviews"),
                "summary": self._cap.get("summary"),
            }
        return None

    def fetch(self, url: str, *, retries: int = 1) -> dict:
        """Navigate to a product page and return the intercepted JSON payloads.

        Event-driven: returns as soon as getProduct is captured (typically
        1-2s), then a short grace window to catch reviews/summary. Raises
        PXBlocked if getProduct never arrives within max_wait_ms.
        """
        for attempt in range(retries + 1):
            self._cap = {}
            self._page.goto(url, wait_until="commit", timeout=60_000)
            self._fidget()
            if self._wait_for("product", self.max_wait_ms):
                break
            if attempt < retries:
                self._page.wait_for_timeout(2000)
        if "product" not in self._cap:
            raise PXBlocked(url)

        # Only chase reviews/summary when the product actually has reviews — the
        # AI summary is derived from reviews, so a 0-review product would just
        # burn the full grace window and an extra navigation for nothing.
        prod = self._cap.get("product") or {}
        if (prod.get("customerReviewsCount") or 0) > 0:
            # The summary XHR still fires on scroll — nudge the page to catch it.
            if "summary" not in self._cap:
                try:
                    self._page.evaluate(
                        "window.scrollTo(0, document.body.scrollHeight * 0.75)")
                except Exception:
                    pass
                self._wait_for("summary", self.capture_grace_ms)
            # The reviews-list XHR no longer auto-fires; request it directly
            # through the warm context (navigates away, so do this last).
            if "reviews" not in self._cap:
                revs = self.fetch_reviews(str(prod.get("id") or ""), prod.get("department"))
                if revs:
                    self._cap["reviews"] = revs

        if self.delay_s:
            time.sleep(self.delay_s)
        return {
            "product": self._cap.get("product"),
            "reviews": self._cap.get("reviews"),
            "summary": self._cap.get("summary"),
        }
