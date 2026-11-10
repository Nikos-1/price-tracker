"""v0.8.3: διορθώσεις από τον έλεγχο του κώδικα."""
from datetime import datetime

import pytest
from flask import Flask, Response
from werkzeug.serving import make_server

from pricetracker import analysis, notifier, productdirs
from pricetracker import updater as upd
from pricetracker.db import Database
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


def _shot(db, tmp_path, pid, folder_name, file="a.jpg"):
    lid = db.add_listing(pid, f"https://www.plaisio.gr/p/{pid}")
    folder = tmp_path / "Προϊόντα" / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / file).write_bytes(b"x")
    db.record_check(lid, 50.0, "EUR", True, screenshot=f"Προϊόντα/{folder_name}/{file}")
    return folder


# ---------------------------------------------------------------- διαγραφή φακέλων
def test_deleting_product_keeps_folder_of_other_product_with_same_name(tmp_path):
    db, c = _app(tmp_path)
    p1, p2 = db.create_product("TV"), db.create_product("TV")
    day = db.get_product(p1)["created_at"][:10]
    f1 = _shot(db, tmp_path, p1, f"{day} TV")
    f2 = _shot(db, tmp_path, p2, f"{day} TV ({p2})")
    assert c.delete(f"/api/products/{p2}").status_code == 200
    assert not f2.exists()
    assert (f1 / "a.jpg").exists()  # ο φάκελος του άλλου «TV» μένει
    # και ανάποδα
    p3 = db.create_product("TV")
    f3 = _shot(db, tmp_path, p3, f"{day} TV ({p3})")
    assert c.delete(f"/api/products/{p1}").status_code == 200
    assert not f1.exists() and (f3 / "a.jpg").exists()


def test_deleting_purchased_product_keeps_other_products_folder(tmp_path):
    db, c = _app(tmp_path)
    p1, p2 = db.create_product("Ψυγείο"), db.create_product("Ψυγείο")
    day = db.get_product(p1)["created_at"][:10]
    f1 = _shot(db, tmp_path, p1, f"{day} Ψυγείο")
    _shot(db, tmp_path, p2, f"{day} Ψυγείο ({p2})")
    db.update_product(p2, purchased=1)
    assert c.delete(f"/api/products/{p2}").status_code == 200
    assert (f1 / "a.jpg").exists()


