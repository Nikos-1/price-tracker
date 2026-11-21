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


def test_delete_purchased_product_keeps_purchase(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    pid = db.create_product("PSU")
    lid = db.add_listing(pid, "https://www.plaisio.gr/p/1")
    shot = _shot(tmp_path, db, pid, lid, "a")
    rid = _buy(c, pid)
    receipt = tmp_path / db.get_receipt(rid)["file"]
    assert c.delete(f"/api/products/{pid}").status_code == 200
    p = db.get_product(pid)
    assert p and p["purchased"] == 1 and p["purchased_shop"] == "Plaisio" and p["purchased_at"] == "2026-11-27"
    assert p["receipt_id"] == rid and p["warranty_months"] == 24 and receipt.exists()
    assert db.list_listings(pid, status=None) == [] and db.history() == [] and not shot.exists()
    assert [w["id"] for w in c.get("/api/warranties").json] == [pid]


def test_warranties_filter_by_category(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    a, b = db.create_product("PSU"), db.create_product("Κουζίνα")
    _buy(c, a, also=[b])
    cat = c.post("/api/categories", json={"name": "Κουζίνες"}).json["id"]
    c.put(f"/api/products/{b}", json={"category_id": cat})
    assert [w["id"] for w in c.get(f"/api/warranties?category={cat}").json] == [b]
    assert [w["id"] for w in c.get("/api/warranties?category=0").json] == [a]
    assert len(c.get("/api/warranties?category=all").json) == 2


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
    c.post("/api/shops", json={"name": "MyShop", "domain": "myshop.gr", "region": "GR",
                               "search_url": "https://www.myshop.gr/search?q={q}"})
    r = c.post("/api/settings/export", json={"format": "json"}).json
    data = json.loads(Path(r["path"]).read_text(encoding="utf-8"))
    assert data["settings"]["check_interval_hours"] == 4 and data["settings"]["email_to"] == "me@example.com"
    assert "x" * 16 not in Path(r["path"]).read_text(encoding="utf-8")
    assert "smtp_password_plain" not in data["settings"]
    assert any(s["domain"] == "myshop.gr" for s in data["shops"])

    x = c.post("/api/settings/export", json={"format": "xlsx"}).json
    wb = load_workbook(x["path"])
    assert wb.sheetnames == ["Ρυθμίσεις", "Καταστήματα"]
    assert any(row[0].value == "MyShop" for row in wb["Καταστήματα"].iter_rows(min_row=2))
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
    assert db2.shop_by_domain("myshop.gr") and not db2.shop_by_domain("bad.gr")
    bad = c2.post("/api/settings/import", content_type="multipart/form-data",
                  data={"file": (io.BytesIO(b"{}"), "x.json")})
    assert bad.status_code == 400


def test_removed_default_shops(tmp_path):
    import sqlite3

    db = Database(tmp_path / "db.sqlite")
    keys = {s["key"] for s in db.list_shops()}
    assert not keys & {"mediamarkt_gr", "ebay_de"} and "amazon_de" in keys
    # Παλιά βάση (v0.3) με eBay σε χρήση και MediaMarkt αχρησιμοποίητο
    con = sqlite3.connect(tmp_path / "db.sqlite")
    con.execute("DELETE FROM settings WHERE key='removed_defaults_v04'")
    con.execute("INSERT INTO shops(key,name,region,domain,search_url) VALUES('ebay_de','eBay.de','EU','ebay.de','https://www.ebay.de/sch/i.html?_nkw={q}')")
    con.execute("INSERT INTO shops(key,name,region,domain,search_url) VALUES('mediamarkt_gr','MM','GR','mediamarkt.gr','x{q}')")
    con.commit()
    con.close()
    con = sqlite3.connect(tmp_path / "db.sqlite")
    ebay = con.execute("SELECT id FROM shops WHERE key='ebay_de'").fetchone()[0]
    con.execute("INSERT INTO products(name, created_at) VALUES('P', '2026-11-01')")
    con.execute("INSERT INTO listings(product_id, shop_id, url, created_at) VALUES(last_insert_rowid(), ?, 'https://www.ebay.de/itm/1', '2026-11-01')", (ebay,))
    con.commit()
    con.close()
    db2 = Database(tmp_path / "db.sqlite")
    e = db2.shop_by_domain("ebay.de")
    assert e and e["search_enabled"] == 0  # σε χρήση → μένει, αλλά όχι στην αναζήτηση
    assert not db2.shop_by_domain("mediamarkt.gr")


def test_bulk_delete_shops(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    pid = db.create_product("P")
    lid = db.add_listing(pid, "https://www.plaisio.gr/p/1")
    shot = _shot(tmp_path, db, pid, lid, "x")
    ids = [s["id"] for s in db.list_shops() if s["key"] in ("plaisio", "kotsovolos", "amazon_de")]
    r = c.post("/api/shops/delete", json={"ids": ids})
    assert r.status_code == 200 and r.json["deleted"] == 3
    keys = {s["key"] for s in db.list_shops()}
    assert not keys & {"plaisio", "kotsovolos", "amazon_de"} and "bestprice" in keys
    assert db.list_listings(pid) == [] and not shot.exists()


def test_search_url_candidates():
    from pricetracker.search import looks_like_search_results, search_url_candidates

    jsonld = """<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"WebSite",
      "url":"https://www.shop.gr/","potentialAction":{"@type":"SearchAction",
      "target":{"@type":"EntryPoint","urlTemplate":"https://www.shop.gr/search?term={search_term_string}"},
      "query-input":"required name=search_term_string"}}</script></head><body></body></html>"""
    c = search_url_candidates(jsonld, "https://www.shop.gr/")
    assert c[0] == "https://www.shop.gr/search?term={q}"

    form = """<html><body><form action="/en/zoeken" method="get" class="newsletter"><input name="email"></form>
      <form action="/el/anazitisi" class="search-form"><input type="hidden" name="lang" value="el">
      <input type="search" name="kw"></form></body></html>"""
    c = search_url_candidates(form, "https://shop.gr/")
    assert c[0] == "https://shop.gr/el/anazitisi?lang=el&kw={q}"
    assert "https://shop.gr/search?q={q}" in c  # και τα συνηθισμένα μοτίβα, για δοκιμή

    other = '<form action="https://tracker.example.com/s"><input type="search" name="q"></form>'
    assert not any("example.com" in x for x in search_url_candidates(other, "https://shop.gr/"))

    res = "<html><title>Αποτελέσματα για samsung</title><body>" + "x" * 600 + "samsung samsung</body></html>"
    assert looks_like_search_results(res, "https://shop.gr/search?q=samsung", "samsung", "https://shop.gr/")
    assert not looks_like_search_results(res, "https://shop.gr/", "samsung", "https://shop.gr/")


def test_add_shop_without_search_url_starts_detection(tmp_path):
    class W(FakeWorker):
        def __init__(self):
            self.detect, self.jobs = {}, []

        def enqueue(self, kind, **kw):
            self.jobs.append((kind, kw))
            return True

    db = Database(tmp_path / "db.sqlite")
    w = W()
    c = create_app(db, w, tmp_path).test_client()
    r = c.post("/api/shops", json={"name": "MyShop", "domain": "myshop.gr", "region": "GR"}).json
    assert r["detecting"] and w.jobs == [("detect_search", {"shop_id": r["id"]})]
    assert c.get(f"/api/shops/{r['id']}/detect").json["state"] == "queued"


def test_purchase_removes_search_data_keeps_purchase(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    pid = db.create_product("PSU")
    lid = db.add_listing(pid, "https://www.plaisio.gr/p/1")
    db.add_listing(pid, "https://www.public.gr/p/2", status="candidate")
    shot = _shot(tmp_path, db, pid, lid, "a")
    db.record_alert(lid, "drop", 90.0)
    rid = _buy(c, pid)
    p = db.get_product(pid)
    assert p["purchased"] == 1 and p["receipt_id"] == rid and p["warranty_months"] == 24
    assert db.list_listings(pid, status=None) == [] and db.history() == [] and not shot.exists()
    with db.conn() as con:
        assert con.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0] == 0
    assert (tmp_path / db.get_receipt(rid)["file"]).exists()


def test_delete_drops_queued_email_and_leaves_no_trace_in_db_file(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    pid = db.create_product("ZXQ Unique Laptop")
    keep = db.create_product("Other")
    lid = db.add_listing(pid, "https://www.plaisio.gr/p/1")
    lid2 = db.add_listing(keep, "https://www.plaisio.gr/p/2")
    shot = _shot(tmp_path, db, pid, lid, "a")
    other = _shot(tmp_path, db, keep, lid2, "b")
    db.set_settings({"email_outbox": [
        {"subject": "Πτώση", "text": "x", "attachments": [str(shot)], "ts": 1, "product_ids": [pid]},
        {"subject": "Και τα δύο", "text": "y", "attachments": [str(shot)], "ts": 2, "product_ids": [pid, keep]},
        {"subject": "Παλιό", "text": "w", "attachments": [str(shot), str(other)], "ts": 4},
        {"subject": "Εγγύηση", "text": "z", "attachments": [], "ts": 3}]})
    assert c.delete(f"/api/products/{pid}").status_code == 200
    box = db.get_settings()["email_outbox"]
    # μόνο γι' αυτό: δεν στέλνεται· και για άλλο προϊόν: στέλνεται χωρίς τα screenshots του
    assert [m["subject"] for m in box] == ["Και τα δύο", "Παλιό", "Εγγύηση"]
    assert box[0]["attachments"] == [] and box[1]["attachments"] == [str(other)]
    raw = b"".join(f.read_bytes() for f in tmp_path.glob("db.sqlite*"))
    assert "ZXQ Unique Laptop".encode() not in raw  # secure_delete: δεν μένει στο αρχείο της βάσης


def test_unbuy_drops_orphan_receipt_record_and_searches_again(tmp_path):
    class W(FakeWorker):
        jobs = []

        def enqueue(self, *a, **k):
            self.jobs.append((a, k))
            return True

    db = Database(tmp_path / "db.sqlite")
    w = W()
    c = create_app(db, w, tmp_path).test_client()
    pid = db.create_product("PSU")
    rid = _buy(c, pid)
    f = tmp_path / db.get_receipt(rid)["file"]
    assert c.post(f"/api/products/{pid}/purchased", json={"purchased": False}).status_code == 200
    assert db.get_receipt(rid) is None and f.exists()  # το αρχείο μένει στον φάκελο, όπως λέει το μήνυμα
    assert (("search_product",), {"product_id": pid}) in w.jobs


def test_replaced_receipt_is_removed(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    pid = db.create_product("PSU")
    r1 = _buy(c, pid, "a.pdf")
    f1 = tmp_path / db.get_receipt(r1)["file"]
    r = c.post(f"/api/products/{pid}/receipt", content_type="multipart/form-data",
               data={"receipt": (io.BytesIO(b"%PDF2"), "b.pdf")})
    assert r.status_code == 200 and r.json["receipt_id"] != r1
    assert db.get_receipt(r1) is None
    files = [x for x in f1.parent.iterdir()]  # μόνο η νέα απόδειξη (παίρνει το ίδιο όνομα)
    assert len(files) == 1 and files[0].read_bytes() == b"%PDF2"


def test_failed_multi_purchase_changes_nothing(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    a, b = db.create_product("A"), db.create_product("B")
    lid = db.add_listing(a, "https://www.plaisio.gr/p/1")
    db.record_check(lid, 100.0, "EUR", True)
    r = c.post("/api/purchase", json={"items": [{"product_id": a, "warranty_months": 24},
                                                 {"product_id": b, "warranty_months": 9999}]})
    assert r.status_code == 400
    assert db.get_product(a)["purchased"] == 0 and len(db.history()) == 1


def test_purchased_product_is_not_searched(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    pid = db.create_product("PSU")
    _buy(c, pid)
    assert c.post(f"/api/products/{pid}/search").status_code == 400
    from pricetracker.worker import Worker

    w = Worker(db, tmp_path)
    w.search_shops = lambda *a: (_ for _ in ()).throw(AssertionError("δεν έπρεπε να ψάξει"))
    assert w.search_product(pid) == []
