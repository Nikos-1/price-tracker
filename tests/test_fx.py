import time

from pricetracker import fx
from pricetracker.db import Database
from pricetracker.extract import currency_for_host, extract
from pricetracker.web import create_app
from pricetracker.worker import Worker

ECB = """<?xml version="1.0" encoding="UTF-8"?><gesmes:Envelope><Cube><Cube time='2026-11-09'>
<Cube currency='USD' rate='1.1214'/><Cube currency='GBP' rate='0.8712'/><Cube currency='JPY' rate='171.3'/>
</Cube></Cube></gesmes:Envelope>"""
RATES = {"date": "2026-11-09", "ts": time.time(), "rates": {"USD": 1.1214, "GBP": 0.8712}}


class FakeWorker:
    status = {"running": False}

    def enqueue(self, *a, **k):
        return True

    def pending(self):
        return 0


def test_parse_and_convert():
    date, rates = fx.parse_ecb_xml(ECB)
    assert date == "2026-11-09" and rates["USD"] == 1.1214 and rates["GBP"] == 0.8712
    assert fx.to_eur(450, "USD", {"rates": rates}) == 401.28
    assert fx.to_eur(100, "EUR", {}) == 100
    assert fx.to_eur(100, "XXX", {"rates": rates}) is None
    assert fx.fmt_with_eur(450, "USD", 401.28) == "450,00 $ / ≈401,28 €"
    assert fx.fmt_with_eur(1234.5, "EUR", 1234.5) == "1.234,50 €"


def test_rates_cache(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    calls = []

    def fetch():
        calls.append(1)
        return fx.parse_ecb_xml(ECB)

    assert fx.get_rates(db, fetch=fetch)["rates"]["USD"] == 1.1214
    fx.get_rates(db, fetch=fetch)
    assert len(calls) == 1  # μέσα στην ώρα: από την αποθήκευση

    def broken():
        raise OSError("offline")

    assert fx.get_rates(db, fetch=broken, force=True)["rates"]["USD"] == 1.1214  # κρατάει τις τελευταίες


def test_currency_from_host():
    html = ('<html><script type="application/ld+json">{"@type":"Product","name":"PSU",'
            '"offers":{"@type":"Offer","price":"99.99"}}</script></html>')
    assert extract(html, "https://www.scan.co.uk/products/psu").currency == "GBP"
    assert extract(html, "https://www.newegg.com/p/1").currency == "USD"
    assert extract(html, "https://www.alternate.de/x").currency == "EUR"
    assert currency_for_host("amazon.com.be") == "EUR"


def test_best_price_compares_in_eur(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.set_settings({"fx": RATES})
    pid = db.create_product("PSU")
    us = db.add_listing(pid, "https://www.newegg.com/p/1")
    uk = db.add_listing(pid, "https://www.scan.co.uk/p/1")
    db.record_check(us, 120.0, "USD", True, price_eur=fx.to_eur(120, "USD", RATES))  # 107,01 €
    db.record_check(uk, 90.0, "GBP", True)  # παλιά εγγραφή χωρίς € → μετατρέπεται τώρα (103,31 €)
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    p = c.get(f"/api/products/{pid}").json
    best = p["best"]["OTHER"]
    assert best["currency"] == "GBP" and best["orig_price"] == 90.0 and best["price"] == 103.31
    assert {l["last_currency"]: l["last_price_eur"] for l in p["listings"]} == {"USD": 107.01, "GBP": 103.31}
    assert all(h["price_eur"] for h in p["history"])
    assert c.get("/api/export.xlsx").status_code == 200


def test_target_alert_uses_eur(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    product = {"name": "PSU", "target_price": 110.0}
    l = {"shop_name": "Newegg", "region": "OTHER", "country": "US", "url": "https://www.newegg.com/p/1"}
    prev = {"price": 140.0, "currency": "USD", "price_eur": 124.84, "in_stock": 1}
    # 120 $ ≈ 107 € <= 110 € στόχος, ενώ τα 120 (σε $) δεν είναι <= 110
    out = w._alerts_for(l, product, prev, [prev], 120.0, "USD", True, None, {}, price_eur=107.01)
    assert [a["kind"] for a in out] == ["target"]
    from pricetracker.notifier import format_alerts

    subject, text, _ = format_alerts(out)
    assert "120,00 $ / ≈107,01 €" in subject and "στόχος 110,00 €" in text
