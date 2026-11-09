import hashlib
import io
import json
import sqlite3
import threading
import zipfile
from datetime import date
from pathlib import Path

from flask import Flask, Response
from werkzeug.serving import make_server

from pricetracker import updater as upd
from pricetracker import warranty
from pricetracker.db import Database
from pricetracker.web import create_app
from pricetracker.worker import Worker


class FakeWorker:
    status = {"running": False}

    def enqueue(self, *a, **k):
        return True

    def pending(self):
        return 0


def test_warranty_math():
    assert warranty.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert warranty.add_months(date(2026, 11, 9), 24) == date(2028, 11, 9)
    w = warranty.warranty_info({"warranty_months": 24, "purchased_at": "2026-11-09"}, date(2028, 10, 1))
    assert w["warranty_end"] == "2028-11-09" and w["warranty_state"] == "soon"
    assert warranty.warranty_info({"warranty_months": None, "purchased_at": "2026-11-09"})["warranty_state"] == "none"
    assert warranty.months_label(24) == "2 έτη" and warranty.months_label(18) == "1 έτος και 6 μήνες"


def test_migration_from_v01(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category_id INTEGER,
        target_price REAL, notes TEXT, ean TEXT, mpn TEXT, brand TEXT, image_url TEXT,
        purchased INTEGER NOT NULL DEFAULT 0, purchased_at TEXT, purchased_price REAL, purchased_shop TEXT,
        created_at TEXT NOT NULL);
        INSERT INTO products(name, created_at) VALUES('Παλιό', '2026-11-01');""")
    c.commit()
    c.close()
    db = Database(path)
    db.update_product(1, warranty_months=24)
    assert db.get_product(1)["warranty_months"] == 24 and db.get_product(1)["receipt_id"] is None


def test_purchase_multiple_with_receipt(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    cat = db.create_category("PSU")
    a = db.create_product("Corsair RM850x", cat)
    b = db.create_product("Seasonic GX-750", cat)
    client = create_app(db, FakeWorker(), tmp_path).test_client()
    items = [{"product_id": a, "price": "129,90", "warranty_months": 24},
             {"product_id": b, "price": "99", "warranty_months": 36},
             {"name": "Καλώδιο τροφοδοσίας", "category_id": cat, "price": "9,90", "warranty_months": 12}]
    r = client.post("/api/purchase", content_type="multipart/form-data", data={
        "items": json.dumps(items), "date": "2026-11-27", "shop": "Plaisio",
        "receipt": (io.BytesIO(b"%PDF-1.4 fake"), "Απόδειξη Plaisio.pdf")})
    assert r.status_code == 200, r.json
    rid = r.json["receipt_id"]
    assert len(r.json["products"]) == 3
    pa, pb = db.get_product(a), db.get_product(b)
    assert pa["purchased"] == 1 and pa["purchased_price"] == 129.9 and pa["warranty_months"] == 24
    assert pb["warranty_months"] == 36 and pa["receipt_id"] == pb["receipt_id"] == rid
    rec = db.get_receipt(rid)
    assert (tmp_path / rec["file"]).read_bytes() == b"%PDF-1.4 fake"
    assert len(client.get(f"/api/receipts/{rid}").json["products"]) == 3
    w = client.get("/api/warranties").json
    assert {x["name"] for x in w} == {"Corsair RM850x", "Seasonic GX-750", "Καλώδιο τροφοδοσίας"}
    assert next(x for x in w if x["id"] == b)["warranty_end"] == "2029-11-27"
    # Λάθος τύπος αρχείου
    bad = client.post("/api/purchase", content_type="multipart/form-data", data={
        "items": json.dumps(items[:1]), "receipt": (io.BytesIO(b"x"), "virus.exe")})
    assert bad.status_code == 400
    # Αλλαγή εγγύησης και επαναφορά σε παρακολούθηση
    client.put(f"/api/products/{a}/warranty", json={"warranty_months": 60})
    assert db.get_product(a)["warranty_months"] == 60
    client.post(f"/api/products/{a}/purchased", json={"purchased": False})
    assert db.get_product(a)["purchased"] == 0 and db.get_product(a)["warranty_months"] is None


def test_attach_receipt_later(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    a = db.create_product("A")
    b = db.create_product("B")
    db.update_product(a, purchased=1, purchased_at="2026-01-10", warranty_months=24)
    db.update_product(b, purchased=1, purchased_at="2026-01-10")
    client = create_app(db, FakeWorker(), tmp_path).test_client()
    r = client.post(f"/api/products/{a}/receipt", content_type="multipart/form-data",
                    data={"receipt": (io.BytesIO(b"\x89PNG"), "receipt.png"), "also": [str(b)]})
    assert r.status_code == 200
    assert db.get_product(a)["receipt_id"] == db.get_product(b)["receipt_id"] == r.json["receipt_id"]


def test_warranty_reminder_once(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    sent = []
    w = Worker(db, tmp_path, notify_callback=sent.append)
    p = db.create_product("Κουζίνα")
    db.update_product(p, purchased=1, purchased_at="2024-12-01", warranty_months=24)
    assert w.check_warranties(today=date(2026, 11, 9))
    assert sent and "Κουζίνα" in sent[0]
    assert w.check_warranties(today=date(2026, 11, 10)) == []  # όχι δεύτερη φορά


def _release_server(zip_bytes: bytes, version: str, good_hash=True):
    app = Flask("rel")
    digest = hashlib.sha256(zip_bytes).hexdigest() if good_hash else "0" * 64

    @app.get("/latest")
    def latest():
        base = f"http://127.0.0.1:{srv.server_port}"
        return {"tag_name": f"v{version}", "body": "Νέα έκδοση",
                "assets": [{"name": upd.ASSET_NAME, "browser_download_url": base + "/zip"},
                           {"name": upd.ASSET_NAME + ".sha256", "browser_download_url": base + "/sha"}]}

    @app.get("/zip")
    def z():
        return Response(zip_bytes, mimetype="application/zip")

    @app.get("/sha")
    def sha():
        return f"{digest}  {upd.ASSET_NAME}"

    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_updater_check_download(tmp_path, monkeypatch):
    zb = _zip({"PriceTracker.exe": b"MZ", "_internal/x.dll": b"x"})
    srv = _release_server(zb, "9.9.9")
    monkeypatch.setattr(upd, "API_URL", f"http://127.0.0.1:{srv.server_port}/latest")
    db = Database(tmp_path / "db.sqlite")
    u = upd.Updater(db, tmp_path)
    assert u.check() is True and u.status()["latest"] == "9.9.9" and u.status()["available"]
    target = u.download(u._release)
    assert (target / "PriceTracker.exe").read_bytes() == b"MZ" and (target / "_internal" / "x.dll").exists()
    # Όχι αυτόματη εγκατάσταση όταν δεν τρέχει ως .exe
    u.check_and_maybe_install()
    assert "PriceTracker.exe" in u.status()["message"]
    srv.shutdown()


def test_updater_rejects_bad_hash_and_old_version(tmp_path, monkeypatch):
    zb = _zip({"PriceTracker.exe": b"MZ"})
    srv = _release_server(zb, "9.9.9", good_hash=False)
    monkeypatch.setattr(upd, "API_URL", f"http://127.0.0.1:{srv.server_port}/latest")
    u = upd.Updater(Database(tmp_path / "db.sqlite"), tmp_path)
    assert u.check()
    try:
        u.download(u._release)
        raise AssertionError("should fail")
    except RuntimeError as e:
        assert "ακεραιότητας" in str(e)
    srv.shutdown()
    srv2 = _release_server(zb, "0.0.1")
    monkeypatch.setattr(upd, "API_URL", f"http://127.0.0.1:{srv2.server_port}/latest")
    assert u.check() is False and not u.status()["available"]
    srv2.shutdown()
    assert upd.parse_version("v0.10.0") > upd.parse_version("0.9.5")
