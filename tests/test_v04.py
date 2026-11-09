import io
import json
from pathlib import Path

from openpyxl import load_workbook

from pricetracker.db import Database
from pricetracker.web import create_app


class FakeWorker:
    status = {"running": False}

    def enqueue(self, *a, **k):
        return True

    def pending(self):
        return 0


def _shot(tmp_path, db, pid, lid, name):
    rel = f"screenshots/{pid}_prod/{name}.jpg"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_bytes(b"jpg")
    db.record_check(lid, 100.0, "EUR", True, screenshot=rel)
    return tmp_path / rel


def _buy(c, pid, receipt_name="r.pdf", also=()):
    items = [{"product_id": pid, "warranty_months": 24}] + [{"product_id": x, "warranty_months": 12} for x in also]
    r = c.post("/api/purchase", content_type="multipart/form-data", data={
        "items": json.dumps(items), "date": "2026-11-27", "shop": "Plaisio",
        "receipt": (io.BytesIO(b"%PDF"), receipt_name)})
    assert r.status_code == 200, r.json
    return r.json["receipt_id"]


def test_delete_product_removes_everything(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    pid = db.create_product("Corsair RM850x")
    lid = db.add_listing(pid, "https://www.plaisio.gr/p/1")
    shot = _shot(tmp_path, db, pid, lid, "a")
    assert c.delete(f"/api/products/{pid}").status_code == 200
    assert db.get_product(pid) is None and db.history() == []
    assert not shot.exists() and not shot.parent.exists()


def test_delete_purchased_product_keeps_shared_receipt(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    a, b, solo = db.create_product("PSU"), db.create_product("Κουζίνα"), db.create_product("Ποντίκι")
    rid = _buy(c, a, also=[b])
    rid2 = _buy(c, solo, "solo.pdf")
    shared_file = tmp_path / db.get_receipt(rid)["file"]
    solo_file = tmp_path / db.get_receipt(rid2)["file"]
    assert shared_file.exists() and solo_file.exists()
    c.delete(f"/api/products/{a}")
    assert shared_file.exists() and db.get_receipt(rid)  # ανήκει και στην κουζίνα
    c.delete(f"/api/products/{solo}")
    assert not solo_file.exists() and db.get_receipt(rid2) is None


def test_delete_category_keeps_only_purchased(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    cat = c.post("/api/categories", json={"name": "PSU"}).json["id"]
    tracked = db.create_product("Seasonic", category_id=cat)
    bought = db.create_product("Corsair", category_id=cat)
    lt = db.add_listing(tracked, "https://www.plaisio.gr/p/1")
    lb = db.add_listing(bought, "https://www.plaisio.gr/p/2")
    shot_t = _shot(tmp_path, db, tracked, lt, "t")
    _shot(tmp_path, db, bought, lb, "b")
    _buy(c, bought)
    assert c.get(f"/api/categories/{cat}/usage").json == {"tracked": 1, "purchased": 1}
    assert c.delete(f"/api/categories/{cat}").status_code == 200
    assert db.get_product(tracked) is None and not shot_t.exists()
    p = db.get_product(bought)
    assert p and p["purchased"] == 1 and p["category_id"] is None and p["receipt_id"]
    assert [w["id"] for w in c.get("/api/warranties").json] == [bought]


def test_settings_export_import(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    c.put("/api/settings", json={"check_interval_hours": 4, "email_to": "me@example.com", "smtp_password": "x" * 16})
    c.post("/api/shops", json={"name": "Germanos", "domain": "germanos.gr", "region": "GR",
                               "search_url": "https://www.germanos.gr/search?q={q}"})
    r = c.post("/api/settings/export", json={"format": "json"}).json
    data = json.loads(Path(r["path"]).read_text(encoding="utf-8"))
    assert data["settings"]["check_interval_hours"] == 4 and data["settings"]["email_to"] == "me@example.com"
    assert "x" * 16 not in Path(r["path"]).read_text(encoding="utf-8")
    assert "smtp_password_plain" not in data["settings"]
    assert any(s["domain"] == "germanos.gr" for s in data["shops"])

    x = c.post("/api/settings/export", json={"format": "xlsx"}).json
    wb = load_workbook(x["path"])
    assert wb.sheetnames == ["Ρυθμίσεις", "Καταστήματα"]
    assert any(row[0].value == "Germanos" for row in wb["Καταστήματα"].iter_rows(min_row=2))
    assert c.get("/api/settings/export?format=xlsx").status_code == 200

    # Εισαγωγή σε καινούργια βάση
    db2 = Database(tmp_path / "db2.sqlite")
    c2 = create_app(db2, FakeWorker(), tmp_path / "d2").test_client()
    data["shops"].append({"name": "Bad", "domain": "bad.gr", "search_url": "https://bad.gr/s"})  # χωρίς {q}
    r = c2.post("/api/settings/import", content_type="multipart/form-data",
                data={"file": (io.BytesIO(json.dumps(data).encode()), "s.json")})
    assert r.status_code == 200, r.json
    assert r.json["shops_added"] == 1
    assert db2.get_settings()["check_interval_hours"] == 4
    assert db2.shop_by_domain("germanos.gr") and not db2.shop_by_domain("bad.gr")
    bad = c2.post("/api/settings/import", content_type="multipart/form-data",
                  data={"file": (io.BytesIO(b"{}"), "x.json")})
    assert bad.status_code == 400


def test_removed_default_shops(tmp_path):
    import sqlite3

    db = Database(tmp_path / "db.sqlite")
    keys = {s["key"] for s in db.list_shops()}
    assert not keys & {"amazon_de", "amazon_it", "mediamarkt_gr", "ebay_de"}
    # Παλιά βάση (v0.3) με Amazon σε χρήση και MediaMarkt αχρησιμοποίητο
    con = sqlite3.connect(tmp_path / "db.sqlite")
    con.execute("DELETE FROM settings WHERE key='removed_defaults_v04'")
    con.execute("INSERT INTO shops(key,name,region,domain,search_url) VALUES('amazon_de','Amazon.de','EU','amazon.de','https://www.amazon.de/s?k={q}')")
    con.execute("INSERT INTO shops(key,name,region,domain,search_url) VALUES('mediamarkt_gr','MM','GR','mediamarkt.gr','x{q}')")
    con.commit()
    con.close()
    pid = Database(tmp_path / "db.sqlite").create_product("P")
    db2 = Database(tmp_path / "db.sqlite")
    db2.add_listing(pid, "https://www.amazon.de/dp/B0")
    assert not any(s["key"] == "amazon_de" for s in db2.list_shops())  # αχρησιμοποίητο → σβήστηκε
    s = db2.shop_by_domain("amazon.de")
    assert s and s["key"].startswith("custom_")  # ξαναδημιουργήθηκε μόνο για το link του χρήστη
    assert not db2.shop_by_domain("mediamarkt.gr")
