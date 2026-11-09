"""Άνοιγμα σελίδων με πραγματικό browser (Microsoft Edge στα Windows) μέσω Playwright."""
from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

COOKIE_BUTTON_SELECTORS = [
    "#sp-cc-accept",                                   # Amazon
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyButtonAccept",
    "#gdpr-banner-accept",                             # eBay
    "button#accept-all", "button.accept-all", "[data-testid='uc-accept-all-button']",
    "button[aria-label*='Accept']", "button[aria-label*='Αποδοχή']",
]
COOKIE_BUTTON_TEXTS = [
    "Αποδοχή όλων", "Αποδοχή", "Συμφωνώ", "Αποδέχομαι", "Accept all", "Accept All", "Accept",
    "Alle akzeptieren", "Akzeptieren", "Accetta tutto", "Accetta", "Tout accepter", "Accepter",
    "Aceptar todo", "Aceptar", "OK",
]

OVERLAY_JS = """
([shop, title, price, when]) => {
  const old = document.getElementById('__pt_overlay'); if (old) old.remove();
  const d = document.createElement('div');
  d.id = '__pt_overlay';
  d.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;' +
    'background:rgba(17,24,39,.92);color:#fff;font:600 15px/1.4 Segoe UI,Arial,sans-serif;' +
    'padding:10px 16px;display:flex;gap:18px;align-items:center;box-shadow:0 -2px 8px rgba(0,0,0,.3)';
  const mk = (t, s) => { const e = document.createElement('span'); e.textContent = t; if (s) e.style.cssText = s; d.appendChild(e); };
  mk('Price Tracker', 'color:#93c5fd');
  mk(shop);
  mk(title, 'flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-weight:400');
  mk(price, 'color:#86efac;font-size:18px');
  mk(when, 'font-weight:400;color:#d1d5db');
  document.body.appendChild(d);
}
"""


@dataclass
class PageResult:
    url: str
    final_url: str
    html: str
    status: int | None


class Browser:
    """Ένας browser για όλους τους ελέγχους ενός κύκλου. Χρησιμοποιείται μόνο από ένα thread."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self._last_host_time: dict[str, float] = {}

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        errors = []
        exe = os.environ.get("PRICETRACKER_BROWSER")
        attempts = []
        if exe:
            attempts.append({"executable_path": exe})
        attempts += [{"channel": "msedge"}, {"channel": "chrome"}, {}]
        for opts in attempts:
            try:
                self._browser = self._pw.chromium.launch(
                    headless=self.headless,
                    args=["--disable-blink-features=AutomationControlled"],
                    **opts,
                )
                break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{opts or 'chromium'}: {e}")
        if self._browser is None:
            self._pw.stop()
            raise RuntimeError("Δεν βρέθηκε browser (Edge/Chrome). " + " | ".join(errors))
        ua = self._browser.new_page().evaluate("navigator.userAgent")
        ua = ua.replace("HeadlessChrome", "Chrome").replace("HeadlessEdg", "Edg")
        self._context = self._browser.new_context(
            user_agent=ua,
            locale="el-GR",
            timezone_id="Europe/Athens",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "el-GR,el;q=0.9,en;q=0.8,de;q=0.7"},
        )
        self._context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

    def close(self):
        for obj in (self._context, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = self._context = None

    def _polite_wait(self, url: str):
        from .shops import host_of

        host = host_of(url)
        last = self._last_host_time.get(host)
        if last:
            gap = time.time() - last
            need = random.uniform(3, 6)
            if gap < need:
                time.sleep(need - gap)
        self._last_host_time[host] = time.time()

    def _accept_cookies(self, page):
        for sel in COOKIE_BUTTON_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=300):
                    loc.click(timeout=1500)
                    page.wait_for_timeout(500)
                    return
            except Exception:  # noqa: BLE001
                continue
        for txt in COOKIE_BUTTON_TEXTS:
            try:
                loc = page.get_by_role("button", name=txt, exact=True).first
                if loc.is_visible(timeout=200):
                    loc.click(timeout=1500)
                    page.wait_for_timeout(500)
                    return
            except Exception:  # noqa: BLE001
                continue

    def open(self, url: str, timeout_ms: int = 45000):
        """Ανοίγει τη σελίδα και επιστρέφει (page, PageResult). Ο καλών κλείνει τη σελίδα."""
        self._polite_wait(url)
        page = self._context.new_page()
        resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        self._accept_cookies(page)
        page.wait_for_timeout(800)
        res = PageResult(url=url, final_url=page.url, html=page.content(),
                         status=resp.status if resp else None)
        return page, res

    @staticmethod
    def screenshot(page, path: Path, shop: str, title: str, price: str, when: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            page.evaluate("window.scrollTo(0, 0)")
            page.evaluate(OVERLAY_JS, [shop, title or "", price, when])
        except Exception:  # noqa: BLE001
            log.debug("overlay failed", exc_info=True)
        page.screenshot(path=str(path), type="jpeg", quality=75, full_page=False)
