import sqlite3

from pricetracker.db import Database
from pricetracker.shops import DEFAULT_SHOPS, guess_country, region_of_country
from pricetracker.web import create_app
from pricetracker.worker import Worker


class FakeWorker:
    status = {"running": False}

    def enqueue(self, *a, **k):
        return True

    def pending(self):
        return 0


def test_upgrade_adds_new_shops_without_touching_existing(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    # Προσομοίωση βάσης v0.4: λίγα καταστήματα, ένα μετονομασμένο, ένα δικό του με domain νέου προεπιλεγμένου
    con = sqlite3.connect(tmp_path / "db.sqlite")
    new_keys = [s["key"] for s in DEFAULT_SHOPS if s["key"] not in ("bestprice", "plaisio", "amazon_de")]
    con.executemany("DELETE FROM shops WHERE key=?", [(k,) for k in new_keys])
    con.execute("UPDATE shops SET name='Το BestPrice μου', search_url='https://www.bestprice.gr/x?q={q}', country=NULL")
    con.execute("INSERT INTO shops(key,name,region,domain,search_url) VALUES('custom_germanos_gr','Germanos (δικό μου)','GR','germanos.gr','')")
    con.execute("INSERT INTO shops(key,name,region,domain,search_url) VALUES('custom_foo_de','Foo','EU','foo.de','')")
    con.commit()
    con.close()
    db2 = Database(tmp_path / "db.sqlite")
    shops = {s["key"]: s for s in db2.list_shops()}
    bp = shops["bestprice"]
    assert bp["name"] == "Το BestPrice μου" and bp["search_url"] == "https://www.bestprice.gr/x?q={q}"
    assert bp["country"] == "GR" and shops["amazon_de"]["country"] == "DE"
    assert shops["custom_foo_de"]["country"] == "DE"
    assert "germanos" not in shops and shops["custom_germanos_gr"]["name"] == "Germanos (δικό μου)"
    for k in ("amazon_uk", "amazon_us", "alternate_de", "electronet", "bhphoto"):
        assert k in shops
    assert shops["amazon_uk"]["region"] == "OTHER" and shops["amazon_us"]["country"] == "US"
    assert "mediamarkt_gr" not in shops and "ebay_de" not in shops


def test_countries():
    assert guess_country("amazon.co.uk") == "GB" and guess_country("x.de") == "DE" and guess_country("x.com") == ""
    assert region_of_country("GB") == "OTHER" and region_of_country("AT") == "EU" and region_of_country("GR") == "GR"


def test_shop_country_api(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    c = create_app(db, FakeWorker(), tmp_path).test_client()
    sid = c.post("/api/shops", json={"name": "Argos", "domain": "argos.co.uk",
                                     "search_url": "https://www.argos.co.uk/search/{q}/"}).json["id"]
    sh = db.get_shop(sid)
    assert sh["country"] == "GB" and sh["region"] == "OTHER"
    sid2 = c.post("/api/shops", json={"name": "Greek .com", "domain": "greekshop.com", "country": "GR"}).json["id"]
    assert db.get_shop(sid2)["region"] == "GR"
    c.put(f"/api/shops/{sid2}", json={"country": "US"})
    assert db.get_shop(sid2)["region"] == "OTHER"
    assert c.put(f"/api/shops/{sid2}", json={"country": "XX"}).status_code == 400
    lst = {s["id"]: s for s in c.get("/api/shops").json}
    assert lst[sid]["country_label"] == "Ηνωμένο Βασίλειο"


def test_research_all_notifies_new_shops(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    a, b = db.create_product("PSU"), db.create_product("Κουζίνα")
    db.update_product(b, purchased=1)
    got = []
    w = Worker(db, tmp_path, notify_callback=got.append)
    calls = []

    def fake_search(pid, label=None):
        calls.append(pid)
        return ["Alternate", "Scan"]

    w.search_product = fake_search
    w.research_all()
    assert calls == [a]  # τα αγορασμένα δεν ψάχνονται
    assert got and "PSU" in got[0]
    db.set_settings({"notify_new_shop": False})
    got.clear()
    w.research_all()
    assert got == []
