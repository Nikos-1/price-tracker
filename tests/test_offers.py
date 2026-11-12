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
    assert [h["price"] for h in db.offers(lid, "Κατάστημα Α")] == [180.0, 170.0]


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
    assert db.offers(lid) == []


def test_normal_listing_has_no_offers(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    pid = db.create_product("X")
    lid = db.add_listing(pid, "https://www.plaisio.gr/p/1")
    db.record_check(lid, 10.0, "EUR", True)
    assert db.latest_offers(lid) == [] and db.offers(lid) == []


# ---------------------------------------------------------------- ανάγνωση σελίδων
from pricetracker.extract import extract  # noqa: E402


def _sk_card(sid, price, name, stock="3 τεμάχια", cid=None, extra=""):
    st = f'<div class="merchant-box-bottom-content"><span class="stock">{stock}</span></div>' if stock else ""
    return f"""<li class="product-card-redesigned" data-shop-id="{sid}" data-raw-price="{price}"
      data-price="{str(price).replace('.', ',')} €" data-product-url="/products/show/{cid or sid}">
      <div class="final-price"><span class="integer-part">{int(price)}</span><span class="currency">€</span></div>
      <div class="product-card-delivery-box">Παράδοση έως Τετ, 18 Νοε</div>{extra}
      <button class="add-to-cart-btn icon" type="button">Προσθήκη στο καλάθι</button>
      <div class="merchant-box"><span class="merchant-logo"><img alt="{name}" src="x.jpg"></span></div>{st}</li>"""


SKROUTZ = f"""<html><head><script type="application/ld+json">{{"@context":"https://schema.org","@type":"Product",
  "name":"Topdon V3000","offers":{{"@type":"AggregateOffer","lowPrice":"163.98","priceCurrency":"EUR",
  "offerCount":3}}}}</script></head><body>
  <ul class="scroll-area featured-product-card-redesigned">{_sk_card(781, 172.99, "Pancarshop")}</ul>
  <ul>{_sk_card(24573, 163.98, "Automoto-Eshop")}{_sk_card(781, 172.99, "Pancarshop")}
  {_sk_card(2413, 179.97, "E-kiriazis", stock=None)}
  {_sk_card(99, 150.0, "Φθηνό αλλά εξαντλημένο", stock=None, extra="<span>Εξαντλήθηκε</span>")}</ul></body></html>"""


def test_skroutz_offers_one_per_shop_and_cheapest_available():
    i = extract(SKROUTZ, "https://www.skroutz.gr/s/61712243/topdon-v3000.html")
    shops = {o["shop"]: o for o in i.offers}
    assert sorted(shops) == ["Automoto-Eshop", "E-kiriazis", "Pancarshop", "Φθηνό αλλά εξαντλημένο"]
    assert shops["E-kiriazis"]["in_stock"] is True and shops["Φθηνό αλλά εξαντλημένο"]["in_stock"] is False
    assert shops["Pancarshop"]["url"] == "https://www.skroutz.gr/products/show/781"
    assert i.price == 163.98 and i.in_stock is True and i.currency == "EUR"


def _bp_offer(oid, shop, price, avail="InStock"):
    return {"@type": "Offer", "url": "https://www.bestprice.gr/item/1/topdon-v3000.html",
            "seller": {"@type": "Organization", "name": shop, "url": f"https://www.bestprice.gr/m/1/{shop}.html"},
            "price": f"{price:.2f}", "priceCurrency": "EUR",
            "@id": f"https://www.bestprice.gr/item/1/topdon-v3000.html#offer-{oid}",
            "availability": f"https://schema.org/{avail}"}


def _bestprice(offers):
    import json
    ld = {"@context": "https://schema.org", "@type": "Product", "name": "Topdon V3000",
          "offers": {"@type": "AggregateOffer", "lowPrice": "160.00", "priceCurrency": "EUR",
                     "offerCount": len(offers), "offers": offers}}
    return f"""<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body>
      <a class="button" href="https://www.bestprice.gr/to/201941633/topdon.html?from=1">Στο κατάστημα</a>
      </body></html>"""


def test_bestprice_offers_from_jsonld_with_links_and_backorder():
    h = _bestprice([_bp_offer(222, "Kassandrinos", 160.0, "BackOrder"), _bp_offer(201941633, "Tool-Market", 167.4),
                    _bp_offer(333, "inzoom.gr", 281.0)])
    i = extract(h, "https://www.bestprice.gr/item/1/topdon-v3000.html")
    shops = {o["shop"]: o for o in i.offers}
    assert shops["Kassandrinos"]["in_stock"] is False  # κατόπιν παραγγελίας: όχι διαθέσιμο τώρα
    assert shops["Tool-Market"]["url"] == "https://www.bestprice.gr/to/201941633/topdon.html?from=1"
    assert shops["inzoom.gr"]["url"] == "https://www.bestprice.gr/item/1/topdon-v3000.html"
    assert i.price == 167.4 and i.in_stock is True  # η φθηνότερη ΔΙΑΘΕΣΙΜΗ, όχι το lowPrice


def test_single_seller_jsonld_is_not_a_comparison_list():
    import json
    ld = {"@type": "Product", "name": "X", "offers": {"@type": "Offer", "price": "10.00", "priceCurrency": "EUR",
                                                      "seller": {"name": "Το κατάστημά μου"},
                                                      "availability": "https://schema.org/InStock"}}
    i = extract(f'<script type="application/ld+json">{json.dumps(ld)}</script>', "https://shop.gr/p")
    assert i.offers == [] and i.price == 10.0


def test_all_offers_out_of_stock():
    h = _bestprice([_bp_offer(1, "Α", 100.0, "OutOfStock"), _bp_offer(2, "Β", 90.0, "OutOfStock")])
    i = extract(h, "https://www.bestprice.gr/item/1/x.html")
    assert i.in_stock is False and i.price == 90.0


# ---------------------------------------------------------------- ευρήματα ελέγχου κώδικα
def test_unknown_stock_beats_cheaper_sold_out():
    h = _bestprice([_bp_offer(1, "Α", 100.0, "OutOfStock"), _bp_offer(2, "Β", 120.0, "Weird")])
    h = h.replace("https://schema.org/Weird", "")  # Β: χωρίς διαθεσιμότητα
    i = extract(h, "https://www.bestprice.gr/item/1/x.html")
    assert i.price == 120.0 and i.in_stock is None


def test_old_offer_list_not_shown_after_check_without_offers(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    pid = db.create_product("X")
    lid = db.add_listing(pid, "https://www.bestprice.gr/item/1/x.html")
    db.record_check(lid, 10.0, "EUR", True, offers=[{**A, "price_eur": 180.0}, {**B, "price_eur": 175.0}])
    assert len(db.latest_offers(lid)) == 2
    db.record_check(lid, None, None, None, error="Δεν βρέθηκε τιμή")  # σφάλμα: η λίστα μένει
    assert len(db.latest_offers(lid)) == 2
    db.record_check(lid, 10.0, "EUR", True)  # επιτυχημένος έλεγχος χωρίς λίστα: δεν δείχνεται η παλιά
    assert db.latest_offers(lid) == []


def test_shop_name_case_does_not_split_history(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    pid = db.create_product("X")
    lid = db.add_listing(pid, "https://www.bestprice.gr/item/1/x.html")
    db.record_check(lid, 1.0, "EUR", True, offers=[{"shop": "PUBLIC", "price": 12.0, "price_eur": 12.0, "in_stock": True}])
    db.record_check(lid, 1.0, "EUR", True, offers=[{"shop": "Public", "price": 10.0, "price_eur": 10.0, "in_stock": True}])
    o = db.latest_offers(lid)[0]
    assert o["prev_price_eur"] == 12.0 and o["min_price_eur"] == 10.0
    assert len(db.offers(lid, "public")) == 2


def test_non_http_offer_links_dropped_and_marketplaces_ignored():
    import json
    offers = [_bp_offer(1, "Α", 10.0), _bp_offer(2, "Β", 11.0)]
    offers[0]["url"] = "javascript:alert(1)"
    i = extract(_bestprice(offers).replace("https://www.bestprice.gr/to/201941633/topdon.html?from=1", "#"),
                "https://www.bestprice.gr/item/1/x.html")
    assert {o["shop"]: o["url"] for o in i.offers}["Α"] is None
    # Ένα marketplace με πολλούς πωλητές στο JSON-LD κρατά τη δική του τιμή και δεν βγάζει λίστα
    m = extract(_bestprice(offers), "https://www.public.gr/product/1")
    assert m.offers == [] and m.price_source != "offers"


def test_greek_case_shop_history_and_route_without_shop(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    pid = db.create_product("X")
    lid = db.add_listing(pid, "https://www.bestprice.gr/item/1/x.html")
    db.record_check(lid, 1.0, "EUR", True, offers=[{"shop": "ΚΑΤΑΣΤΗΜΑ Α", "price": 12.0, "price_eur": 12.0, "in_stock": True}])
    db.record_check(lid, 1.0, "EUR", True, offers=[{"shop": "Κατάστημα Α", "price": 10.0, "price_eur": 10.0, "in_stock": True}])
    assert db.latest_offers(lid)[0]["prev_price_eur"] == 12.0
    from pricetracker.web import create_app
    c = create_app(db, Worker(db, tmp_path), tmp_path).test_client()
    assert len(c.get(f"/api/listings/{lid}/offers").get_json()) == 2


def test_skroutz_backorder_card_is_not_available():
    h = SKROUTZ.replace('<span class="stock">3 τεμάχια</span>', '<span class="stock">Κατόπιν παραγγελίας</span>', 1)
    i = extract(h, "https://www.skroutz.gr/s/1/x.html")
    # η πρώτη κάρτα (Pancarshop, προβεβλημένη) είναι κατόπιν παραγγελίας· η δεύτερη ίδια κάρτα όχι → κρατά τη διαθέσιμη
    assert {o["shop"]: o["in_stock"] for o in i.offers}["Pancarshop"] is True
    only = SKROUTZ.replace('<span class="stock">3 τεμάχια</span>', '<span class="stock">Κατόπιν παραγγελίας</span>')
    j = extract(only, "https://www.skroutz.gr/s/1/x.html")
    assert {o["shop"]: o["in_stock"] for o in j.offers}["Automoto-Eshop"] is False


def test_duplicate_seller_keeps_available_cheapest_and_unknown_value_is_unknown():
    offers = [_bp_offer(1, "Shop X", 200.0, "OutOfStock"), _bp_offer(2, "Shop X", 150.0), _bp_offer(3, "Y", 99.0)]
    offers[2]["availability"] = "Διαθέσιμο κάπως"
    i = extract(_bestprice(offers), "https://www.bestprice.gr/item/1/x.html")
    shops = {o["shop"]: o for o in i.offers}
    assert shops["Shop X"]["price"] == 150.0 and shops["Shop X"]["in_stock"] is True
    assert shops["Y"]["in_stock"] is True  # «διαθέσιμο» στο κείμενο → διαθέσιμο, όχι εξαντλημένο


def test_excel_offers_sheet_name_not_taken_by_product(tmp_path, monkeypatch):
    db, pid, lid = _check(tmp_path, monkeypatch, [[A, B]])
    db.create_product("Skroutz-BestPrice")
    from openpyxl import load_workbook
    from pricetracker.export import export_xlsx
    wb = load_workbook(export_xlsx(db, tmp_path, tmp_path / "x.xlsx"))
    assert wb["Skroutz-BestPrice"].cell(1, 4).value == "Κατάστημα"
