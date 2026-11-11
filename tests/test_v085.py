"""v0.8.5: ασφαλής μεταφορά βάσης, σωστό απόθεμα/τιμές, ουρά email, μπλοκαρίσματα, ενημερώσεις, εύρεση αναζήτησης."""
import json
import smtplib
import threading
import time

from pricetracker import dbfile, notifier, updater as upd
from pricetracker.db import Database
from pricetracker.extract import extract, parse_price
from pricetracker.search import looks_like_search_results, probe_words, search_url_candidates
from pricetracker.web import create_app, explain_email_error
from pricetracker.worker import Worker, _interleave_by_host


class FakeWorker:
    status = {"running": False}
    health = {}

    def enqueue(self, *a, **k):
        return True

    def pending(self):
        return 0


# ---------------------------------------------------------------- βάση (Ν1–Ν3, Ν15)
def test_move_db_never_overwrites_with_empty_db(tmp_path, monkeypatch):
    import pathlib

    src, dst = tmp_path / "a" / "pricetracker.db", tmp_path / "b" / "pricetracker.db"
    src.parent.mkdir()
    Database(src).create_product("Ρολόι")
    (src.parent / "pricetracker.db-journal").write_bytes(b"")
    real_unlink = pathlib.Path.unlink

    def unlink(self, missing_ok=False):  # βοηθητικό αρχείο που δεν σβήνεται (π.χ. το κρατά το OneDrive)
        if self.name.endswith("-journal") and self.parent == src.parent:
            raise PermissionError("in use")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    assert dbfile._move_db(src, dst) is True
    monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
    assert Database(dst).list_products()[0]["name"] == "Ρολόι"
    # Η πηγή έλειπε: τίποτα δεν γράφεται πάνω στη σωστή βάση
    assert dbfile._move_db(tmp_path / "λείπει.db", dst) is False
    assert Database(dst).list_products()[0]["name"] == "Ρολόι"


