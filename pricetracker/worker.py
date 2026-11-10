"""Thread που κάνει τους ελέγχους (ένας browser κάθε φορά) και ο χρονοπρογραμματισμός τους."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import analysis, fx, notifier, productdirs
from .browser import Browser
from .extract import extract
from .search import (build_query, find_candidates, looks_like_search_results, search_url,
                     search_url_candidates)
from .shops import DEFAULT_SHOPS, REGION_LABELS, country_label, currency_of_country, host_of

log = logging.getLogger(__name__)


def _common_paths():
    from .search import COMMON_SEARCH_PATHS

    return COMMON_SEARCH_PATHS


def _slug(text: str, n=40) -> str:
    s = re.sub(r"[^\w\-]+", "_", text or "", flags=re.UNICODE).strip("_")
    return s[:n] or "x"


class Worker:
    def __init__(self, db, data_dir: Path, notify_callback=None, app_dir: Path | None = None):
        self.db = db
        self.data_dir = Path(data_dir)  # Έγγραφα\PriceTracker: screenshots
        self.app_dir = Path(app_dir) if app_dir else self.data_dir  # εσωτερικά: προφίλ browser
        self.q: queue.Queue = queue.Queue()
        self.notify_callback = notify_callback  # π.χ. ειδοποίηση στο system tray
        self.status = {"running": False, "task": "", "progress": 0, "total": 0,
                       "last_run": None, "next_run": None, "last_error": None, "log": []}
        self.detect: dict[int, dict] = {}  # shop_id → κατάσταση αυτόματης εύρεσης URL αναζήτησης
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
            self.detect_new_default_shops()
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

    def detect_new_default_shops(self):
        """Προεπιλεγμένα καταστήματα χωρίς URL αναζήτησης: η εφαρμογή το βρίσκει μόνη της (μία φορά το καθένα)."""
        tried = set(self.db.get_settings().get("detect_tried") or [])
        keys = {s["key"] for s in DEFAULT_SHOPS}
        for sh in self.db.list_shops():
            if sh["key"] in keys and not sh.get("search_url") and sh["key"] not in tried:
                self.detect[sh["id"]] = {"state": "queued", "message": "Σε αναμονή…"}
                self.enqueue("detect_search", shop_id=sh["id"])

    # ------------------------------------------------------------ loops
    def _scheduler(self):
        last = time.time()
        last_warranty = 0.0
        while not self._stop.wait(30):
            try:
                last_warranty = self._scheduler_tick(last, last_warranty)
            except Exception:  # noqa: BLE001 - ο προγραμματιστής δεν πρέπει να σταματά ποτέ
                log.exception("scheduler tick failed")

    def _scheduler_tick(self, last: float, last_warranty: float) -> float:
        if time.time() - last_warranty > 6 * 3600:
            last_warranty = time.time()
            try:
                self.check_warranties()
            except Exception:  # noqa: BLE001
                log.exception("warranty check failed")
        settings = self.db.get_settings()
        if settings.get("research_daily", True) and not self.status["running"]:
            last_r = float(settings.get("last_research_ts") or 0)
            if last_r == 0:  # πρώτη φορά: όχι αμέσως, άσε πρώτα τους ελέγχους τιμών
                self.db.set_settings({"last_research_ts": time.time() - 23 * 3600})
            elif time.time() - last_r > 24 * 3600:
                self.db.set_settings({"last_research_ts": time.time()})
                self.enqueue("research_all")
        hours = analysis.current_interval_hours(settings)
        base = self.status.get("last_run_ts") or last
        nxt = base + hours * 3600
        self.status["next_run"] = datetime.fromtimestamp(nxt).strftime("%Y-%m-%d %H:%M")
        if time.time() >= nxt and not self.status["running"]:
            self.enqueue("check_all")
            self.status["last_run_ts"] = time.time()
        return last_warranty

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
                elif kind == "research_all":
                    self.research_all()
                elif kind == "detect_search":
                    self.detect_search(kw["shop_id"])
                self.status["last_error"] = None
            except Exception as e:  # noqa: BLE001
                log.exception("job failed")
                self.status["last_error"] = str(e)
                self._log(f"Σφάλμα: {e}")
            finally:
                self.status.update(running=self.pending() > 0, task="", progress=0, total=0)

    # ------------------------------------------------------------ jobs
    def browser_mode(self) -> str:
        s = self.db.get_settings()
        mode = s.get("browser_mode")
        if mode in ("auto", "hidden", "visible"):
            return mode
        return "visible" if s.get("headless") is False else "auto"

    def _browser(self):
        return Browser(headless=self.browser_mode() != "visible", profile_dir=self.app_dir / "browser-profile")

    def _ask_user_to_unblock(self, br: Browser, url: str, reason: str):
        """Ανοίγει ορατό browser μόνο για αυτή τη σελίδα, ώστε ο χρήστης να λύσει π.χ. ένα captcha.

        Επιστρέφει (page, res) όταν η σελίδα ξεμπλοκάρει, αλλιώς None. Ο browser ξαναγίνεται κρυφός μετά.
        """
        self._log(f"Χρειάζεται ενέργεια: {reason}")
        if self.notify_callback:
            try:
                self.notify_callback("Χρειάζεται η βοήθειά σου: λύσε την επιβεβαίωση (captcha) στο παράθυρο που άνοιξε. "
                                     "Οι έλεγχοι συνεχίζουν μόλις τελειώσεις.")
            except Exception:  # noqa: BLE001
                pass
        br.restart(headless=False)
        page, res = br.open(url)
        self.status["task"] = "Περιμένω να λύσεις το captcha στο παράθυρο του browser…"
        ok = br.wait_until(page, lambda html: not extract(html, url).blocked, timeout_s=180)
        if ok:
            page.wait_for_timeout(1500)
            res.html, res.final_url = page.content(), page.url
            return page, res
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
        return None

    def check_listings(self, listings: list[dict], label: str):
        # Τα αγορασμένα προϊόντα δεν ελέγχονται πια
        products = {p["id"]: p for p in self.db.list_products()}
        listings = [l for l in listings if l["product_id"] in products and not products[l["product_id"]]["purchased"]]
        if not listings:
            return
        self.status.update(task=label, progress=0, total=len(listings))
        self._log(f"{label}: {len(listings)} σελίδες")
        settings = self.db.get_settings()
        try:  # ισοτιμίες για τιμές σε ξένο νόμισμα (ανανεώνονται αν είναι παλιότερες από 1 ώρα)
            self.fx = fx.get_rates(self.db)
        except Exception:  # noqa: BLE001
            self.fx = self.db.get_settings().get("fx") or {}
        alerts = []
        try:
            with self._browser() as br:
                for i, l in enumerate(listings, 1):
                    if self._stop.is_set():
                        break
                    self.status["progress"] = i
                    if not self.db.get_listing(l["id"]):
                        continue  # διαγράφηκε ενώ έτρεχε ο έλεγχος
                    try:
                        a = self._check_one(br, l, products[l["product_id"]], settings)
                        alerts += a
                    except Exception as e:  # noqa: BLE001
                        msg = (str(e).splitlines() or [type(e).__name__])[0][:300]
                        if not self.db.get_listing(l["id"]):
                            self._log(f"{l.get('shop_name')}: διαγράφηκε κατά τον έλεγχο")
                            continue
                        try:
                            self.db.record_check(l["id"], None, None, None, error=msg)
                        except Exception:  # noqa: BLE001
                            log.exception("record error failed")
                        self._log(f"✗ {l.get('shop_name')}: {msg}")
        finally:
            # Οι ειδοποιήσεις που μαζεύτηκαν φεύγουν ό,τι κι αν συμβεί στα υπόλοιπα
            if alerts:
                try:
                    self._send_alerts(alerts, settings)
                except Exception:  # noqa: BLE001
                    log.exception("sending alerts failed")

    def _check_one(self, br: Browser, l: dict, product: dict, settings: dict) -> list[dict]:
        prev_hist = self.db.history(listing_id=l["id"])
        prev_ok = [h for h in prev_hist if not h["error"]]
        prev = prev_ok[-1] if prev_ok else None
        page, res = br.open(l["url"])
        interactive = False
        try:
            info = extract(res.html, res.final_url)
            if info.blocked and br.headless and self.browser_mode() == "auto":
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
                interactive = True
                got = self._ask_user_to_unblock(br, l["url"], info.blocked)
                if got is None:
                    page = None
                    raise RuntimeError(info.blocked + " (δεν λύθηκε μέσα σε 3 λεπτά)")
                page, res = got
                info = extract(res.html, res.final_url)
            if info.blocked:
                raise RuntimeError(info.blocked)
            if res.status and res.status >= 400 and info.price is None:
                raise RuntimeError(f"Η σελίδα απάντησε HTTP {res.status}")
            if info.price is None and info.in_stock is None:
                raise RuntimeError("Δεν βρέθηκε τιμή ή διαθεσιμότητα στη σελίδα")
            in_stock = info.in_stock if info.in_stock is not None else (info.price is not None)
            # Νόμισμα: από τη σελίδα, αλλιώς αυτό που όρισε ο χρήστης, αλλιώς από τη χώρα του καταστήματος
            info.currency = info.currency or l.get("currency_override") or currency_of_country(l.get("country"))
            if info.price is not None and not info.currency:
                self._ask_currency(l, product)
            shot = None
            # Screenshot: την πρώτη φορά (η μέρα που προστέθηκε) και μετά μόνο όταν πέφτει η τιμή.
            # Όλα μένουν στον φάκελο του προϊόντος, Προϊόντα\<ημερομηνία προσθήκης> <όνομα>.
            last = next((h for h in reversed(prev_ok) if h["price"] is not None), None)
            dropped = (last is not None and info.price is not None
                       and (last.get("currency") or "") == (info.currency or last.get("currency") or "")
                       and info.price < last["price"])
            if info.price is not None and (last is None or dropped or settings.get("screenshot_mode") == "always"):
                when = datetime.now().strftime("%d/%m/%Y %H:%M")
                path = (productdirs.product_dir(self.db, self.data_dir, product) /
                        f"{datetime.now():%Y%m%d_%H%M%S}_{_slug(l.get('shop_name') or 'shop', 20)}.jpg")
                price_txt = (fx.fmt_with_eur(info.price, info.currency, fx.to_eur(info.price, info.currency, self._fx()))
                             + ("" if in_stock else " (μη διαθέσιμο)"))
                shop_txt = f"{l.get('shop_name') or ''} · {host_of(res.final_url)}"
                try:
                    br.screenshot(page, path, shop_txt, info.title or product["name"], price_txt, when)
                    shot = path.relative_to(self.data_dir).as_posix()
                except Exception as e:  # noqa: BLE001
                    self._log(f"Screenshot απέτυχε: {e}")
        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:  # noqa: BLE001
                pass
            if interactive:
                br.restart(headless=True)  # πίσω σε κρυφό browser για τα υπόλοιπα
        price_eur = fx.to_eur(info.price, info.currency, self._fx())
        try:
            self.db.record_check(l["id"], info.price, info.currency, in_stock, title=info.title, screenshot=shot,
                                 price_eur=price_eur)
        except Exception:
            if shot:  # η σελίδα/το προϊόν διαγράφηκε στο μεταξύ: να μη μείνει ορφανό screenshot
                productdirs.remove_tree(self.data_dir / shot)
            raise
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
        return self._alerts_for(l, product, prev, prev_ok, info.price, info.currency, in_stock, shot, settings,
                                price_eur=price_eur)

    def _ask_currency(self, l, product):
        """Η τιμή βρέθηκε αλλά όχι το νόμισμα: ζητάμε από τον χρήστη να το διαλέξει (μία ειδοποίηση)."""
        self._log(f"{l.get('shop_name')}: δεν βρέθηκε το νόμισμα της τιμής, διάλεξέ το στο προϊόν")
        if l.get("currency_asked"):
            return
        self.db.mark_currency_asked(l["id"])
        if self.notify_callback:
            try:
                self.notify_callback(f"Σε ποιο νόμισμα είναι η τιμή του «{product['name']}» στο {l.get('shop_name')}; "
                                     "Άνοιξε το προϊόν και διάλεξέ το.")
            except Exception:  # noqa: BLE001
                pass

    def _fx(self) -> dict:
        if not getattr(self, "fx", None):
            self.fx = self.db.get_settings().get("fx") or {}
        return self.fx

    def _alerts_for(self, l, product, prev, prev_ok, price, currency, in_stock, shot, settings, price_eur=None):
        if prev is None:
            return []
        if price_eur is None and (currency or "").upper() == "EUR":
            price_eur = price
        base = {"product": product["name"], "shop": l.get("shop_name") or "", "region": l.get("region"),
                "region_label": (country_label(l.get("country")) or REGION_LABELS.get(l.get("region"), "")),
                "price": price, "currency": currency, "price_eur": price_eur,
                "url": l["url"], "target": product.get("target_price"),
                "screenshot": str(self.data_dir / shot) if shot else None}
        out = []
        if settings.get("notify_back_in_stock", True) and in_stock and not prev["in_stock"]:
            out.append({**base, "kind": "in_stock"})
        tgt = product.get("target_price")
        prev_eur = prev.get("price_eur") if "price_eur" in prev.keys() else None
        if prev_eur is None and (prev.get("currency") or "").upper() == "EUR":
            prev_eur = prev["price"]
        # Η τιμή-στόχος είναι σε €: συγκρίνεται με την τιμή σε € (μετά τη μετατροπή αν χρειάζεται)
        if settings.get("notify_target_price", True) and tgt and price_eur and in_stock and price_eur <= tgt and \
                not (prev_eur and prev_eur <= tgt and prev["in_stock"]):
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

    def detect_search(self, shop_id: int, word: str = "samsung"):
        """Βρίσκει μόνο του το URL αναζήτησης ενός καταστήματος από την αρχική του σελίδα."""
        shop = self.db.get_shop(shop_id)
        if not shop:
            return
        st = self.detect[shop_id] = {"state": "running", "message": "Αναζήτηση…"}
        self.status.update(task=f"Εύρεση αναζήτησης στο {shop['name']}", progress=0, total=0)
        found = guess = None
        try:
            with self._browser() as br:
                home = None
                for url in (f"https://www.{shop['domain']}/", f"https://{shop['domain']}/", f"http://{shop['domain']}/"):
                    try:
                        page, res = br.open(url)
                        page.close()
                        if res.html and (not res.status or res.status < 400):
                            home = res
                            break
                    except Exception as e:  # noqa: BLE001
                        self._log(f"{url}: {e}")
                if home is None:
                    raise RuntimeError("Δεν άνοιξε η αρχική σελίδα του καταστήματος")
                cands = search_url_candidates(home.html, home.final_url)
                structured = [c for c in cands if not any(c.endswith(p) for p in _common_paths())]
                guess = structured[0] if structured else None
                self.status["total"] = min(len(cands), 8)
                for i, tmpl in enumerate(cands[:8], 1):
                    if self._stop.is_set():
                        break
                    self.status["progress"] = i
                    try:
                        page, res = br.open(search_url(tmpl, word), timeout_ms=30000)
                        page.close()
                    except Exception:  # noqa: BLE001
                        continue
                    if (not res.status or res.status < 400) and looks_like_search_results(
                            res.html, res.final_url, word, home.final_url):
                        found = tmpl
                        break
        except Exception as e:  # noqa: BLE001
            st.update(state="notfound", message=f"Δεν βρέθηκε: {e}")
            self._log(f"{shop['name']}: εύρεση αναζήτησης απέτυχε: {e}")
            return
        sh = self.db.get_shop(shop_id)
        if sh:
            tried = set(self.db.get_settings().get("detect_tried") or [])
            self.db.set_settings({"detect_tried": sorted(tried | {sh["key"]})})
        url = found or guess
        if url and not (self.db.get_shop(shop_id) or {}).get("search_url"):
            self.db.update_shop(shop_id, search_url=url, search_enabled=1)
        if found:
            st.update(state="found", url=url, message="Βρέθηκε και δοκιμάστηκε")
        elif guess:
            st.update(state="guessed", url=url, message="Βρέθηκε, αλλά δεν επιβεβαιώθηκε. Έλεγξέ το.")
        else:
            st.update(state="notfound", message="Δεν βρέθηκε. Πρόσθεσέ το με το χέρι.")
        self._log(f"{shop['name']}: URL αναζήτησης: {url or 'δεν βρέθηκε'}")

    def research_all(self):
        """Μία φορά τη μέρα: ψάχνει κάθε προϊόν που παρακολουθείς στα καταστήματα που δεν το έχουν ακόμα
        και ειδοποιεί αν βρεθεί σε νέο κατάστημα (για επιβεβαίωση)."""
        products = self.db.list_products(purchased=False)
        news = []
        for i, p in enumerate(products, 1):
            if self._stop.is_set():
                break
            shops = self.search_product(p["id"], label=f"Ημερήσια αναζήτηση {i}/{len(products)}")
            if shops:
                news.append((p, shops))
        if not news:
            return
        settings = self.db.get_settings()
        if not settings.get("notify_new_shop", True):
            return
        lines = [f"• {p['name']}: {', '.join(shops)}" for p, shops in news]
        subject = (f"[Price Tracker] Βρέθηκε σε νέο κατάστημα: {news[0][0]['name']}" if len(news) == 1
                   else f"[Price Tracker] {len(news)} προϊόντα βρέθηκαν σε νέα καταστήματα")
        text = ("Η ημερήσια αναζήτηση βρήκε τα παρακάτω σε καταστήματα που δεν τα είχαν πριν.\n"
                "Άνοιξε την εφαρμογή και επιβεβαίωσε με ✔ αν είναι το ίδιο προϊόν:\n\n" + "\n".join(lines))
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
                self._log(f"Αποτυχία email: {e}")
        self._log(subject.replace("[Price Tracker] ", ""))

    def search_shops(self, product_id: int, have_shops=frozenset()) -> list[dict]:
        """Τα καταστήματα όπου ψάχνεται το προϊόν. Αν έχει γενική κατηγορία (π.χ. Ηλεκτρικές συσκευές), παραλείπονται
        τα καταστήματα άλλων κατηγοριών (π.χ. Ρούχα). Καταστήματα χωρίς γενική κατηγορία ψάχνονται για όλα."""
        sector = self.db.product_sector(product_id)
        shop_secs = self.db.shop_sector_ids()
        return [s for s in self.db.list_shops()
                if s["search_enabled"] and s.get("search_url") and s["id"] not in have_shops
                and (not sector or not shop_secs.get(s["id"]) or sector in shop_secs[s["id"]])]

    def search_product(self, product_id: int, label: str | None = None) -> list[str]:
        """Αναζήτηση στα καταστήματα που δεν έχουν ακόμα το προϊόν. Επιστρέφει τα καταστήματα με νέες προτάσεις."""
        product = self.db.get_product(product_id)
        if not product:
            return []
        existing = self.db.list_listings(product_id=product_id, status=None)
        have_shops = {l["shop_id"] for l in existing}
        title = next((l["title"] for l in existing if l.get("title")), product["name"])
        shops = self.search_shops(product_id, have_shops)
        if not shops:
            return []
        query = build_query(title, product.get("ean"), product.get("mpn"), product.get("brand"))
        self.status.update(task=(f"{label}: " if label else "") + f"Αναζήτηση «{query}»", progress=0,
                           total=len(shops))
        new_shops: list[str] = []
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
                    if cands:
                        new_shops.append(s["name"])
                except Exception as e:  # noqa: BLE001
                    self._log(f"Αναζήτηση στο {s['name']} απέτυχε: {str(e).splitlines()[0][:200]}")
        self._log(f"Βρέθηκαν {found} πιθανές αντιστοιχίες – έλεγξέ τις στο προϊόν")
        return new_shops
