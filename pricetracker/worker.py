"""Thread που κάνει τους ελέγχους (ένας browser κάθε φορά) και ο χρονοπρογραμματισμός τους."""
from __future__ import annotations

import logging
import queue
import random
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import analysis, fx, notifier, productdirs
from .browser import Browser
from .extract import extract
from .search import (build_query, find_candidates, looks_like_search_results, probe_words, search_url,
                     search_url_candidates)
from .shops import DEFAULT_SHOPS, REGION_LABELS, country_label, currency_of_country, host_of

log = logging.getLogger(__name__)

RETRY_AFTER_S = 600
BLOCK_STATUS = {403, 429, 503}
NET_ERROR = re.compile(r"net::ERR_|ERR_NAME_NOT_RESOLVED|ERR_INTERNET_DISCONNECTED|getaddrinfo|Timeout \d+ms exceeded"
                       r"|timed out|Network is unreachable|ConnectionRefused", re.I)
ALERT_COOLDOWN_H = 24
BLOCK_PAUSE_S = 3600          # κατάστημα που απάντησε 403/429/503: ξεκούραση 1 ώρα
CAPTCHA_PAUSE_S = 3 * 3600    # captcha που δεν λύθηκε: 3 ώρες χωρίς ερωτήσεις (ούτε παράθυρα τη νύχτα)
OUTBOX_MAX_AGE_H = 48


def _common_paths():
    from .search import COMMON_SEARCH_PATHS

    return COMMON_SEARCH_PATHS


def _slug(text: str, n=40) -> str:
    s = re.sub(r"[^\w\-]+", "_", text or "", flags=re.UNICODE).strip("_")
    return s[:n] or "x"


def _interleave_by_host(listings: list[dict]) -> list[dict]:
    """Εναλλάξ ανά κατάστημα (Amazon, Plaisio, Public, Amazon…) αντί για όλα της Amazon στη σειρά."""
    groups: dict[str, list[dict]] = {}
    for l in listings:
        groups.setdefault(host_of(l.get("url") or ""), []).append(l)
    out = []
    queues = list(groups.values())
    while queues:
        for q in list(queues):
            out.append(q.pop(0))
            if not q:
                queues.remove(q)
    return out


DETECT_RETRY_S = 30 * 86400


