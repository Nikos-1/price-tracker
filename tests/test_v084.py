"""v0.8.4: βάση εκτός OneDrive, σωστή ανάγνωση τιμής/διαθεσιμότητας/νομίσματος, ειδοποιήσεις χωρίς σπαμ."""
import json
import os
import time
from datetime import datetime, timedelta

from pricetracker import dbfile, notifier
from pricetracker.db import Database
from pricetracker.extract import detect_currency, extract, normalize_currency, parse_price
from pricetracker.web import create_app
from pricetracker.worker import Worker


class FakeWorker:
    status = {"running": False}
    health = {}

    def enqueue(self, *a, **k):
        return True

    def pending(self):
        return 0


# ---------------------------------------------------------------- βάση
def test_db_moves_from_documents_to_appdata_with_daily_backup(tmp_path):
    docs, app = tmp_path / "Documents" / "PriceTracker", tmp_path / "Local" / "PriceTracker"
    docs.mkdir(parents=True)
    Database(docs / "pricetracker.db").create_product("Ρολόι")  # v0.8.x: η βάση ήταν στα Έγγραφα
    live, events = dbfile.place_live_db(docs, app)
    assert live == app / "pricetracker.db" and events and not (docs / "pricetracker.db").exists()
    assert Database(live).list_products()[0]["name"] == "Ρολόι"
    assert dbfile.backup_if_due(live, docs) is True
    backup = docs / dbfile.BACKUP_NAME
    assert dbfile.is_ok(backup) and Database(backup).list_products()[0]["name"] == "Ρολόι"
    assert dbfile.backup_if_due(live, docs) is False  # όχι ξανά μέσα στην ίδια μέρα
    assert sorted(p.name for p in docs.iterdir()) == [dbfile.BACKUP_NAME]
    # μετά από format: δεν υπάρχει τοπική βάση, επαναφέρεται από το αντίγραφο
    for f in app.iterdir():
        f.unlink()
    live, events = dbfile.place_live_db(docs, app)
    assert "επαναφορά" in " ".join(events) and Database(live).list_products()[0]["name"] == "Ρολόι"


def test_corrupt_db_is_kept_aside_and_restored(tmp_path):
    docs, app = tmp_path / "docs", tmp_path / "app"
    docs.mkdir()
    app.mkdir()
    Database(app / "pricetracker.db").create_product("TV")
    dbfile.backup_if_due(app / "pricetracker.db", docs, force=True)
    (app / "pricetracker.db").write_bytes(b"SQLite format 3\x00" + b"\xff" * 5000)
    live, events = dbfile.place_live_db(docs, app)
    assert any("χαλασμένη" in e for e in events) and any(p.name.startswith("pricetracker-χαλασμένη") for p in app.iterdir())
    assert Database(live).list_products()[0]["name"] == "TV"


def test_newer_local_db_wins_over_old_documents_copy(tmp_path):
    docs, app = tmp_path / "docs", tmp_path / "app"
    docs.mkdir()
    app.mkdir()
    Database(docs / "pricetracker.db").create_product("παλιό")
    old = time.time() - 3600
    os.utime(docs / "pricetracker.db", (old, old))
    Database(app / "pricetracker.db").create_product("νέο")
    live, _ = dbfile.place_live_db(docs, app)
    assert Database(live).list_products()[0]["name"] == "νέο" and not (docs / "pricetracker.db").exists()


def test_backup_before_update(tmp_path):
    db = Database(tmp_path / "pricetracker.db")
    db.set_settings({"app_version": "0.8.3"})
    assert dbfile.backup_before_update(tmp_path / "pricetracker.db", tmp_path, "0.8.4")
    assert (tmp_path / dbfile.PRE_UPDATE_NAME).exists()
    db.set_settings({"app_version": "0.8.4"})
    assert not dbfile.backup_before_update(tmp_path / "pricetracker.db", tmp_path, "0.8.4")


# ---------------------------------------------------------------- τιμή
def test_price_parsing_edge_cases():
    assert parse_price("-20% 1.039,20 €") == 1039.20
    assert parse_price("1 299,00 €") == 1299.0  # λεπτό κενό
    assert parse_price("Από 3 άτοκες δόσεις των 99,90 €") == 99.90
    assert parse_price("1.299,00 €") == 1299.0 and parse_price("1.299,- €") == 1299.0
    assert parse_price("€1,234.56") == 1234.56 and parse_price("349") == 349.0