def test_copy_db_refuses_missing_source(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        dbfile.copy_db(tmp_path / "λείπει.db", tmp_path / "x.db")
    assert not (tmp_path / "x.db").exists() and not (tmp_path / "λείπει.db").exists()


def test_is_ok_with_special_characters_in_path(tmp_path):
    for name in ("Νίκος%20Σ", "a#b", "κενό διάστημα"):
        d = tmp_path / name
        d.mkdir()
        Database(d / "pricetracker.db").create_product("x")
        assert dbfile.is_ok(d / "pricetracker.db"), name
        bad = d / "bad.db"
        bad.write_bytes(b"not a database" * 100)
        assert not dbfile.is_ok(bad), name


def test_corrupt_db_without_backup_is_reported(tmp_path):
    docs, app = tmp_path / "Docs", tmp_path / "App"
    docs.mkdir()
    app.mkdir()
    (app / "pricetracker.db").write_bytes(b"garbage" * 1000)
    live, events = dbfile.place_live_db(docs, app)
    probs = dbfile.problems(events)
    assert probs and any("χαλάσει" in p for p in probs) and any("κενή βάση" in p for p in probs)
    assert list(app.glob("pricetracker-χαλασμένη-*.db"))


def test_corrupt_db_restored_from_backup_is_reported(tmp_path):
    docs, app = tmp_path / "Docs", tmp_path / "App"
    app.mkdir()
    Database(app / "pricetracker.db").create_product("Ρολόι")
    dbfile.backup_if_due(app / "pricetracker.db", docs, force=True)
    (app / "pricetracker.db").write_bytes(b"garbage" * 1000)
    live, events = dbfile.place_live_db(docs, app)
    assert Database(live).list_products()[0]["name"] == "Ρολόι"
    assert any("επαναφορά" in p for p in dbfile.problems(events))


def test_corrupt_old_documents_db_is_not_used(tmp_path):
    docs, app = tmp_path / "Docs", tmp_path / "App"
    docs.mkdir()
    (docs / "pricetracker.db").write_bytes(b"garbage" * 1000)
    live, _ = dbfile.place_live_db(docs, app)
    assert live == app / "pricetracker.db" and not (docs / "pricetracker.db").exists()


def test_two_computers_never_overwrite_each_others_backup(tmp_path, monkeypatch):
    docs, a, b = tmp_path / "Docs", tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    Database(a / "pricetracker.db").create_product("από LAPTOP")
    Database(b / "pricetracker.db").create_product("από DESKTOP")
    monkeypatch.setenv("COMPUTERNAME", "LAPTOP")
    assert dbfile.backup_if_due(a / "pricetracker.db", docs, force=True)
    monkeypatch.setenv("COMPUTERNAME", "DESKTOP")
    assert dbfile.backup_if_due(b / "pricetracker.db", docs, force=True)
    shared, own = docs / dbfile.BACKUP_NAME, docs / "Αντίγραφο βάσης (DESKTOP).db"
    assert Database(shared).list_products()[0]["name"] == "από LAPTOP"
    assert Database(own).list_products()[0]["name"] == "από DESKTOP"
    # Επαναφορά στο DESKTOP: από το δικό του αντίγραφο
    (b / "pricetracker.db").unlink()
    live, _ = dbfile.place_live_db(docs, b)
    assert Database(live).list_products()[0]["name"] == "από DESKTOP"


# ---------------------------------------------------------------- απόθεμα και τιμές (Ν4, Ν5, Ν16, Α4, Α6)
def test_related_products_cart_button_does_not_mean_in_stock():
    html = """<html><body><header><span>0 προϊόντα στο καλάθι</span><button>Στο καλάθι</button></header>
      <main><h1>Ψυγείο</h1><span class="price">499,00 €</span><p>Εξαντλήθηκε</p>
      <div class="related-products"><button>Προσθήκη στο καλάθι</button></div></main></body></html>"""
    assert extract(html, "https://shop.gr/p").in_stock is False


def test_own_cart_button_means_in_stock():
    html = """<html><body><main class="product-page"><h1>Ψυγείο</h1><span class="price">499,00 €</span>
      <button class="add-to-cart">Προσθήκη στο καλάθι</button></main></body></html>"""
    assert extract(html, "https://shop.gr/p").in_stock is True


def test_old_price_filter_matches_whole_class_words():
    for cls in ("price font-bold", "price placeholder"):
        html = f'<html><body><h1>X</h1><span class="{cls}">299,00 €</span></body></html>'
        assert extract(html, "https://shop.gr/p").price == 299.0, cls
    holder = '<div class="price"><span class="price-holder">299,00 €</span></div>'
    assert extract(f"<html><body>{holder}</body></html>", "https://shop.gr/p").price == 299.0
    shopify = '<div class="price"><span class="price-item price-item--regular">50,00 €</span></div>'
    assert extract(f"<html><body>{shopify}</body></html>", "https://shop.gr/p").price == 50.0
    sale = ('<div class="product-info"><div class="price"><span class="price-item--regular">50,00 €</span>'
            '<span class="price-item--sale">40,00 €</span></div></div>')
    assert extract(f"<html><body>{sale}</body></html>", "https://shop.gr/p").price == 40.0
    disc = '<div class="price"><span class="price-discounted">35,00 €</span></div>'
    assert extract(f"<html><body>{disc}</body></html>", "https://shop.gr/p").price == 35.0


def test_alpha_currency_symbols_and_instalments():
    assert parse_price("Microsoft 365 Personal 69,99 €") == 69.99
    assert parse_price("3 άτοκες δόσεις των 116,33 € · 349,00 €") == 349.0
    assert parse_price("Από 29 €/μήνα ή 599 €") == 599.0
    assert parse_price("349 €") == 349.0


def test_variant_from_url_and_page_order():
    ld = {"@context": "https://schema.org", "@type": "ProductGroup", "name": "T",
          "hasVariant": [{"@type": "Product", "sku": "S", "offers": {"@type": "Offer", "price": "10", "priceCurrency": "EUR"}},
                         {"@type": "Product", "sku": "L", "offers": {"@type": "Offer", "price": "15", "priceCurrency": "EUR"}}]}
    html = f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body></body></html>'
    assert extract(html, "https://shop.gr/p?sku=S").price == 10.0
    assert extract(html, "https://shop.gr/p?sku=L").price == 15.0
    assert extract(html, "https://shop.gr/p").price == 10.0  # η πρώτη της σελίδας, όχι η τελευταία


def test_preorder_is_unknown_not_out_of_stock():
    ld = {"@type": "Product", "name": "PS6", "offers": {"@type": "Offer", "price": "599", "priceCurrency": "EUR",
                                                        "availability": "https://schema.org/PreOrder"}}
    html = f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body></body></html>'
    info = extract(html, "https://shop.gr/p")
    assert info.price == 599.0 and info.in_stock is not False


def test_price_source_is_recorded():
    html = '<html><body><h1>X</h1><span class="price">299,00 €</span></body></html>'
    assert extract(html, "https://shop.gr/p").price_source == "generic"


# ---------------------------------------------------------------- ειδοποιήσεις (Ν4, Ν11)
def _hist(price, in_stock=True, eur=None):
    return {"price": price, "currency": "EUR", "in_stock": in_stock, "price_eur": eur or price, "error": None}


def _alerts(w, prev_ok, price, stock_known=True, target=100):
    l = {"id": None, "url": "https://shop.gr/p", "shop_name": "Shop", "region": "GR"}
    product = {"name": "P", "target_price": target}
    prev = prev_ok[-1] if prev_ok else None
    return [a["kind"] for a in w._alerts_for(l, product, prev, prev_ok, price, "EUR", True, None, {},
                                              price_eur=price, stock_known=stock_known)]


def test_no_target_alert_when_stock_unknown(tmp_path):
    w = Worker(Database(tmp_path / "db.sqlite"), tmp_path)
    assert _alerts(w, [_hist(120)], 90, stock_known=False) == []
    assert _alerts(w, [_hist(120)], 90) == ["target"]


def test_target_alert_on_first_check_and_no_flapping(tmp_path):
    w = Worker(Database(tmp_path / "db.sqlite"), tmp_path)
    assert _alerts(w, [], 90) == ["target"]
    no_price = {"price": None, "currency": None, "in_stock": False, "price_eur": None, "error": None}
    assert _alerts(w, [_hist(99), no_price], 99) == []  # 99 → χωρίς τιμή → 99: όχι ξανά


# ---------------------------------------------------------------- email (Ν6, Ν7, Ν12, Ν14)
def _email_worker(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.set_settings({"email_enabled": True, "smtp_user": "me@example.com", "email_to": "me@example.com"})
    w = Worker(db, tmp_path)
    return db, w


def test_new_failed_email_not_lost_during_retry(tmp_path, monkeypatch):
    db, w = _email_worker(tmp_path)
    monkeypatch.setattr(notifier, "load_password", lambda *a, **k: "pw")
    db.set_settings({"email_outbox": [{"subject": "παλιό", "text": "t", "ts": time.time(), "tries": 1}]})
    inside, release = threading.Event(), threading.Event()

    def send(settings, pw, subject, *a, **k):
        if subject == "παλιό":
            inside.set()
            release.wait(5)
        raise OSError("down")

    monkeypatch.setattr(notifier, "send_email", send)
    t = threading.Thread(target=w.retry_outbox, kwargs={"force": True})
    t.start()
    inside.wait(5)
    t2 = threading.Thread(target=w._email, args=(db.get_settings(), "νέο", "t"))
    t2.start()
    time.sleep(0.3)
    release.set()
    t.join(5)
    t2.join(5)
    subjects = sorted(m["subject"] for m in db.get_settings()["email_outbox"])
    assert subjects == ["νέο", "παλιό"]


def test_permanently_rejected_email_does_not_block_queue(tmp_path, monkeypatch):
    db, w = _email_worker(tmp_path)
    monkeypatch.setattr(notifier, "load_password", lambda *a, **k: "pw")
    now = time.time()
    db.set_settings({"email_outbox": [{"subject": "μεγάλο", "text": "t", "ts": now, "tries": 1},
                                      {"subject": "κανονικό", "text": "t", "ts": now, "tries": 1}]})
    sent = []

    def send(settings, pw, subject, *a, **k):
        if subject == "μεγάλο":
            raise smtplib.SMTPDataError(552, b"Message too large")
        sent.append(subject)

    monkeypatch.setattr(notifier, "send_email", send)
    w.retry_outbox(force=True)
    assert sent == ["κανονικό"] and db.get_settings()["email_outbox"] == []


def test_research_email_goes_through_queue(tmp_path, monkeypatch):
    db, w = _email_worker(tmp_path)
    monkeypatch.setattr(notifier, "load_password", lambda *a, **k: "pw")
    monkeypatch.setattr(notifier, "send_email", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    pid = db.create_product("P")
    monkeypatch.setattr(w, "search_product", lambda *a, **k: ["Public"])
    w.research_all()
    assert len(db.get_settings()["email_outbox"]) == 1 and w.health["email_error"]
    assert pid


def test_smtp_host_change_has_clear_message(tmp_path, monkeypatch):
    db, w = _email_worker(tmp_path)
    db.set_settings({"smtp_password_host": "smtp.gmail.com", "smtp_host": "smtp-mail.outlook.com"})
    monkeypatch.setattr(notifier, "send_email", lambda *a, **k: None)
    w._email(db.get_settings(), "x", "t")
    assert "Άλλαξε ο SMTP server" in w.health["email_error"]


def test_reading_settings_writes_nothing(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.set_settings({"smtp_user": "me@example.com"})
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    c.get("/api/settings")
    assert "smtp_password_host" not in db.get_settings()


def test_smtp_errors_explained_in_greek():
    assert "κωδικός εφαρμογής" in explain_email_error("(535, b'5.7.8 Username and Password not accepted')")


# ---------------------------------------------------------------- μπλοκαρίσματα (Ν8, Α1)
def test_interleave_by_shop():
    ls = [{"url": f"https://www.amazon.de/p/{i}"} for i in range(3)] + [{"url": "https://www.plaisio.gr/p/1"}]
    hosts = [l["url"].split("/")[2] for l in _interleave_by_host(ls)]
    assert hosts[:2] == ["www.amazon.de", "www.plaisio.gr"]


def test_real_captcha_asks_once_and_host_rests(tmp_path, monkeypatch):
    from pricetracker.browser import PageResult

    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    pid = db.create_product("P")
    l1 = db.add_listing(pid, "https://www.idealo.de/p/1")
    l2 = db.add_listing(pid, "https://www.idealo.de/p/2")
    captcha = '<html><head><title>Just a moment...</title></head><body><div class="cf-turnstile"></div></body></html>'

    class Page:
        def close(self):
            pass

    class Br:
        headless = True

        def open(self, url):
            return Page(), PageResult(url, url, captcha, 403)

        def restart(self, headless):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    asked = []
    monkeypatch.setattr(w, "_browser", lambda: Br())
    monkeypatch.setattr(w, "_ask_user_to_unblock", lambda br, url, reason: asked.append(url) or None)
    w.check_listings(db.list_listings(product_id=pid), "Έλεγχος")
    assert asked == ["https://www.idealo.de/p/1"]
    assert "παραλείφθηκε" in db.get_listing(l2)["last_error"]
    w.check_listings(db.list_listings(product_id=pid), "Νέα προσπάθεια")  # μέσα στις 3 ώρες: ούτε ερώτηση
    assert asked == ["https://www.idealo.de/p/1"] and db.get_listing(l1)["last_error"]


# ---------------------------------------------------------------- ενημερώσεις (Ν9)
def test_pending_update_counts_as_failed_until_version_matches(tmp_path, monkeypatch):
    u = upd.Updater(Database(tmp_path / "db.sqlite"), tmp_path)
    u.dir.mkdir(parents=True)
    pending = u.dir / "update_pending.txt"
    pending.write_text("99.0.0", encoding="utf-8")
    assert u._failed_version() == "99.0.0"  # αμέσως, όχι μετά από 10 λεπτά
    pending.write_text(upd.__version__, encoding="utf-8")
    assert u._failed_version() is None and not pending.exists()


def test_robocopy_failure_writes_fail_file():
    assert "$rc -ge 8" in upd.PS_SCRIPT and "$FailFile" in upd.PS_SCRIPT.split("robocopy")[-1]


# ---------------------------------------------------------------- αναζήτηση καταστήματος (Α5)
BOURNOUS = """<html><body><input id="sb" placeholder="πατήστε Enter">
<script>function go(v){location.href="/search.php?searchfor="+encodeURIComponent(v)}</script>
<a href="/stihl-ms-170.html">STIHL MS 170 Αλυσοπρίονο</a><a href="/stihl-fs.html">STIHL FS 55</a>
<a href="/karcher.html">KARCHER K2</a><a href="/karcher2.html">Karcher K3</a></body></html>"""


def test_search_detection_for_js_search_box():
    cands = search_url_candidates(BOURNOUS, "https://www.bournous-tools.gr/")
    assert cands[0] == "https://www.bournous-tools.gr/search.php?searchfor={q}"
    assert probe_words(BOURNOUS)[0] == "stihl"


def test_search_results_with_word_once_and_in_url():
    html = "<html><body><h1>Αναζήτηση για stihl</h1>" + "<a href='/p'>x</a>" * 30 + "</body></html>"
    assert looks_like_search_results(html, "https://x.gr/search.php?searchfor=stihl", "stihl", "https://x.gr/")


def test_failed_detection_is_retried_after_30_days(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    queued = []
    monkeypatch.setattr(w, "enqueue", lambda kind, **kw: queued.append(kw["shop_id"]))
    no_url = [s for s in db.list_shops() if not s.get("search_url")]
    if not no_url:
        return
    key = no_url[0]["key"]
    db.set_settings({"detect_tried": {key: time.time()}})
    w.detect_new_default_shops()
    assert no_url[0]["id"] not in queued
    db.set_settings({"detect_tried": {key: time.time() - 31 * 86400}})
    w.detect_new_default_shops()
    assert no_url[0]["id"] in queued


# ---------------------------------------------------------------- ασφάλεια και οθόνες (Ν17, Ν18, Ο6)
def test_instance_id_and_cross_site_downloads_refused(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path, exports_dir=tmp_path / "exp", instance="abc").test_client()
    assert c.get("/api/instance").get_json() == {"id": "abc"}
    assert c.get("/api/export.xlsx", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.get("/api/settings/export", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert c.get("/api/settings/export", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_undo_accepted_candidate(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    pid = db.create_product("P")
    lid = db.add_listing(pid, "https://www.public.gr/p/1", status="candidate", title="P")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    assert c.post(f"/api/listings/{lid}/accept").status_code == 200
    db.record_check(lid, 10.0, "EUR", True)
    assert c.post(f"/api/listings/{lid}/undo").status_code == 200
    l = db.get_listing(lid)
    assert l["status"] == "candidate" and l["last_price"] is None and db.history(listing_id=lid) == []


def test_url_without_scheme_is_completed(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    r = c.post("/api/products", json={"url": "www.plaisio.gr/product/123"})
    assert r.status_code == 200


def test_every_fetch_goes_through_api_helpers():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "pricetracker" / "static" / "app.js").read_text(encoding="utf-8")
    # Κάθε fetch περνά από api()/apiForm(), που στέλνουν την κεφαλίδα X-PriceTracker
    assert js.count("fetch(") == js.count('"X-PriceTracker": "1"')


# ---------------------------------------------------------------- screenshot μόνο με διαθεσιμότητα (Nick, 11/11)
def test_screenshot_only_when_available_and_replaces_unavailable_one(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    pid = db.create_product("Ψυγείο")
    lid = db.add_listing(pid, "https://www.plaisio.gr/p/1")
    w = Worker(db, tmp_path)
    # Παλιότερη καταχώριση: screenshot από τότε που ήταν εξαντλημένο
    old = tmp_path / "Προϊόντα" / "old.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"jpg")
    db.record_check(lid, 300.0, "EUR", False, screenshot="Προϊόντα/old.jpg")
    readings = iter([(300.0, False), (300.0, False), (300.0, True), (300.0, True), (300.0, False), (300.0, True)])
    shots = []

    class Info:
        blocked = None
        currency = "EUR"
        title = "Ψυγείο"
        ean = mpn = brand = image = None
        price_source = "jsonld"

    class Res:
        html, final_url, status = "", "https://www.plaisio.gr/p/1", 200

    class Br:
        def open(self, url):
            return self, Res()

        def close(self):
            pass

        def screenshot(self, page, path, *a):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"jpg")
            shots.append(path)

    def fake_extract(html, url):
        i = Info()
        i.price, i.in_stock = next(readings)
        return i

    monkeypatch.setattr("pricetracker.worker.extract", fake_extract)
    settings = db.get_settings()
    counts = []
    for _ in range(6):
        l = db.list_listings(product_id=pid, status=None)[0]
        w._check_one(Br(), l, db.get_product(pid), settings)
        counts.append(len(shots))
    # εξαντλημένο → κανένα· γίνεται διαθέσιμο → ένα· μένει διαθέσιμο → κανένα· εξαντλείται → κανένα· ξανά → ένα
    assert counts == [0, 0, 1, 1, 1, 2]
    assert not old.exists() and "Προϊόντα/old.jpg" not in db.product_screenshots(pid)
    assert db.get_listing(lid)["last_screenshot"] and db.get_listing(lid)["last_screenshot"] != "Προϊόντα/old.jpg"
