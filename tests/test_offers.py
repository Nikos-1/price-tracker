"""Καταστήματα από σελίδες σύγκρισης τιμών (Skroutz, BestPrice): αποθήκευση, λίστα κάτω από τη γραμμή, Excel."""

from pricetracker.db import Database
from pricetracker.worker import Worker


def _check(tmp_path, monkeypatch, readings):
    db = Database(tmp_path / "db.sqlite")
    pid = db.create_product("Booster")
    lid = db.add_listing(pid, "https://www.skroutz.gr/s/1/booster.html")
    w = Worker(db, tmp_path)

    class Res:
        html, final_url, status = "", "https://www.skroutz.gr/s/1/booster.html", 200

    class Br:
        def open(self, url):
            return self, Res()

        def close(self):
            pass

        def screenshot(self, page, path, *a):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"jpg")

    it = iter(readings)

    def fake_extract(html, url):
        from pricetracker.extract import ProductInfo
        offers = next(it)
        live = [o for o in offers if o["in_stock"]]
        return ProductInfo(title="Booster", price=min(o["price"] for o in live) if live else None,
                           currency="EUR", in_stock=bool(live), offers=offers, price_source="skroutz")

    monkeypatch.setattr("pricetracker.worker.extract", fake_extract)
    for _ in readings:
        l = db.list_listings(product_id=pid, status=None)[0]
        w._check_one(Br(), l, db.get_product(pid), db.get_settings())
    return db, pid, lid


A = {"shop": "Κατάστημα Α", "url": "https://a.gr/p", "price": 180.0, "in_stock": True}
B = {"shop": "Κατάστημα Β", "url": "https://b.gr/p", "price": 175.0, "in_stock": False}
C = {"shop": "Κατάστημα Γ", "url": None, "price": 190.0, "in_stock": True}


def test_offers_saved_sorted_with_change_and_lowest(tmp_path, monkeypatch):
    db, pid, lid = _check(tmp_path, monkeypatch, [[A, B, C], [{**A, "price": 170.0}, B, C]])
    offers = db.latest_offers(lid)
    assert [o["shop"] for o in offers] == ["Κατάστημα Α", "Κατάστημα Γ", "Κατάστημα Β"]  # μη διαθέσιμο στο τέλος
    a = offers[0]
    assert a["price_eur"] == 170.0 and a["prev_price_eur"] == 180.0 and a["min_price_eur"] == 170.0
    assert db.get_listing(lid)["last_price"] == 170.0  # η γραμμή κρατά τη φθηνότερη διαθέσιμη
    assert [h["price"] for h in db.offer_history(lid, "Κατάστημα Α")] == [180.0, 170.0]


def test_offers_in_product_api_and_history_route(tmp_path, monkeypatch):
    db, pid, lid = _check(tmp_path, monkeypatch, [[A, B]])
    from pricetracker.web import create_app
    app = create_app(db, Worker(db, tmp_path), tmp_path)
    c = app.test_client()
    p = c.get(f"/api/products/{pid}").get_json()
    assert [o["shop"] for o in p["listings"][0]["offers"]] == ["Κατάστημα Α", "Κατάστημα Β"]
    h = c.get(f"/api/listings/{lid}/offers?shop=Κατάστημα Β").get_json()
    assert len(h) == 1 and h[0]["in_stock"] == 0


def test_offers_removed_with_listing_history_and_in_excel(tmp_path, monkeypatch):
    db, pid, lid = _check(tmp_path, monkeypatch, [[A, B]])
    from openpyxl import load_workbook
    from pricetracker.export import export_xlsx
    out = export_xlsx(db, tmp_path, tmp_path / "x.xlsx")
    ws = load_workbook(out)["Skroutz-BestPrice"]
    assert {r[3] for r in ws.iter_rows(min_row=2, values_only=True)} == {"Κατάστημα Α", "Κατάστημα Β"}
    db.clear_history(lid)
    assert db.latest_offers(lid) == []
    db.record_check(lid, 1.0, "EUR", True, offers=[A])
    db.delete_listing(lid)
    assert db.all_offers(lid) == []


def test_normal_listing_has_no_offers(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    pid = db.create_product("X")
    lid = db.add_listing(pid, "https://www.plaisio.gr/p/1")
    db.record_check(lid, 10.0, "EUR", True)
    assert db.latest_offers(lid) == [] and db.all_offers(lid) == []