def test_generic_price_ignores_crossed_out_and_badges():
    html = """<html><body><h1>TV</h1><div class="price"><del>1.299,00 €</del> <ins>999,00 €</ins></div></body></html>"""
    assert extract(html, "https://www.shop.gr/p/1").price == 999.0
    html = """<html><body><h1>TV</h1><span class="price-old">1.299,00 €</span>
              <div class="price"><span class="discount-badge">-20%</span> 1.039,20 €</div></body></html>"""
    assert extract(html, "https://www.shop.gr/p/1").price == 1039.20


def test_jsonld_prefers_buyable_offer_and_skips_strikethrough():
    def page(offers):
        return ('<html><head><script type="application/ld+json">'
                + json.dumps({"@type": "Product", "name": "PSU", "offers": offers}) + "</script></head><body></body></html>")

    info = extract(page([{"@type": "Offer", "price": "79", "priceCurrency": "EUR",
                          "availability": "https://schema.org/OutOfStock"},
                         {"@type": "Offer", "price": "99", "priceCurrency": "EUR",
                          "availability": "https://schema.org/InStock"}]), "https://www.shop.gr/p")
    assert info.price == 99 and info.in_stock is True
    info = extract(page([{"@type": "Offer", "priceCurrency": "EUR", "availability": "https://schema.org/InStock",
                          "priceSpecification": [{"priceType": "https://schema.org/StrikethroughPrice", "price": 150},
                                                 {"price": 120}]}]), "https://www.shop.gr/p")
    assert info.price == 120


def test_related_products_do_not_decide_stock():
    html = """<html><body><main><h1>Κουζίνα</h1><div class="price">499,00 €</div>
              <button class="btn add">Προσθήκη στο καλάθι</button></main>
              <aside>Σχετικά: Ψυγείο X – Εξαντλήθηκε</aside></body></html>"""
    assert extract(html, "https://www.shop.gr/p/1").in_stock is True
    html = """<html><body><main><h1>Κουζίνα</h1><div class="price">499,00 €</div>
              <button disabled>Προσθήκη στο καλάθι</button><p>Εξαντλήθηκε</p></main></body></html>"""
    assert extract(html, "https://www.shop.gr/p/1").in_stock is False


# ---------------------------------------------------------------- νόμισμα
def test_currency_symbols_and_false_hits():
    assert normalize_currency("€") == "EUR" and normalize_currency("eur") == "EUR"
    assert normalize_currency("VAT") is None
    assert detect_currency("12,99 incl. VAT") is None
    assert detect_currency("Only 2 left 12.99") is None
    assert detect_currency("1.299 Ft") == "HUF" and detect_currency("12,99 €") == "EUR"
    html = ('<html><head><script type="application/ld+json">{"@type":"Product","name":"X","offers":'
            '{"@type":"Offer","price":"10","priceCurrency":"€"}}</script></head><body></body></html>')
    assert extract(html, "https://www.shop.com/p").currency == "EUR"


