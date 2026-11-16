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
            # Χωρίς αυτόματη αναπαραγωγή βίντεο/ήχου: λιγότερα δεδομένα στους ελέγχους
            args=["--disable-blink-features=AutomationControlled", "--autoplay-policy=user-gesture-required"],
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
        # Χωρίς page.route: στο Playwright απενεργοποιεί την cache του browser, άρα θα έφερνε ΠΕΡΙΣΣΟΤΕΡΑ δεδομένα.

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

    def _finish(self, page, url: str, status, deadline: float, idle_ms: int, pause_ms: int) -> PageResult:
        """Κοινό τέλος για open/open_many: αναμονή να ηρεμήσει το δίκτυο, cookies, ανάγνωση της σελίδας."""
        try:
            page.wait_for_load_state("networkidle", timeout=max(500, min(idle_ms, int((deadline - time.time()) * 1000))))
        except Exception:  # noqa: BLE001
            pass
        self._accept_cookies(page)
        page.wait_for_timeout(pause_ms)
        return PageResult(url=url, final_url=page.url, html=page.content(), status=status)

    @staticmethod
    def _close(page):
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass

    def open(self, url: str, timeout_ms: int = 45000):
        """Ανοίγει τη σελίδα και επιστρέφει (page, PageResult). Ο καλών κλείνει τη σελίδα."""
        self._polite_wait(url)
        page = self._context.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return page, self._finish(page, url, resp.status if resp else None, time.time() + 4, 4000, 600)
        except Exception:
            self._close(page)  # να μη μένουν ανοιχτές καρτέλες από σελίδες που δεν άνοιξαν
            raise

    def open_many(self, urls: list[str], timeout_ms: int = 45000, idle_ms: int = 2500, stop=None) -> list:
        """Ανοίγει πολλές σελίδες ΤΑΥΤΟΧΡΟΝΑ (μία καρτέλα η καθεμία) και επιστρέφει για την καθεμία
        (page, PageResult) ή την εξαίρεση. Ο χρόνος χάνεται στην αναμονή των sites, οπότε 4 καρτέλες μαζί
        τελειώνουν σχεδόν όσο γρήγορα και μία. Ο καλών κλείνει τις σελίδες. Σε ορατό browser μία-μία: οι
        καρτέλες στο παρασκήνιο «φρενάρονται» και δεν προλαβαίνουν να δείξουν τα αποτελέσματα."""
        if not self.headless:
            out = []
            for u in urls:
                if stop is not None and stop.is_set():
                    break
                try:
                    out.append(self.open(u, timeout_ms))
                except Exception as e:  # noqa: BLE001
                    out.append(e)
            return out
        started = []
        for url in urls:
            self._polite_wait(url)
            try:
                page = self._context.new_page()
            except Exception as e:  # noqa: BLE001
                started.append((None, url, e, None))
                continue
            status: dict = {}

            def on_response(r, page=page, status=status):
                try:  # η τελευταία απάντηση της κύριας σελίδας (μετά από ανακατευθύνσεις), όπως στο goto
                    if not status.get("frozen") and r.request.is_navigation_request() and r.frame == page.main_frame:
                        status["code"] = r.status
                except Exception:  # noqa: BLE001
                    pass

            page.on("response", on_response)
            try:
                # Χωρίς αναμονή: η πλοήγηση ξεκινά και ο browser φορτώνει όλες τις καρτέλες μαζί
                page.evaluate("u => { window.location.href = u; }", url)
            except Exception as e:  # noqa: BLE001
                if "context was destroyed" not in str(e):
                    started.append((page, url, e, None))
                    continue
                # η πλοήγηση ξεκίνησε τόσο γρήγορα που «χάλασε» το ίδιο το evaluate: όλα καλά
            started.append((page, url, None, status))
        out = []
        deadline = time.time() + timeout_ms / 1000

        def left():
            return max(1000, int((deadline - time.time()) * 1000))

        for page, url, error, status in started:
            if stop is not None and stop.is_set():  # Διακοπή: κλείνουν οι καρτέλες που περιμένουν ακόμα
                if page is not None:
                    self._close(page)
                continue
            if error is not None:
                if page is not None:
                    self._close(page)
                out.append(error)
                continue
            try:
                page.wait_for_url(lambda u: not u.startswith("about:"), timeout=left(), wait_until="commit")
                page.wait_for_load_state("domcontentloaded", timeout=left())
                status["frozen"] = True  # ό,τι φορτώσει μετά (π.χ. μετά τα cookies) δεν αλλάζει την κατάσταση
                if page.url.startswith("chrome-error://"):  # δεν άνοιξε (π.χ. δεν απαντά ο server), όπως το goto
                    raise RuntimeError(f"Η σελίδα δεν άνοιξε: {url}")
                out.append((page, self._finish(page, url, status.get("code"), deadline, idle_ms, 300)))
            except Exception as e:  # noqa: BLE001
                self._close(page)
                out.append(e)
        return out

    @staticmethod
    def screenshot(page, path: Path, shop: str, title: str, price: str, when: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            page.evaluate("window.scrollTo(0, 0)")
            page.evaluate(OVERLAY_JS, [shop, title or "", price, when])
        except Exception:  # noqa: BLE001
            log.debug("overlay failed", exc_info=True)
        page.screenshot(path=str(path), type="jpeg", quality=75, full_page=False)
