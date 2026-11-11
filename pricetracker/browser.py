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

COOKIE_JS = """
([selectors, texts]) => {
  const visible = (e) => { const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  for (const sel of selectors) {
    try { const e = document.querySelector(sel); if (e && visible(e)) { e.click(); return true; } } catch (_) {}
  }
  const banner = /cookie|consent|gdpr|cmp|privacy|onetrust|didomi|usercentrics|cookiebot/i;
  const inBanner = (e) => { for (let a = e; a && a !== document.body; a = a.parentElement) {
      if (banner.test((a.id || '') + ' ' + (typeof a.className === 'string' ? a.className : '')
                      + ' ' + (a.getAttribute('aria-label') || ''))) return true; } return false; };
  const generic = new Set(['OK', 'Accept', 'Αποδοχή', 'Συμφωνώ', 'Accetta', 'Accepter', 'Aceptar', 'Akzeptieren']);
  const buttons = Array.from(document.querySelectorAll('button, [role=button], a.button, input[type=button], input[type=submit]'));
  for (const t of texts) {
    for (const b of buttons) {
      const label = (b.innerText || b.value || '').trim();
      if (label !== t || !visible(b)) continue;
      if (generic.has(t) && !inBanner(b)) continue;
      b.click(); return true;
    }
  }
  return false;
}
"""

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

    _ua_cache: dict[str, str] = {}
    # Κοινό για όλους τους browsers της εφαρμογής: η απόσταση ανάμεσα σε δύο σελίδες του ίδιου καταστήματος
    # ισχύει και από εργασία σε εργασία (π.χ. έλεγχος και αμέσως μετά αναζήτηση)
    _last_host_time: dict[str, float] = {}
    BLOCK_TYPES = {"font", "media"}  # οι εικόνες μένουν: το screenshot βγαίνει από την ίδια σελίδα

    def __init__(self, headless: bool = True, profile_dir: Path | None = None):
        self.headless = headless
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self._pw = None
        self._context = None
        self._launch_opts = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def _launch(self, opts: dict, user_agent: str | None):
        common = dict(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            locale="el-GR",
            timezone_id="Europe/Athens",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "el-GR,el;q=0.9,en;q=0.8,de;q=0.7"},
            **opts,
        )
        if user_agent:
            common["user_agent"] = user_agent
        if self.profile_dir:
            # Μόνιμο προφίλ: cookies (π.χ. αποδοχή cookies, λυμένο captcha) κρατιούνται ανάμεσα στους ελέγχους
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            return self._pw.chromium.launch_persistent_context(str(self.profile_dir), **common)
        browser = self._pw.chromium.launch(headless=common.pop("headless"), args=common.pop("args"),
                                           **{k: common.pop(k) for k in list(common) if k in ("channel", "executable_path")})
        ctx = browser.new_context(**common)
        ctx._pt_browser = browser  # για να κλείσει μαζί
        return ctx

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
            key = repr(opts)
            try:
                self._context = self._launch(opts, self._ua_cache.get(key))
                if key not in self._ua_cache:
                    page = self._context.pages[0] if self._context.pages else self._context.new_page()
                    ua = page.evaluate("navigator.userAgent")
                    clean = ua.replace("HeadlessChrome", "Chrome").replace("HeadlessEdg", "Edg")
                    self._ua_cache[key] = clean
                    if clean != ua:  # ξανά με «κανονικό» user agent
                        self._close_context()
                        self._context = self._launch(opts, clean)
                self._launch_opts = opts
                break
            except Exception as e:  # noqa: BLE001
                self._close_context()
                errors.append(f"{opts or 'chromium'}: {str(e).splitlines()[0]}")
        if self._context is None:
            self._pw.stop()
            self._pw = None
            raise RuntimeError("Δεν βρέθηκε browser (Edge/Chrome). " + " | ".join(errors))
        self._context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        try:  # λιγότερα δεδομένα και γρηγορότεροι έλεγχοι: χωρίς γραμματοσειρές και βίντεο
            self._context.route("**/*", self._route)
        except Exception:  # noqa: BLE001
            log.debug("route failed", exc_info=True)

    @classmethod
    def _route(cls, route):
        try:
            if route.request.resource_type in cls.BLOCK_TYPES:
                route.abort()
            else:
                route.continue_()
        except Exception:  # noqa: BLE001
            pass

    def _close_context(self):
        ctx, self._context = self._context, None
        if ctx is None:
            return
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
        br = getattr(ctx, "_pt_browser", None)
        if br is not None:
            try:
                br.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self):
        self._close_context()
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = None

    def restart(self, headless: bool):
        """Ξανανοίγει τον browser κρυφό ή ορατό, με το ίδιο προφίλ."""
        self.close()
        self.headless = headless
        self.start()

    @staticmethod
    def wait_until(page, done, timeout_s: int = 180, every_s: float = 3.0) -> bool:
        """Περιμένει (π.χ. τον χρήστη να λύσει ένα captcha) μέχρι το done(html) να γίνει True."""
        end = time.time() + timeout_s
        while time.time() < end:
            try:
                if done(page.content()):
                    return True
            except Exception:  # noqa: BLE001
                pass
            if page.is_closed():
                return False
            time.sleep(every_s)
        return False

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
        """Πατά «Αποδοχή» σε banner cookies, με έναν μόνο έλεγχο στη σελίδα (όχι 26 αναμονές). Τα γενικά
        κουμπιά («OK», «Accept») μετράνε μόνο μέσα σε κάτι που μοιάζει με banner cookies."""
        try:
            clicked = page.evaluate(COOKIE_JS, [COOKIE_BUTTON_SELECTORS, COOKIE_BUTTON_TEXTS])
        except Exception:  # noqa: BLE001
            return
        if clicked:
            page.wait_for_timeout(500)

    def open(self, url: str, timeout_ms: int = 45000):
        """Ανοίγει τη σελίδα και επιστρέφει (page, PageResult). Ο καλών κλείνει τη σελίδα."""
        self._polite_wait(url)
        page = self._context.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            try:  # να μη μένουν ανοιχτές καρτέλες από σελίδες που δεν άνοιξαν
                page.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:  # noqa: BLE001
            pass
        self._accept_cookies(page)
        page.wait_for_timeout(600)
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
