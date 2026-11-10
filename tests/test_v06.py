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


def test_documents_keep_only_settings_products_receipts(tmp_path):
    from pricetracker.app import move_internal_files

    docs, app = tmp_path / "Documents" / "PriceTracker", tmp_path / "Local" / "PriceTracker"
    downloads = tmp_path / "Downloads" / "PriceTracker"
    (docs / "Αποδείξεις").mkdir(parents=True)
    (docs / "browser-profile").mkdir()
    (docs / "browser-profile" / "x").write_text("1")
    (docs / "exports").mkdir()
    (docs / "exports" / "a.xlsx").write_text("x")
    (docs / "app.log").write_text("old")
    app.mkdir(parents=True)
    (app / "app.log").write_text("new")
    moved = move_internal_files(docs, app, downloads)
    assert {"browser-profile", "exports"} <= set(moved)
    assert sorted(p.name for p in docs.iterdir()) == ["Αποδείξεις"]
    assert (app / "app.log").read_text() == "new" and (downloads / "a.xlsx").exists()
    assert move_internal_files(docs, app, downloads) == []


def test_product_folder_dated_and_screenshot_only_first_and_on_drop(tmp_path, monkeypatch):
    from pricetracker import productdirs
    from pricetracker.worker import Worker

    db = Database(tmp_path / "db.sqlite")
    pid = db.create_product("Κουζίνα Pitsos")
    created = db.get_product(pid)["created_at"][:10]
    lid = db.add_listing(pid, "https://www.plaisio.gr/p/1")
    w = Worker(db, tmp_path)
    prices = iter([300.0, 300.0, 310.0, 289.0])
    shots = []

    class Info:
        blocked = None
        in_stock = True
        currency = "EUR"
        title = "Κουζίνα"
        ean = mpn = brand = image = None

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
        i.price = next(prices)
        return i

    monkeypatch.setattr("pricetracker.worker.extract", fake_extract)
    settings = db.get_settings()
    for _ in range(4):
        l = db.list_listings(product_id=pid, status=None)[0]
        w._check_one(Br(), l, db.get_product(pid), settings)
    assert len(shots) == 2  # 1η φορά (ημέρα προσθήκης) και όταν έπεσε 310 → 289
    folder = tmp_path / "Προϊόντα" / f"{created} Κουζίνα Pitsos"
    assert all(p.parent == folder for p in shots)
    # μετονομασία προϊόντος → μετονομάζεται και ο φάκελος, οι διαδρομές στη βάση ενημερώνονται
    db.update_product(pid, name="Κουζίνα Bosch")
    productdirs.sync(db, tmp_path, pid)
    new = tmp_path / "Προϊόντα" / f"{created} Κουζίνα Bosch"
    assert new.is_dir() and not folder.exists()
    for rel in db.product_screenshots(pid):
        assert (tmp_path / rel).exists() and rel.startswith(f"Προϊόντα/{created} Κουζίνα Bosch/")
    _ = lid


def test_old_screenshot_folders_are_moved(tmp_path):
    from pricetracker import productdirs

    db = Database(tmp_path / "db.sqlite")
    pid = db.create_product("Ρολόι Casio")
    lid = db.add_listing(pid, "https://www.plaisio.gr/p/2")
    old = tmp_path / "screenshots" / f"{pid}_rolo_casio"
    old.mkdir(parents=True)
    (old / "a.jpg").write_bytes(b"x")
    db.record_check(lid, 50.0, "EUR", True, screenshot=f"screenshots\\{pid}_rolo_casio\\a.jpg")
    productdirs.sync_all(db, tmp_path)
    rel = next(iter(db.product_screenshots(pid)))
    assert rel.startswith("Προϊόντα/") and (tmp_path / rel).exists()
    assert not (tmp_path / "screenshots").exists()


def test_settings_file_is_kept_up_to_date(tmp_path):
    db, c = _app(tmp_path)
    f = tmp_path / "Ρυθμίσεις.json"
    assert json.loads(f.read_text(encoding="utf-8"))["kind"] == "settings"
    c.post("/api/sectors", json={"name": "Κατοικίδια"})
    assert "Κατοικίδια" in json.loads(f.read_text(encoding="utf-8"))["sectors"]



