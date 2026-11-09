import io
import json
import os
from pathlib import Path

import pytest

from pricetracker import receipts
from pricetracker.db import Database
from pricetracker.extract import extract
from pricetracker.web import create_app

FIX = Path(__file__).parent / "fixtures"


class FakeWorker:
    status = {"running": False}

    def enqueue(self, *a, **k):
        return True

    def pending(self):
        return 0


def test_shops_add_delete(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    r = c.post("/api/shops", json={"name": "MyShop", "domain": "https://www.myshop.gr/", "region": "GR",
                                   "search_url": "https://www.myshop.gr/search?q={q}"})
    assert r.status_code == 200, r.json
    sid = r.json["id"]
    sh = next(s for s in c.get("/api/shops").json if s["id"] == sid)
    assert sh["domain"] == "myshop.gr" and sh["search_enabled"] == 1 and sh["used"] == 0
    assert c.post("/api/shops", json={"name": "Dup", "domain": "myshop.gr"}).status_code == 400
    assert c.post("/api/shops", json={"name": "X", "domain": "x.gr", "search_url": "https://x.gr/s"}).status_code == 400
    # Διαγραφή καταστήματος σε χρήση: χρειάζεται επιβεβαίωση
    pid = db.create_product("P")
    db.add_listing(pid, "https://www.myshop.gr/p/1")
    assert c.delete(f"/api/shops/{sid}").status_code == 400
    assert c.delete(f"/api/shops/{sid}?listings=1").status_code == 200
    assert db.list_listings(pid) == []
    # Διαγραμμένο προεπιλεγμένο κατάστημα δεν ξαναεμφανίζεται μετά από επανεκκίνηση
    bp = next(s for s in db.list_shops() if s["key"] == "bestprice")
    c.delete(f"/api/shops/{bp['id']}")
    db2 = Database(tmp_path / "db.sqlite")
    assert not any(s["key"] == "bestprice" for s in db2.list_shops())


def test_receipt_naming_and_organize(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    a = db.create_product("Corsair RM850x")
    b = db.create_product('Κουζίνα Pitsos 60cm "inox"')
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    r = c.post("/api/purchase", content_type="multipart/form-data", data={
        "items": json.dumps([{"product_id": a, "warranty_months": 24}, {"product_id": b, "warranty_months": 24}]),
        "date": "2026-11-27", "shop": "Plaisio", "receipt": (io.BytesIO(b"%PDF"), "scan001.PDF")})
    rid = r.json["receipt_id"]
    rec = db.get_receipt(rid)
    assert rec["file"].startswith("Αποδείξεις/2026/2026-11-27 Plaisio - ")
    assert rec["file"].endswith(".pdf") and "Corsair RM850x" in rec["file"] and '"' not in rec["file"]
    assert (tmp_path / rec["file"]).read_bytes() == b"%PDF"
    # Μετονομασία προϊόντος → μετονομάζεται και το αρχείο
    c.put(f"/api/products/{a}", json={"name": "Corsair RM850x 2021"})
    rec2 = db.get_receipt(rid)
    assert "Corsair RM850x 2021" in rec2["file"] and (tmp_path / rec2["file"]).exists()
    assert not (tmp_path / rec["file"]).exists()


def test_old_receipts_are_moved(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    p = db.create_product("Seasonic GX-750")
    old = tmp_path / "receipts" / "2026" / "20261109_120000_scan.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"JPG")
    rid = db.create_receipt("receipts/2026/20261109_120000_scan.jpg", "scan.jpg", "Public", "2026-11-09")
    db.update_product(p, receipt_id=rid)
    create_app(db, FakeWorker(), tmp_path)  # στην εκκίνηση
    new = db.get_receipt(rid)["file"]
    assert new == "Αποδείξεις/2026/2026-11-09 Public - Seasonic GX-750.jpg"
    assert (tmp_path / new).read_bytes() == b"JPG" and not (tmp_path / "receipts").exists()


def test_long_names_are_trimmed():
    n = receipts.receipt_name({"date": "2026-01-01", "shop": "Shop"}, [f"Προϊόν {i} με μεγάλο όνομα" for i in range(20)], ".png")
    assert len(n) < 200 and n.endswith(".png") and "και άλλα (20)" in n


def test_challenge_detected():
    i = extract((FIX / "challenge.html").read_text(), "https://www.shop.gr/p/1")
    assert i.blocked and "captcha" in i.blocked


def test_browser_mode_auto_asks_user_only_when_blocked(tmp_path, monkeypatch):
    from pricetracker import worker as wmod

    db = Database(tmp_path / "db.sqlite")
    w = wmod.Worker(db, tmp_path)
    assert w.browser_mode() == "auto"

    class FakePage:
        def close(self):
            pass

    class FakeBrowser:
        headless = True
        restarted = []

        def open(self, url):
            from pricetracker.browser import PageResult

            return FakePage(), PageResult(url, url, (FIX / "challenge.html").read_text(), 200)

        def restart(self, headless):
            self.restarted.append(headless)

    asked = []
    monkeypatch.setattr(w, "_ask_user_to_unblock", lambda br, url, reason: asked.append(url) or None)
    pid = db.create_product("P")
    lid = db.add_listing(pid, "https://www.shop.gr/p/1")
    br = FakeBrowser()
    with pytest.raises(RuntimeError):
        w._check_one(br, db.get_listing(lid), db.get_product(pid), db.get_settings())
    assert asked == ["https://www.shop.gr/p/1"] and br.restarted == [True]
    # «Πάντα κρυφό»: δεν ενοχλεί τον χρήστη
    db.set_settings({"browser_mode": "hidden"})
    asked.clear()
    with pytest.raises(RuntimeError):
        w._check_one(FakeBrowser(), db.get_listing(lid), db.get_product(pid), db.get_settings())
    assert asked == []
