"""Thread που κάνει τους ελέγχους (ένας browser κάθε φορά) και ο χρονοπρογραμματισμός τους."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import analysis, notifier
from .browser import Browser
from .extract import extract
from .search import build_query, find_candidates, search_url
from .shops import REGION_LABELS, host_of

log = logging.getLogger(__name__)


def _slug(text: str, n=40) -> str:
    s = re.sub(r"[^\w\-]+", "_", text or "", flags=re.UNICODE).strip("_")
    return s[:n] or "x"


class Worker:
    def __init__(self, db, data_dir: Path, notify_callback=None):
        self.db = db
        self.data_dir = Path(data_dir)
        self.shots_dir = self.data_dir / "screenshots"
        self.q: queue.Queue = queue.Queue()
        self.notify_callback = notify_callback  # π.χ. ειδοποίηση στο system tray
        self.status = {"running": False, "task": "", "progress": 0, "total": 0,
                       "last_run": None, "next_run": None, "last_error": None, "log": []}
        self._stop = threading.Event()
        self._queued_keys: set = set()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="worker", daemon=True)
        self._sched = threading.Thread(target=self._scheduler, name="scheduler", daemon=True)

    # ------------------------------------------------------------ public
    def start(self, check_on_start=True):
        self._thread.start()
        if check_on_start:
            self.enqueue("check_all")
        self._sched.start()

    def stop(self):
        self._stop.set()
        self.q.put(None)

    def enqueue(self, kind: str, **kw):
        key = (kind, tuple(sorted(kw.items())))
        with self._lock:
            if key in self._queued_keys:
                return False
            self._queued_keys.add(key)
        self.q.put((kind, kw, key))
        return True

    def pending(self) -> int:
        return self.q.qsize()

    def _log(self, msg: str):
        log.info(msg)
        entry = f"{datetime.now():%H:%M:%S} {msg}"
        self.status["log"] = (self.status["log"] + [entry])[-60:]

    # ------------------------------------------------------------ loops
    def _scheduler(self):
        last = time.time()
        last_warranty = 0.0
        while not self._stop.wait(30):
            if time.time() - last_warranty > 6 * 3600:
                last_warranty = time.time()
                try:
                    self.check_warranties()
                except Exception:  # noqa: BLE001
                    log.exception("warranty check failed")
            settings = self.db.get_settings()
            hours = analysis.current_interval_hours(settings)
            base = self.status.get("last_run_ts") or last
            nxt = base + hours * 3600
            self.status["next_run"] = datetime.fromtimestamp(nxt).strftime("%Y-%m-%d %H:%M")
            if time.time() >= nxt and not self.status["running"]:
                self.enqueue("check_all")
                self.status["last_run_ts"] = time.time()

    def _loop(self):
        while not self._stop.is_set():
            item = self.q.get()
            if item is None:
                break
            kind, kw, key = item
            with self._lock:
                self._queued_keys.discard(key)
            self.status["running"] = True
            try:
                if kind == "check_all":
                    self.status["last_run_ts"] = time.time()
                    self.check_listings(self.db.list_listings(status="active"), label="Έλεγχος όλων")
                    self.status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                elif kind == "check_product":
                    self.check_listings(self.db.list_listings(product_id=kw["product_id"], status="active"),
                                        label="Έλεγχος προϊόντος")
                elif kind == "check_listing":
                    l = self.db.get_listing(kw["listing_id"])
                    if l:
                        self.check_listings([l], label="Έλεγχος")
                elif kind == "new_product":
                    self.new_product(kw["product_id"], kw.get("search", True))
                elif kind == "search_product":
                    self.search_product(kw["product_id"])
                self.status["last_error"] = None
            except Exception as e:  # noqa: BLE001
                log.exception("job failed")
                self.status["last_error"] = str(e)
                self._log(f"Σφάλμα: {e}")
            finally:
                self.status.update(running=self.pending() > 0, task="", progress=0, total=0)

    # ------------------------------------------------------------ jobs
    def _browser(self):
        return Browser(headless=bool(self.db.get_settings().get("headless", True)))

    def check_listings(self, listings: list[dict], label: str):
        # Τα αγορασμένα προϊόντα δεν ελέγχονται πια
        products = {p["id"]: p for p in self.db.list_products()}
        listings = [l for l in listings if l["product_id"] in products and not products[l["product_id"]]["purchased"]]
        if not listings:
            return
        self.status.update(task=label, progress=0, total=len(listings))
        self._log(f"{label}: {len(listings)} σελίδες")
        settings = self.db.get_settings()
        alerts = []
        with self._browser() as br:
            for i, l in enumerate(listings, 1):
                if self._stop.is_set():
                    break
                self.status["progress"] = i
                try:
                    a = self._check_one(br, l, products[l["product_id"]], settings)
                    alerts += a
                except Exception as e:  # noqa: BLE001
                    msg = str(e).splitlines()[0][:300]
                    self.db.record_check(l["id"], None, None, None, error=msg)
                    self._log(f"✗ {l.get('shop_name')}: {msg}")
        if alerts:
            self._send_alerts(alerts, settings)

    def _check_one(self, br: Browser, l: dict, product: dict, settings: dict) -> list[dict]:
        prev_hist = self.db.history(listing_id=l["id"])
        prev_ok = [h for h in prev_hist if not h["error"]]
        prev = prev_ok[-1] if prev_ok else None
        page, res = br.open(l["url"])
        try:
            info = extract(res.html, res.final_url)
            if info.blocked:
                raise RuntimeError(info.blocked)
            if res.status and res.status >= 400 and info.price is None:
                raise RuntimeError(f"Η σελίδα απάντησε HTTP {res.status}")
            if info.price is None and info.in_stock is None:
                raise RuntimeError("Δεν βρέθηκε τιμή ή διαθεσιμότητα στη σελίδα")
            in_stock = info.in_stock if info.in_stock is not None else (info.price is not None)
            changed = (prev is None or prev["price"] != info.price or bool(prev["in_stock"]) != bool(in_stock))
            shot = None
            if info.price is not None and (changed or settings.get("screenshot_mode") == "always"
                                           or analysis.in_bf_window(settings)):
                when = datetime.now().strftime("%d/%m/%Y %H:%M")
                path = (self.shots_dir / f"{product['id']}_{_slug(product['name'])}" /
                        f"{datetime.now():%Y%m%d_%H%M%S}_{_slug(l.get('shop_name') or 'shop', 20)}.jpg")
                cur = info.currency or "EUR"
                price_txt = (f"{info.price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                             + (" €" if cur == "EUR" else f" {cur}") + ("" if in_stock else " (μη διαθέσιμο)"))
                shop_txt = f"{l.get('shop_name') or ''} · {host_of(res.final_url)}"
                try:
                    br.screenshot(page, path, shop_txt, info.title or product["name"], price_txt, when)
                    shot = str(path.relative_to(self.data_dir))
                except Exception as e:  # noqa: BLE001
                    self._log(f"Screenshot απέτυχε: {e}")
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        self.db.record_check(l["id"], info.price, info.currency, in_stock, title=info.title, screenshot=shot)
        # Συμπλήρωση στοιχείων προϊόντος αν λείπουν
        upd = {}
        for k_info, k_prod in (("ean", "ean"), ("mpn", "mpn"), ("brand", "brand"), ("image", "image_url")):
            if getattr(info, k_info) and not product.get(k_prod):
                upd[k_prod] = getattr(info, k_info)
                product[k_prod] = upd[k_prod]
        if upd:
            self.db.update_product(product["id"], **upd)
        price_s = f"{info.price:.2f}" if info.price else "-"
        self._log(f"✓ {l.get('shop_name')}: {price_s} {'διαθέσιμο' if in_stock else 'μη διαθέσιμο'}")
        return self._alerts_for(l, product, prev, prev_ok, info.price, info.currency, in_stock, shot, settings)

    def _alerts_for(self, l, product, prev, prev_ok, price, currency, in_stock, shot, settings):
        if prev is None:
            return []
        base = {"product": product["name"], "shop": l.get("shop_name") or "", "region": l.get("region"),
                "region_label": REGION_LABELS.get(l.get("region"), ""), "price": price, "currency": currency,
                "url": l["url"], "target": product.get("target_price"),
                "screenshot": str(self.data_dir / shot) if shot else None}
        out = []
        if settings.get("notify_back_in_stock", True) and in_stock and not prev["in_stock"]:
            out.append({**base, "kind": "in_stock"})
        tgt = product.get("target_price")
        if settings.get("notify_target_price", True) and tgt and price and in_stock and price <= tgt and \
                not (prev["price"] and prev["price"] <= tgt and prev["in_stock"]):
            out.append({**base, "kind": "target"})
        if settings.get("notify_new_low", True) and price and in_stock and not out:
            prices = [h["price"] for h in prev_ok if h["price"]]
            if len(prices) >= 3 and price < min(prices) * 0.99:
                out.append({**base, "kind": "new_low"})
        return out

    def _send_alerts(self, alerts, settings):
        subject, text, body = notifier.format_alerts(alerts)
        if self.notify_callback:
            try:
                self.notify_callback(subject.replace("[Price Tracker] ", ""))
            except Exception:  # noqa: BLE001
                pass
        if settings.get("email_enabled"):
            try:
                pw = notifier.load_password(self.db, settings.get("smtp_user", ""))
                notifier.send_email(settings, pw, subject, text, body,
                                    [Path(a["screenshot"]) for a in alerts if a.get("screenshot")])
                self._log(f"Στάλθηκε email: {subject}")
            except Exception as e:  # noqa: BLE001
                self._log(f"Αποτυχία email: {e}")

    def check_warranties(self, today=None):
        """Ειδοποίηση μία φορά όταν πλησιάζει η λήξη μιας εγγύησης."""
        from . import warranty

        settings = self.db.get_settings()
        days = int(settings.get("warranty_reminder_days") or 0)
        if days <= 0:
            return []
        sent = set(settings.get("warranty_notified") or [])
        due = []
        for p in self.db.list_purchased():
            w = warranty.warranty_info(p, today)
            key = f"{p['id']}:{w['warranty_end']}"
            if w["warranty_end"] and 0 <= w["warranty_days_left"] <= days and key not in sent:
                due.append((p, w, key))
        if not due:
            return []
        lines = [f"• {p['name']}: η εγγύηση λήγει στις {w['warranty_end']} (σε {w['warranty_days_left']} μέρες)"
                 for p, w, _ in due]
        subject = (f"[Price Tracker] Λήγει η εγγύηση: {due[0][0]['name']}" if len(due) == 1
                   else f"[Price Tracker] Λήγουν {len(due)} εγγυήσεις")
        text = "Πλησιάζει η λήξη εγγύησης για:\n\n" + "\n".join(lines)
        if self.notify_callback:
            try:
                self.notify_callback(subject.replace("[Price Tracker] ", ""))
            except Exception:  # noqa: BLE001
                pass
        if settings.get("email_enabled"):
            try:
                pw = notifier.load_password(self.db, settings.get("smtp_user", ""))
                notifier.send_email(settings, pw, subject, text)
            except Exception as e:  # noqa: BLE001
                self._log(f"Αποτυχία email εγγύησης: {e}")
        self.db.set_settings({"warranty_notified": sorted(sent | {k for _, _, k in due})})
        self._log(subject.replace("[Price Tracker] ", ""))
        return due

    def new_product(self, product_id: int, search: bool = True):
        listings = self.db.list_listings(product_id=product_id, status="active")
        self.check_listings(listings, label="Πρώτος έλεγχος")
        product = self.db.get_product(product_id)
        if not product:
            return
        if product["name"].startswith("Νέο προϊόν") or product["name"].startswith("http"):
            titled = [l for l in self.db.list_listings(product_id=product_id) if l.get("title")]
            if titled:
                self.db.update_product(product_id, name=titled[0]["title"][:150])
        if search:
            self.search_product(product_id)

    def search_product(self, product_id: int):
        product = self.db.get_product(product_id)
        if not product:
            return
        existing = self.db.list_listings(product_id=product_id, status=None)
        have_shops = {l["shop_id"] for l in existing}
        title = next((l["title"] for l in existing if l.get("title")), product["name"])
        shops = [s for s in self.db.list_shops()
                 if s["search_enabled"] and s.get("search_url") and s["id"] not in have_shops]
        if not shops:
            return
        query = build_query(title, product.get("ean"), product.get("mpn"), product.get("brand"))
        self.status.update(task=f"Αναζήτηση «{query}»", progress=0, total=len(shops))
        self._log(f"Αναζήτηση «{query}» σε {len(shops)} καταστήματα")
        found = 0
        with self._browser() as br:
            for i, s in enumerate(shops, 1):
                if self._stop.is_set():
                    break
                self.status["progress"] = i
                try:
                    page, res = br.open(search_url(s["search_url"], query))
                    page.close()
                    cands = find_candidates(res.html, res.final_url, title, product.get("ean"),
                                            product.get("mpn"), product.get("brand"))
                    if not cands and product.get("ean"):
                        page, res = br.open(search_url(s["search_url"], product["ean"]))
                        page.close()
                        cands = find_candidates(res.html, res.final_url, title, product.get("ean"),
                                                product.get("mpn"), product.get("brand"))
                    for c in cands:
                        self.db.add_listing(product_id, c["url"], status="candidate", title=c["title"])
                        found += 1
                except Exception as e:  # noqa: BLE001
                    self._log(f"Αναζήτηση στο {s['name']} απέτυχε: {str(e).splitlines()[0][:200]}")
        self._log(f"Βρέθηκαν {found} πιθανές αντιστοιχίες – έλεγξέ τις στο προϊόν")
