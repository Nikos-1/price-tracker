from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from pricetracker import analysis
from pricetracker.db import Database
from pricetracker.export import export_xlsx
from pricetracker.extract import extract, parse_price
from pricetracker.search import build_query, find_candidates

FIX = Path(__file__).parent / "fixtures"


def html(name):
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("text,expected", [
    ("1.234,56 €", 1234.56), ("€1,234.56", 1234.56), ("1 234,56", 1234.56), ("349", 349.0),
    ("12,50€", 12.5), ("99.99", 99.99), ("1.299 €", 1299.0), ("EUR 89,-", 89.0), ("από 45,90€", 45.9),
    ("349 € 2 τεμ", 349.0), ("0,00", None), ("", None), (None, None), (139.9, 139.9),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_extract_jsonld():
    i = extract(html("shop_jsonld.html"), "https://www.example.gr/p/1")
    assert i.price == 139.90 and i.currency == "EUR" and i.in_stock is True
    assert i.ean == "0840006640425" and i.mpn == "CP-9020200-EU" and i.brand == "Corsair"
    assert "RM850x" in i.title


def test_extract_meta_out_of_stock():
    i = extract(html("shop_oos.html"), "https://shop.gr/x")
    assert i.price == 109.0 and i.in_stock is False


def test_extract_amazon():
    i = extract(html("amazon_product.html"), "https://www.amazon.de/dp/B08R5MHCQZ")
    assert i.price == 124.99 and i.in_stock is True and i.mpn == "CP-9020200-EU"
    u = extract(html("amazon_unavailable.html"), "https://www.amazon.de/dp/B08R5MHCQZ")
    assert u.in_stock is False and u.price is None
    c = extract(html("amazon_captcha.html"), "https://www.amazon.de/dp/B08R5MHCQZ")
    assert c.blocked


def test_extract_ebay():
    i = extract(html("ebay_product.html"), "https://www.ebay.de/itm/123456789012")
    assert i.price == 119.0 and i.in_stock is True


def test_search_candidates_amazon():
    c = find_candidates(html("amazon_search.html"), "https://www.amazon.de/s?k=x",
                        "Corsair RM850x (2021) 850W Τροφοδοτικό", mpn="CP-9020200-EU", brand="Corsair")
    assert c[0]["url"] == "https://www.amazon.de/dp/B08R5MHCQZ"
    assert all("Kabel" not in x["title"] for x in c)
    assert all("RM750x" not in x["title"] for x in c)


def test_search_candidates_generic():
    c = find_candidates(html("generic_search.html"), "https://www.shop.gr/search?q=x",
                        "Corsair RM850x (2021) 850W Τροφοδοτικό")
    urls = [x["url"] for x in c]
    assert urls[0] == "https://www.shop.gr/p/123456/corsair-rm850x-2021-850w"
    assert not any("other-site" in u or "/category/" in u for u in urls)


def test_build_query():
    assert build_query("x", mpn="CP-9020200-EU", brand="Corsair") == "Corsair CP-9020200-EU"
    assert build_query("Corsair RM850x 2021 850W Gold Full Modular ATX extra words") == \
        "Corsair RM850x 2021 850W Gold Full Modular"


def test_black_friday_dates():
    assert analysis.black_friday(2026) == date(2026, 11, 27)
    assert analysis.cyber_monday(2026) == date(2026, 11, 30)
    assert analysis.black_friday(2025) == date(2025, 11, 28)
    s = {"bf_window_auto": True, "check_interval_hours": 6, "bf_interval_hours": 1}
    assert analysis.current_interval_hours(s, datetime(2026, 11, 10)) == 6
    assert analysis.current_interval_hours(s, datetime(2026, 11, 20, 8)) == 1
    assert analysis.current_interval_hours(s, datetime(2026, 12, 2, 23)) == 1
    assert analysis.current_interval_hours(s, datetime(2026, 12, 3)) == 6


def _hist(prices, start=datetime(2026, 10, 1)):
    return [{"checked_at": (start + timedelta(days=i)).isoformat(sep=" "), "price": p, "in_stock": 1}
            for i, p in enumerate(prices)]


def test_analysis_real_and_fake():
    real = analysis.analyze(_hist([100] * 40 + [80]))
    assert real["verdict"] == "real"
    # Τιμή φούσκωσε από 100 σε 130 τις τελευταίες 2 εβδομάδες και «έπεσε» στα 100
    fake = analysis.analyze(_hist([100] * 60 + [130] * 14 + [100]))
    assert fake["verdict"] == "fake"
    few = analysis.analyze(_hist([100, 90]))
    assert few["verdict"] == "few"
    assert analysis.analyze([])["verdict"] == "nodata"


def test_db_crud_and_export(tmp_path):
    db = Database(tmp_path / "t.db")
    cat = db.create_category("Τροφοδοτικά (PSU)")
    with pytest.raises(ValueError):
        db.create_category("Τροφοδοτικά (PSU)")
    pid = db.create_product("Corsair RM850x", cat, 120)
    l1 = db.add_listing(pid, "https://www.bestprice.gr/item/123/corsair.html")
    l2 = db.add_listing(pid, "https://www.amazon.de/dp/B08R5MHCQZ")
    l3 = db.add_listing(pid, "https://www.newshop.it/p/1")
    assert db.get_listing(l1)["region"] == "GR" and db.get_listing(l1)["shop_name"] == "BestPrice"
    assert db.get_listing(l2)["region"] == "EU"
    assert db.get_listing(l3)["region"] == "EU"  # άγνωστο .it κατάστημα → Ε.Ε.
    db.record_check(l1, 139.9, "EUR", True, title="Corsair RM850x", screenshot="screenshots/a.jpg")
    db.record_check(l2, 124.99, "EUR", True)
    db.record_check(l3, None, None, None, error="timeout")
    assert db.get_listing(l1)["last_price"] == 139.9
    assert db.get_listing(l3)["last_error"] == "timeout"
    out = export_xlsx(db, tmp_path, tmp_path / "out.xlsx")
    from openpyxl import load_workbook

    wb = load_workbook(out)
    assert wb.sheetnames[:2] == ["Σύνοψη", "Ιστορικό"]
    assert wb["Ιστορικό"].max_row == 4
    assert len(wb.sheetnames) == 3  # + φύλλο προϊόντος με γράφημα
    db.rename_category(cat, "PSU")
    db.delete_category(cat)
    assert db.get_product(pid)["category_id"] is None
    db.delete_product(pid)
    assert db.history() == []