def test_screenshot_never_lands_in_data_root_for_deleted_product(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    pid = db.create_product("X")
    p = db.get_product(pid)
    db.delete_product(pid)
    with pytest.raises(RuntimeError):
        productdirs.product_dir(db, tmp_path, p)


# ---------------------------------------------------------------- Black Friday
def test_bf_dates_greek_format_and_validation(tmp_path):
    db, c = _app(tmp_path)
    assert c.put("/api/settings", json={"bf_window_auto": False, "bf_start": "20/11", "bf_end": "2/12"}).status_code == 200
    s = db.get_settings()
    assert (s["bf_start"], s["bf_end"]) == ("11-20", "12-02")
    r = c.put("/api/settings", json={"bf_start": "20-11"})  # ΜΜ-ΗΗ: μήνας 20 δεν υπάρχει
    assert r.status_code == 400 and "Black Friday" in r.get_json()["error"]
    assert c.put("/api/settings", json={"bf_start": "30/2"}).status_code == 400
    assert db.get_settings()["bf_start"] == "11-20"


def test_invalid_bf_setting_never_stops_checks(tmp_path):
    db, c = _app(tmp_path)
    db.set_settings({"bf_window_auto": False, "bf_start": "χάλια", "bf_end": "20/11"})  # π.χ. από παλιά έκδοση
    assert analysis.bf_window(db.get_settings(), 2026) == (datetime(2026, 11, 20).date(), datetime(2026, 12, 2).date())
    assert c.get("/api/status").status_code == 200
    w = Worker(db, tmp_path)
    w.enqueue = lambda *a, **k: True
    w.check_warranties = lambda: None
    w._scheduler_tick(0, 0)  # δεν σκάει


def test_bf_window_across_new_year():
    s = {"bf_window_auto": False, "bf_start": "12-20", "bf_end": "01-05"}
    assert analysis.in_bf_window(s, datetime(2026, 12, 25))
    assert analysis.in_bf_window(s, datetime(2027, 1, 3))
    assert not analysis.in_bf_window(s, datetime(2027, 1, 10))
    assert analysis.in_bf_window({}, datetime(2026, 11, 27)) and not analysis.in_bf_window({}, datetime(2026, 11, 1))


# ---------------------------------------------------------------- διαγραφή κατά τον έλεγχο
def test_deleting_listing_during_check_keeps_other_alerts(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    pid = db.create_product("P")
    l1 = db.add_listing(pid, "https://a.gr/1")
    l2 = db.add_listing(pid, "https://b.gr/2")
    l3 = db.add_listing(pid, "https://c.gr/3")

    class Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(w, "_browser", lambda: Ctx())

    def check_one(br, l, product, settings):
        if l["id"] == l1:
            return [{"kind": "target", "product": "P"}]
        if l["id"] == l2:
            db.delete_listing(l2)  # ο χρήστης τη σβήνει ενώ ελέγχεται
            raise RuntimeError("FOREIGN KEY constraint failed")
        return [{"kind": "new_low", "product": "P"}]

    sent = []
    monkeypatch.setattr(w, "_check_one", check_one)
    monkeypatch.setattr(w, "_send_alerts", lambda alerts, settings: sent.extend(alerts))
    w.check_listings(db.list_listings(product_id=pid), "Έλεγχος")
    assert [a["kind"] for a in sent] == ["target", "new_low"]
    assert db.get_listing(l3)


# ---------------------------------------------------------------- τοπικός server
def test_local_server_rejects_foreign_pages(tmp_path):
    db, c = _app(tmp_path)
    assert c.get("/api/settings", headers={"Host": "evil.example:48231"}).status_code == 403  # DNS rebinding
    assert c.get("/api/settings").status_code == 200
    body = {"smtp_host": "smtp.evil.example"}
    assert c.put("/api/settings", json=body, headers={"X-PriceTracker": ""}).status_code == 403
    assert c.put("/api/settings", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.put("/api/settings", json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/api/settings/test-email", headers={"X-PriceTracker": ""}).status_code == 403
    assert db.get_settings()["smtp_host"] == "smtp.gmail.com"
    ok = c.put("/api/settings", json={"check_interval_hours": 6},
               headers={"Origin": "http://127.0.0.1:48231", "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200


def test_shots_serves_only_screenshots(tmp_path):
    db, c = _app(tmp_path)
    pid = db.create_product("TV")
    day = db.get_product(pid)["created_at"][:10]
    _shot(db, tmp_path, pid, f"{day} TV")
    assert c.get(f"/shots/Προϊόντα/{day} TV/a.jpg").status_code == 200
    for bad in ("db.sqlite", "Ρυθμίσεις.json", "../etc/passwd", f"Προϊόντα/{day} TV"):
        assert c.get(f"/shots/{bad}").status_code == 404


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, svc, user, pw):
        self.store[(svc, user)] = pw

    def get_password(self, svc, user):
        return self.store.get((svc, user))


def test_password_bound_to_smtp_server_and_never_plaintext(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    kr = FakeKeyring()
    monkeypatch.setattr(notifier, "_keyring", lambda: kr)
    db.set_settings({"smtp_user": "me@gmail.com"})
    notifier.save_password(db, "me@gmail.com", "app-pass")
    assert notifier.load_password(db, "me@gmail.com") == "app-pass"
    assert not db.get_settings().get("smtp_password_plain")
    db.set_settings({"smtp_host": "smtp.evil.example"})  # π.χ. από εισαγωγή ρυθμίσεων
    assert notifier.load_password(db, "me@gmail.com") == ""
    db.set_settings({"smtp_host": "smtp.gmail.com"})
    assert notifier.load_password(db, "me@gmail.com") == "app-pass"
    # παλιός κωδικός σε απλό κείμενο: μεταφέρεται στο Credential Manager
    db.set_settings({"smtp_password_plain": "old", "smtp_user": "x@y.gr"})
    assert notifier.load_password(db, "x@y.gr") == "old"
    assert kr.store[(notifier.KEYRING_SERVICE, "x@y.gr")] == "old" and not db.get_settings()["smtp_password_plain"]
    # χωρίς Credential Manager: σφάλμα, όχι απλό κείμενο
    monkeypatch.setattr(notifier, "_keyring", lambda: None)
    with pytest.raises(RuntimeError):
        notifier.save_password(db, "me@gmail.com", "p2")
    assert not db.get_settings().get("smtp_password_plain")


# ---------------------------------------------------------------- ενημέρωση
def test_updater_refuses_release_without_hash(tmp_path, monkeypatch):
    app = Flask("rel")

    @app.get("/latest")
    def latest():
        base = f"http://127.0.0.1:{srv.server_port}"
        return {"tag_name": "v9.9.9", "body": "",
                "assets": [{"name": upd.ASSET_NAME, "browser_download_url": base + "/zip"}]}

    @app.get("/zip")
    def z():
        return Response(b"PK", mimetype="application/zip")

    srv = make_server("127.0.0.1", 0, app, threaded=True)
    import threading

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(upd, "API_URL", f"http://127.0.0.1:{srv.server_port}/latest")
    u = upd.Updater(Database(tmp_path / "db.sqlite"), tmp_path)
    assert u.check()
    with pytest.raises(RuntimeError, match="sha256"):
        u.download(u._release)
    srv.shutdown()
