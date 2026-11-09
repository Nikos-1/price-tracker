import io
import json

from pricetracker.db import Database
from pricetracker.extract import currency_for_host
from pricetracker.web import create_app
from pricetracker.worker import Worker


class FakeWorker:
    status = {"running": False}

    def enqueue(self, *a, **k):
        return True

    def pending(self):
        return 0


def _app(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    return db, create_app(db, FakeWorker(), tmp_path).test_client()


def test_unknown_currency_is_asked_and_fixed(tmp_path):
    db, c = _app(tmp_path)
    assert currency_for_host("www.unknown-shop.xyz") is None
    pid = db.create_product("Ρολόι")
    lid = db.add_listing(pid, "https://www.unknown-shop.xyz/p/1")
    db.record_check(lid, 120.0, None, True)
    p = c.get(f"/api/products/{pid}").get_json()
    l = next(x for x in p["listings"] if x["id"] == lid)
    assert l["last_price"] == 120.0 and not l["last_currency"]
    assert any(x["need_currency"] for x in c.get("/api/products").get_json())
    db.set_settings({"fx": {"date": "2026-11-09", "ts": 9e12, "rates": {"USD": 1.2}}})
    assert c.put(f"/api/listings/{lid}/currency", json={"currency": "USD"}).status_code == 200
    l = next(x for x in c.get(f"/api/products/{pid}").get_json()["listings"] if x["id"] == lid)
    assert l["last_currency"] == "USD" and abs(l["last_price_eur"] - 100.0) < 0.01


def test_sectors_crud_and_shop_mapping(tmp_path):
    db, c = _app(tmp_path)
    names = [x["name"] for x in c.get("/api/sectors").get_json()]
    assert "Ρούχα" in names and "Ηλεκτρικές συσκευές" in names
    sid = c.post("/api/sectors", json={"name": "Είδη κατοικιδίων"}).get_json()["id"]
    assert c.post("/api/sectors", json={"name": "Είδη κατοικιδίων"}).status_code == 400
    assert c.put(f"/api/sectors/{sid}", json={"name": "Κατοικίδια"}).status_code == 200
    shop = next(s for s in c.get("/api/shops").get_json() if s["key"] == "bestprice")
    assert shop["sector_ids"] == []
    c.put(f"/api/shops/{shop['id']}", json={"sector_ids": [sid]})
    shop = next(s for s in c.get("/api/shops").get_json() if s["key"] == "bestprice")
    assert shop["sector_ids"] == [sid]
    assert next(x for x in c.get("/api/sectors").get_json() if x["id"] == sid)["shop_count"] == 1
    c.delete(f"/api/sectors/{sid}")
    assert next(s for s in c.get("/api/shops").get_json() if s["key"] == "bestprice")["sector_ids"] == []


def test_search_only_in_relevant_shops(tmp_path):
    db, c = _app(tmp_path)
    secs = {x["name"]: x["id"] for x in db.list_sectors()}
    shops = {s["key"]: s for s in c.get("/api/shops").get_json()}
    clothes = c.post("/api/shops", json={"name": "Ρουχάδικο", "domain": "www.rouxa-test.gr",
                                         "search_url": "https://www.rouxa-test.gr/s?q={q}",
                                         "sector_ids": [secs["Ρούχα"]]}).get_json()["id"]
    cat = c.post("/api/categories", json={"name": "Κουζίνες"}).get_json()["id"]
    pid = c.post("/api/products", json={"url": "https://www.plaisio.gr/p/1", "name": "Κουζίνα Pitsos",
                                        "category_id": cat, "sector_id": secs["Ηλεκτρικές συσκευές"],
                                        "search": False}).get_json()["id"]
    # η κατηγορία «μαθαίνει» τη γενική κατηγορία
    assert next(x for x in db.list_categories() if x["id"] == cat)["sector_id"] == secs["Ηλεκτρικές συσκευές"]
    w = Worker(db, tmp_path)
    keys = {s["key"] for s in w.search_shops(pid)}
    ids = {s["id"] for s in w.search_shops(pid)}
    assert clothes not in ids
    assert "kotsovolos" in keys and "bestprice" in keys  # σχετικό + «πουλάει απ' όλα»
    assert "mindfactory" not in keys  # μόνο Ηλεκτρονικά & υπολογιστές
    # νέο προϊόν στην ίδια κατηγορία παίρνει αυτόματα τη γενική κατηγορία της
    pid2 = db.create_product("Κουζίνα Bosch", cat)
    assert clothes not in {s["id"] for s in w.search_shops(pid2)}
    # χωρίς γενική κατηγορία: όλα τα καταστήματα
    pid3 = db.create_product("Κάτι")
    assert clothes in {s["id"] for s in w.search_shops(pid3)}
    _ = shops


def test_settings_export_import_keeps_sectors(tmp_path):
    db, c = _app(tmp_path)
    sid = c.post("/api/sectors", json={"name": "Κατοικίδια"}).get_json()["id"]
    shop = next(s for s in c.get("/api/shops").get_json() if s["key"] == "bestprice")
    c.put(f"/api/shops/{shop['id']}", json={"sector_ids": [sid]})
    data = c.get("/api/settings/export").data
    payload = json.loads(data)
    assert "Κατοικίδια" in payload["sectors"]
    assert next(s for s in payload["shops"] if s["domain"] == shop["domain"])["sectors"] == ["Κατοικίδια"]
    db2, c2 = _app(tmp_path / "other")
    r = c2.post("/api/settings/import", data={"file": (io.BytesIO(data), "s.json")},
                content_type="multipart/form-data")
    assert r.status_code == 200
    names = {x["id"]: x["name"] for x in db2.list_sectors()}
    shop2 = next(s for s in c2.get("/api/shops").get_json() if s["key"] == "bestprice")
    assert [names[i] for i in shop2["sector_ids"]] == ["Κατοικίδια"]
    assert c.get("/api/settings/export?format=xlsx").status_code == 200