# ---------------------------------------------------------------- ειδοποιήσεις
def _alert_setup(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    pid = db.create_product("PSU", target_price=100.0)
    lid = db.add_listing(pid, "https://www.shop.gr/p/1")
    return db, w, db.get_listing(lid), db.get_product(pid)


def test_target_alert_not_repeated_every_hour(tmp_path):
    db, w, l, p = _alert_setup(tmp_path)
    prev = {"price": 120.0, "currency": "EUR", "price_eur": 120.0, "in_stock": 1}
    out = w._alerts_for(l, p, prev, [prev], 95.0, "EUR", True, None, {}, price_eur=95.0)
    assert [a["kind"] for a in out] == ["target"]
    w._send_alerts(out, {})
    # μια ανάγνωση χωρίς τιμή «μηδένισε» το προηγούμενο, αλλά δεν ξαναστέλνεται μέσα στο 24ωρο
    assert w._alerts_for(l, p, prev, [prev], 95.0, "EUR", True, None, {}, price_eur=95.0) == []
    # εκτός αν έπεσε κι άλλο
    assert [a["kind"] for a in w._alerts_for(l, p, prev, [prev], 90.0, "EUR", True, None, {}, price_eur=90.0)] == ["target"]


def test_back_in_stock_needs_two_out_of_stock_checks(tmp_path):
    db, w, l, p = _alert_setup(tmp_path)
    p["target_price"] = None
    ins = {"price": 120.0, "currency": "EUR", "price_eur": 120.0, "in_stock": 1}
    out = {"price": 120.0, "currency": "EUR", "price_eur": 120.0, "in_stock": 0}
    assert w._alerts_for(l, p, out, [ins, out], 120.0, "EUR", True, None, {}, price_eur=120.0) == []
    got = w._alerts_for(l, p, out, [ins, out, out], 120.0, "EUR", True, None, {}, price_eur=120.0)
    assert [a["kind"] for a in got] == ["in_stock"]


def test_failed_email_is_queued_and_retried(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    db.set_settings({"email_enabled": True, "smtp_user": "me@gmail.com"})
    monkeypatch.setattr(notifier, "load_password", lambda db, user: "pw")
    calls = []

    def fail(*a, **k):
        calls.append(1)
        raise OSError("Connection refused")

    monkeypatch.setattr(notifier, "send_email", fail)
    w._send_alerts([{"kind": "target", "product": "PSU", "shop": "S", "price": 1.0, "currency": "EUR",
                     "price_eur": 1.0, "url": "u", "target": 2.0}], db.get_settings())
    assert len(db.get_settings()["email_outbox"]) == 1 and w.health["email_error"]
    monkeypatch.setattr(notifier, "send_email", lambda *a, **k: calls.append(2))
    w.retry_outbox(force=True)
    assert db.get_settings()["email_outbox"] == [] and calls[-1] == 2 and w.health["email_error"] is None


def test_warranty_reminder_not_marked_sent_when_email_fails(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    pid = db.create_product("Ψυγείο")
    soon = (datetime.now() - timedelta(days=720)).date().isoformat()
    db.update_product(pid, purchased=1, purchased_at=soon, warranty_months=24)
    db.set_settings({"email_enabled": True, "smtp_user": "me@gmail.com", "warranty_reminder_days": 30})
    monkeypatch.setattr(notifier, "load_password", lambda db, user: "pw")
    monkeypatch.setattr(notifier, "send_email", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert w.check_warranties() == []
    assert not db.get_settings().get("warranty_notified")
    monkeypatch.setattr(notifier, "send_email", lambda *a, **k: None)
    assert len(w.check_warranties()) == 1


# ---------------------------------------------------------------- έλεγχοι
def test_blocked_403_and_captcha_given_up_once_per_cycle(tmp_path, monkeypatch):
    from pricetracker.browser import PageResult

    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    pid = db.create_product("P")
    l1 = db.add_listing(pid, "https://www.idealo.de/p/1")
    l2 = db.add_listing(pid, "https://www.idealo.de/p/2")

    class Page:
        def close(self):
            pass

    class Br:
        headless = True

        def open(self, url):
            return Page(), PageResult(url, url, "<html><body>Access</body></html>", 403)

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
    # v0.8.5: ένα σκέτο 403 (χωρίς captcha) δεν ανοίγει παράθυρο· το δεύτερο του ίδιου καταστήματος παραλείπεται
    assert asked == []
    assert "μπλόκαρε" in db.get_listing(l1)["last_error"] and "παραλείφθηκε" in db.get_listing(l2)["last_error"]


def test_network_failure_schedules_retry(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    pid = db.create_product("P")
    db.add_listing(pid, "https://www.shop.gr/p/1")

    class Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(w, "_browser", lambda: Ctx())

    def boom(*a, **k):
        raise RuntimeError("page.goto: net::ERR_INTERNET_DISCONNECTED at https://www.shop.gr/p/1")

    monkeypatch.setattr(w, "_check_one", boom)
    w.check_listings(db.list_listings(product_id=pid), "Έλεγχος")
    assert w.retry_at and w.retry_at - time.time() > 500


# ---------------------------------------------------------------- ρυθμίσεις / υγεία
def test_changing_smtp_user_keeps_password(tmp_path, monkeypatch):
    store = {}

    class KR:
        def set_password(self, s, u, p):
            store[u] = p

        def get_password(self, s, u):
            return store.get(u)

    monkeypatch.setattr(notifier, "_keyring", lambda: KR())
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    assert c.put("/api/settings", json={"smtp_user": "a@gmail.com", "smtp_password": "pw"}).status_code == 200
    assert c.put("/api/settings", json={"smtp_user": "b@gmail.com"}).status_code == 200
    assert notifier.load_password(db, "b@gmail.com") == "pw"


def test_status_shows_health_issues(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    w = FakeWorker()
    w.health = {"scheduler_ts": time.time() - 3600, "email_error": "535 auth", "email_error_at": "10/11 09:00"}
    db.set_settings({"email_enabled": True})
    c = create_app(db, w, tmp_path).test_client()
    h = [x["text"] for x in c.get("/api/status").get_json()["health"]]  # από v0.8.5: {text, fix}
    assert any("σταματήσει" in x for x in h) and any("535" in x and "κωδικός εφαρμογής" in x for x in h)