def _detect_tried(settings) -> dict[str, float]:
    """Πότε δοκιμάστηκε τελευταία η εύρεση αναζήτησης ανά κατάστημα (παλιά μορφή: απλή λίστα)."""
    v = settings.get("detect_tried") or {}
    if isinstance(v, list):
        return {k: 0.0 for k in v}  # από v0.8.4: ξαναδοκιμάζονται μία φορά με τη νέα, καλύτερη εύρεση
    return {k: float(t or 0) for k, t in v.items()}


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
        # «Υγεία»: ό,τι σφάλμα θα περνούσε απαρατήρητο εμφανίζεται στην εφαρμογή
        self.health = {"scheduler_ts": None, "email_error": None, "email_error_at": None,
                       "backup_ts": None, "backup_error": None, "db_problems": []}
        self._backup_retry_at = 0.0
        self.backup = None  # συνάρτηση: ημερήσιο αντίγραφο της βάσης στα Έγγραφα (από το app.py)
        self.retry_at = None  # επανάληψη των σελίδων που απέτυχαν (π.χ. χωρίς δίκτυο στην εκκίνηση)
        self.current = None  # η εργασία που τρέχει τώρα
        self._blocked_hosts: dict[str, float] = {}  # κατάστημα → έως πότε δεν ξαναδοκιμάζεται
        self._jitter = 0.0
        self._stop = threading.Event()
        self._queued_keys: set = set()
        self._lock = threading.Lock()
        self._outbox_lock = threading.RLock()
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
        tried = _detect_tried(self.db.get_settings())
        keys = {s["key"] for s in DEFAULT_SHOPS}
        for sh in self.db.list_shops():
            # Μια αποτυχία δεν μένει για πάντα: νέα προσπάθεια μετά από 30 μέρες (το κατάστημα μπορεί να άλλαξε)
            if sh["key"] in keys and not sh.get("search_url") and time.time() - tried.get(sh["key"], 0) > DETECT_RETRY_S:
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
        self.health["scheduler_ts"] = time.time()
        if self.backup is not None and time.time() >= self._backup_retry_at:
            try:
                if self.backup():
                    self.health["backup_ts"] = time.time()
                self.health["backup_error"] = None
            except Exception as e:  # noqa: BLE001
                # π.χ. το OneDrive κρατά το αρχείο: νέα προσπάθεια σε 1 ώρα (όχι κάθε 30" που γεμίζει το log)
                self._backup_retry_at = time.time() + 3600
                log.warning("backup failed: %s", e)
                self.health["backup_error"] = str(e)[:300]
        try:
            self.retry_outbox()
        except Exception:  # noqa: BLE001
            log.exception("email retry failed")
        if self.retry_at and time.time() >= self.retry_at:
            self.retry_at = None
            self.enqueue("retry_failed")
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
        nxt = base + hours * 3600 + self._jitter
        self.status["next_run"] = datetime.fromtimestamp(nxt).strftime("%Y-%m-%d %H:%M")
        # Ο έλεγχος μπαίνει στην ουρά στην ώρα του, ακόμα κι αν τρέχει άλλη εργασία (π.χ. η ημερήσια αναζήτηση).
        # Η ουρά δεν βάζει τον ίδιο έλεγχο δύο φορές· μόνο αν τρέχει ήδη έλεγχος όλων, περιμένει να τελειώσει.
        if time.time() >= nxt and self.current != "check_all":
            self.enqueue("check_all")
            self.status["last_run_ts"] = time.time()
            # Μικρή τυχαία μετατόπιση (±10%, έως ±20 λεπτά) ώστε οι έλεγχοι να μη γίνονται πάντα στην ίδια ώρα
            span = min(hours * 360, 1200)
            self._jitter = random.uniform(-span, span)
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
            self.current = kind
            try:
                if kind == "check_all":
                    self.status["last_run_ts"] = time.time()
                    self.check_listings(self.db.list_listings(status="active"), label="Έλεγχος όλων")
                    self.status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                elif kind == "retry_failed":
                    failed = [l for l in self.db.list_listings(status="active") if l.get("last_error")]
                    if failed:
                        self.check_listings(failed, label="Νέα προσπάθεια", retry=False)
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
                self.current = None
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
        def solved(html):  # λύθηκε μόνο όταν φαίνεται πραγματικά το προϊόν, όχι απλώς «χωρίς captcha»
            i = extract(html, url)
            return not i.blocked and (i.price is not None or i.in_stock is not None)

        ok = br.wait_until(page, solved, timeout_s=180)
        if ok:
            page.wait_for_timeout(1500)
            res.html, res.final_url = page.content(), page.url
            return page, res
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
        return None

    def check_listings(self, listings: list[dict], label: str, retry: bool = True):
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
        failed_net = 0
        now = time.time()
        self._blocked_hosts = {h: t for h, t in self._blocked_hosts.items() if t > now}
        listings = _interleave_by_host(listings)
        try:
            with self._browser() as br:
                for i, l in enumerate(listings, 1):
                    if self._stop.is_set():
                        break
                    self.status["progress"] = i
                    cur = self.db.get_listing(l["id"])
                    if not cur or cur.get("status") not in (None, "active"):
                        continue  # διαγράφηκε ή αναιρέθηκε η επιβεβαίωσή του ενώ έτρεχε ο έλεγχος
                    host = host_of(l["url"])
                    if self._blocked_hosts.get(host, 0) > time.time():
                        msg = "Το κατάστημα μπλόκαρε πριν λίγο: παραλείφθηκε σε αυτόν τον έλεγχο"
                        self._log(f"{l.get('shop_name')}: {msg}")
                        try:
                            self.db.record_check(l["id"], None, None, None, error=msg)
                        except Exception:  # noqa: BLE001
                            log.exception("record skip failed")
                        continue  # δεν «σφυροκοπάμε» ένα κατάστημα που μόλις μας μπλόκαρε
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
                        if NET_ERROR.search(msg):
                            failed_net += 1
                        self._log(f"✗ {l.get('shop_name')}: {msg}")
        except Exception as e:  # noqa: BLE001 - π.χ. δεν ξεκίνησε ο browser
            if retry and NET_ERROR.search(str(e)):
                failed_net += 1
            raise
        finally:
            if retry and failed_net and self.retry_at is None:
                # π.χ. αυτόματη εκκίνηση με τα Windows πριν συνδεθεί το Wi-Fi: νέα προσπάθεια σε 10 λεπτά
                self.retry_at = time.time() + RETRY_AFTER_S
                self._log(f"{failed_net} σελίδες δεν άνοιξαν (δίκτυο;), νέα προσπάθεια σε 10 λεπτά")
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
        host = host_of(l["url"])
        try:
            info = extract(res.html, res.final_url)
            if not info.blocked and info.price is None and res.status in BLOCK_STATUS:
                # 403/429/503 χωρίς captcha (π.χ. «Access Denied», συντήρηση): δεν ανοίγει παράθυρο, γιατί
                # δεν υπάρχει κάτι να λύσει ο χρήστης. Το κατάστημα ξεκουράζεται 1 ώρα.
                self._blocked_hosts[host] = time.time() + BLOCK_PAUSE_S
                raise RuntimeError(f"Το κατάστημα μπλόκαρε προσωρινά την πρόσβαση (HTTP {res.status}). "
                                   "Νέα προσπάθεια σε 1 ώρα.")
            if info.blocked and br.headless and self.browser_mode() == "auto":
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
                interactive = True
                got = self._ask_user_to_unblock(br, l["url"], info.blocked)
                if got is None:
                    page = None
                    self._blocked_hosts[host] = time.time() + CAPTCHA_PAUSE_S
                    raise RuntimeError(info.blocked + " (δεν λύθηκε μέσα σε 3 λεπτά)")
                page, res = got
                info = extract(res.html, res.final_url)
            if info.blocked:
                self._blocked_hosts[host] = time.time() + CAPTCHA_PAUSE_S
                raise RuntimeError(info.blocked)
            if res.status and res.status >= 400 and info.price is None:
                raise RuntimeError(f"Η σελίδα απάντησε HTTP {res.status}")
            if info.price is None and info.in_stock is None:
                raise RuntimeError("Δεν βρέθηκε τιμή ή διαθεσιμότητα στη σελίδα")
            stock_known = info.in_stock is not None
            in_stock = info.in_stock if stock_known else (info.price is not None)
            # Νόμισμα: αυτό που διάλεξε ο χρήστης (υπερισχύει), αλλιώς από τη σελίδα, αλλιώς από τη χώρα του καταστήματος
            info.currency = l.get("currency_override") or info.currency or currency_of_country(l.get("country"))
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
        src = getattr(info, "price_source", None)
        last_src = next((h.get("source") for h in reversed(prev_ok) if h["price"] is not None and h.get("source")), None)
        if src and last_src and src != last_src:
            # π.χ. από JSON-LD σε «γενική ανάγνωση»: πιθανή αλλαγή στη σελίδα του καταστήματος, αξίζει έλεγχο
            self._log(f"{l.get('shop_name')}: η τιμή διαβάστηκε αλλιώς ({last_src} → {src}), έλεγξέ την")
        try:
            self.db.record_check(l["id"], info.price, info.currency, in_stock, title=info.title, screenshot=shot,
                                 price_eur=price_eur, source=src)
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
                                price_eur=price_eur, stock_known=stock_known)

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

    def _alerts_for(self, l, product, prev, prev_ok, price, currency, in_stock, shot, settings, price_eur=None,
                    stock_known=True):
        """Ειδοποιήσεις για έναν έλεγχο. Όταν η σελίδα δεν λέει καθαρά αν υπάρχει απόθεμα (stock_known=False),
        δεν στέλνεται «έφτασε τον στόχο» ούτε «ξανά διαθέσιμο»: καλύτερα καμία παρά μια ψεύτικη."""
        if price_eur is None and (currency or "").upper() == "EUR":
            price_eur = price
        base = {"product": product["name"], "shop": l.get("shop_name") or "", "region": l.get("region"),
                "region_label": (country_label(l.get("country")) or REGION_LABELS.get(l.get("region"), "")),
                "price": price, "currency": currency, "price_eur": price_eur,
                "url": l["url"], "target": product.get("target_price"),
                "screenshot": str(self.data_dir / shot) if shot else None}
        base["listing_id"] = l.get("id")

        def eur_of(h):
            v = h["price_eur"] if "price_eur" in h.keys() else None
            return v if v is not None else (h["price"] if (h.get("currency") or "EUR").upper() == "EUR" else None)

        out = []
        ok_stock = [h for h in prev_ok if h["in_stock"] is not None]
        # «Ξανά διαθέσιμο» μόνο αν ήταν εξαντλημένο σε δύο συνεχόμενους ελέγχους (όχι από μία λάθος ανάγνωση)
        was_out = bool(ok_stock) and not ok_stock[-1]["in_stock"] and (len(ok_stock) < 2 or not ok_stock[-2]["in_stock"])
        if stock_known and settings.get("notify_back_in_stock", True) and in_stock and was_out:
            out.append({**base, "kind": "in_stock"})
        tgt = product.get("target_price")
        # Σύγκριση με τον τελευταίο έλεγχο ΜΕ τιμή: ένα «99 → χωρίς τιμή → 99» δεν ξαναστέλνει ειδοποίηση
        last_priced = next((h for h in reversed(prev_ok) if h["price"] is not None), None)
        prev_eur = eur_of(last_priced) if last_priced else None
        prev_hit = bool(prev_eur and tgt and prev_eur <= tgt and last_priced["in_stock"])
        # Η τιμή-στόχος είναι σε €: συγκρίνεται με την τιμή σε €. Ισχύει και στον πρώτο έλεγχο.
        if stock_known and settings.get("notify_target_price", True) and tgt and price_eur and in_stock \
                and price_eur <= tgt and not prev_hit:
            out.append({**base, "kind": "target"})
        out = [a for a in out if not self._recently_sent(a)]
        if prev is not None and settings.get("notify_new_low", True) and price and in_stock and not out:
            prices = [v for v in (eur_of(h) for h in prev_ok if h["price"]) if v]
            if price_eur and len(prices) >= 3 and price_eur < min(prices) * 0.99:
                a = {**base, "kind": "new_low"}
                if not self._recently_sent(a):
                    out.append(a)
        return out

    def _recently_sent(self, a) -> bool:
        """Η ίδια ειδοποίηση για την ίδια σελίδα δεν ξαναστέλνεται μέσα σε 24 ώρες, εκτός αν η τιμή έπεσε κι άλλο."""
        last = self.db.last_alert(a["listing_id"], a["kind"])
        if not last:
            return False
        age_h = (datetime.now() - datetime.fromisoformat(last["sent_at"])).total_seconds() / 3600
        if age_h >= ALERT_COOLDOWN_H:
            return False
        if a["kind"] != "in_stock" and a.get("price_eur") and last.get("price_eur") \
                and a["price_eur"] < last["price_eur"] * 0.99:
            return False
        return True

    def _send_alerts(self, alerts, settings):
        for a in alerts:
            if a.get("listing_id"):
                try:
                    self.db.record_alert(a["listing_id"], a["kind"], a.get("price_eur"))
                except Exception:  # noqa: BLE001
                    log.exception("record alert failed")
        subject, text, body = notifier.format_alerts(alerts)
        if self.notify_callback:
            try:
                self.notify_callback(subject.replace("[Price Tracker] ", ""))
            except Exception:  # noqa: BLE001
                pass
        if settings.get("email_enabled"):
            self._email(settings, subject, text, body, [a["screenshot"] for a in alerts if a.get("screenshot")])

    def _email(self, settings, subject, text, body=None, attachments=None, queue_on_fail=True) -> bool:
        """Στέλνει email. Αν αποτύχει, μπαίνει σε ουρά και ξαναδοκιμάζεται (έως 48 ώρες), και το σφάλμα
        φαίνεται στην εφαρμογή αντί να χάνεται σε ένα log."""
        try:
            self._send_now(settings, subject, text, body, attachments)
            return True
        except Exception as e:  # noqa: BLE001
            permanent = self._email_failed(e)
            if queue_on_fail and not permanent:
                with self._outbox_lock:
                    box = list(self.db.get_settings().get("email_outbox") or [])
                    box.append({"subject": subject, "text": text, "body": body,
                                "attachments": list(attachments or []), "ts": time.time(), "tries": 1})
                    self.db.set_settings({"email_outbox": box[-50:]})
            return False

    def _send_now(self, settings, subject, text, body=None, attachments=None):
        user = settings.get("smtp_user", "")
        pw = notifier.load_password(self.db, user)
        if not pw and user and notifier.host_changed(self.db):
            raise RuntimeError("Άλλαξε ο SMTP server: γράψε ξανά τον κωδικό εφαρμογής στις Ρυθμίσεις")
        notifier.send_email(settings, pw, subject, text, body, [Path(p) for p in attachments or []])
        self._log(f"Στάλθηκε email: {subject}")
        self.health.update(email_error=None, email_error_at=None)
        self._email_tray_warned = False

    def _email_failed(self, e) -> bool:
        """Καταγράφει το σφάλμα email. Επιστρέφει True αν είναι μόνιμο για ΑΥΤΟ το μήνυμα (π.χ. ο server το
        απέρριψε επειδή είναι μεγάλο): τότε δεν ξαναδοκιμάζεται, ώστε να μη μπλοκάρει τα υπόλοιπα."""
        import smtplib

        msg = (str(e).splitlines() or [type(e).__name__])[0][:300]
        self._log(f"Αποτυχία email: {msg}")
        self.health.update(email_error=msg, email_error_at=datetime.now().strftime("%d/%m %H:%M"))
        if self.notify_callback and not getattr(self, "_email_tray_warned", False):
            self._email_tray_warned = True  # μία ειδοποίηση ανά σφάλμα, όχι σε κάθε προσπάθεια
            try:
                self.notify_callback("Τα email ειδοποιήσεων δεν στέλνονται. Δες την εφαρμογή.")
            except Exception:  # noqa: BLE001
                pass
        if isinstance(e, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError)):
            code = getattr(e, "smtp_code", None) or 0
            return 500 <= int(code) < 600 and int(code) not in (530, 534, 535)
        return False

    def retry_outbox(self, force: bool = False):
        """Ξαναστέλνει τα email που απέτυχαν (κάθε 15 λεπτά). Ένα μήνυμα που απορρίπτεται μόνιμα βγαίνει από
        την ουρά. Σε σφάλμα σύνδεσης/κωδικού σταματά και ξαναδοκιμάζει αργότερα."""
        settings = self.db.get_settings()
        if not (settings.get("email_outbox") or []) or not settings.get("email_enabled"):
            return
        if not force and time.time() - getattr(self, "_last_outbox_try", 0) < 900:
            return
        self._last_outbox_try = time.time()
        with self._outbox_lock:  # ένα νέο email που αποτυγχάνει στο μεταξύ δεν χάνεται
            box = list(self.db.get_settings().get("email_outbox") or [])
            keep = []
            stop = False
            for m in box:
                if stop:
                    keep.append(m)
                    continue
                if time.time() - float(m.get("ts") or 0) > OUTBOX_MAX_AGE_H * 3600:
                    self._log(f"Το email «{m.get('subject')}» δεν στάλθηκε μέσα σε 48 ώρες και διαγράφηκε")
                    continue
                try:
                    self._send_now(settings, m["subject"], m["text"], m.get("body"), m.get("attachments"))
                    continue
                except Exception as e:  # noqa: BLE001
                    if self._email_failed(e):
                        self._log(f"Το email «{m.get('subject')}» απορρίφθηκε από τον server και διαγράφηκε")
                        continue
                m["tries"] = int(m.get("tries") or 1) + 1
                keep.append(m)
                stop = True  # ο server δεν απαντά ή ο κωδικός είναι λάθος: τα υπόλοιπα την επόμενη φορά
            self.db.set_settings({"email_outbox": keep})

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
        keys = {k for _, _, k in due}
        shown = getattr(self, "_warranty_tray_shown", set())
        if self.notify_callback and not keys <= shown:  # στο tray μία φορά, όχι σε κάθε νέα προσπάθεια email
            self._warranty_tray_shown = shown | keys
            try:
                self.notify_callback(subject.replace("[Price Tracker] ", ""))
            except Exception:  # noqa: BLE001
                pass
        if settings.get("email_enabled") and not self._email(settings, subject, text, queue_on_fail=False):
            return []  # δεν σημειώνεται «σταλμένη»: ξαναδοκιμάζει στον επόμενο έλεγχο εγγυήσεων
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

    def detect_search(self, shop_id: int, word: str | None = None):
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
                cands = search_url_candidates(home.html, home.final_url)[:16]
                structured = [c for c in cands if not any(c.endswith(p) for p in _common_paths())]
                guess = structured[0] if structured else None
                words = [word] if word else (probe_words(home.html) + ["samsung"])[:2]
                self.status["total"] = len(cands)
                for i, tmpl in enumerate(cands, 1):
                    if self._stop.is_set() or found:
                        break
                    self.status["progress"] = i
                    for w in words:
                        try:
                            page, res = br.open(search_url(tmpl, w), timeout_ms=30000)
                            page.close()
                        except Exception:  # noqa: BLE001
                            break
                        if res.status and res.status >= 400:
                            break  # η διεύθυνση δεν υπάρχει: επόμενο μοτίβο
                        if looks_like_search_results(res.html, res.final_url, w, home.final_url):
                            found = tmpl
                            break
        except Exception as e:  # noqa: BLE001
            st.update(state="notfound", message=f"Δεν βρέθηκε: {e}")
            self._log(f"{shop['name']}: εύρεση αναζήτησης απέτυχε: {e}")
            return
        sh = self.db.get_shop(shop_id)
        if sh:
            tried = _detect_tried(self.db.get_settings())
            tried[sh["key"]] = time.time()
            self.db.set_settings({"detect_tried": tried})
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
            self._email(settings, subject, text)  # σε αποτυχία μπαίνει στην ουρά και φαίνεται στο «Υγεία»
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