def test_new_default_sectors_added_on_upgrade_but_not_deleted_ones(tmp_path):
    import sqlite3

    from pricetracker.shops import DEFAULT_SECTORS, DEFAULT_SECTORS_V06

    db = Database(tmp_path / "db.sqlite")
    assert {x["name"] for x in db.list_sectors()} == set(DEFAULT_SECTORS)
    # Προσομοίωση v0.6: μόνο οι 11 παλιές, μία σβησμένη από τον χρήστη, σημάδι 'true'
    con = sqlite3.connect(tmp_path / "db.sqlite")
    con.executemany("DELETE FROM sectors WHERE name=?", [(n,) for n in DEFAULT_SECTORS if n not in DEFAULT_SECTORS_V06])
    con.execute("DELETE FROM sectors WHERE name='Βιβλία'")
    con.execute("UPDATE settings SET value='true' WHERE key='sectors_seeded'")
    con.commit()
    con.close()
    names = {x["name"] for x in Database(tmp_path / "db.sqlite").list_sectors()}
    assert "Κινητά & τηλέφωνα" in names and "Επαγγελματικός εξοπλισμός" in names
    assert "Βιβλία" not in names and "Κατοικίδια" not in names
    db3 = Database(tmp_path / "db.sqlite")
    sid = next(x["id"] for x in db3.list_sectors() if x["name"] == "Gaming & κονσόλες")
    db3.delete_sector(sid)
    assert "Gaming & κονσόλες" not in {x["name"] for x in Database(tmp_path / "db.sqlite").list_sectors()}


def test_electronics_shops_get_new_related_sectors(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    names = {x["id"]: x["name"] for x in db.list_sectors()}
    plaisio = next(s for s in db.list_shops() if s["key"] == "plaisio")
    got = {names[i] for i in db.shop_sector_ids()[plaisio["id"]]}
    assert {"Κινητά & τηλέφωνα", "Τηλεοράσεις & ήχος", "Ηλεκτρικές συσκευές"} <= got
    skroutz = next(s for s in db.list_shops() if s["key"] == "skroutz")
    assert not db.shop_sector_ids().get(skroutz["id"])


def test_leftover_browser_cache_is_removed_even_if_read_only(tmp_path):
    import os
    import stat

    from pricetracker.app import move_internal_files

    docs, app = tmp_path / "docs", tmp_path / "app"
    for base in (docs, app):
        (base / "browser-profile" / "Default" / "Cache").mkdir(parents=True)
    f = docs / "browser-profile" / "Default" / "Cache" / "data_1"
    f.write_text("x")
    os.chmod(f, stat.S_IREAD)
    (docs / "webview").mkdir()
    (docs / "webview" / "x").write_text("1")
    moved = move_internal_files(docs, app)
    assert not (docs / "browser-profile").exists() and "browser-profile" in moved
    assert (app / "webview" / "x").exists() and not (docs / "webview").exists()


def test_deleting_product_removes_all_its_files_and_folders(tmp_path):
    import os
    import stat

    from pricetracker import productdirs

    db, c = _app(tmp_path)
    pid = db.create_product("Ρολόι Casio")
    other = db.create_product("Κουζίνα")
    created = db.get_product(pid)["created_at"][:10]
    lid = db.add_listing(pid, "https://www.plaisio.gr/p/9")
    folder = tmp_path / "Προϊόντα" / f"{created} Ρολόι Casio"
    folder.mkdir(parents=True)
    shot = folder / "a.jpg"
    shot.write_bytes(b"x")
    (folder / "χωρίς εγγραφή στη βάση.jpg").write_bytes(b"y")
    os.chmod(shot, stat.S_IREAD)
    db.record_check(lid, 50.0, "EUR", True, screenshot=f"Προϊόντα/{created} Ρολόι Casio/a.jpg")
    old = tmp_path / "screenshots" / f"{pid}_rologi"
    old.mkdir(parents=True)
    (old / "b.jpg").write_bytes(b"z")
    keep = tmp_path / "Προϊόντα" / f"{created} Κουζίνα"
    keep.mkdir()
    (keep / "k.jpg").write_bytes(b"k")
    assert c.delete(f"/api/products/{pid}").status_code == 200
    assert not folder.exists() and not old.exists() and not (tmp_path / "screenshots").exists()
    assert (keep / "k.jpg").exists()
    _ = (other, productdirs)
