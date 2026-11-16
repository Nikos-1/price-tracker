"""Αναζήτηση στα καταστήματα: σωστή σύγκριση τίτλων (κωδικός μοντέλου, εκδόσεις) και εντοπισμός λάθος URL
αναζήτησης (σελίδα που ανοίγει την αρχική, όπως στο you.gr)."""
from pricetracker.browser import PageResult
from pricetracker.db import Database
from pricetracker.search import (build_queries, find_candidates, looks_like_search_results, model_codes,
                                 returns_home, score, tokens)
from pricetracker.web import create_app
from pricetracker.worker import Worker

LIAN = "LIAN-LI O11 DYNAMIC EVO XL"
TOPDON = "Topdon V3000 Φορητός Εκκινητής Μπαταρίας Αυτοκινήτου & Μοτοσυκλέτας 24000mAh με Φακό 12V"


def _s(title, cand, brand=None):
    return score(tokens(" ".join(filter(None, [brand, title]))), cand, [], model_codes(title), brand)


def test_model_code_beats_long_comparison_title():
    assert _s(TOPDON, "Εκκινητής - Booster Μπαταρίας TOPDON V3000", "Topdon") >= 0.8
    assert _s(TOPDON, "Topdon V2000", "Topdon") == 0  # άλλο μοντέλο της σειράς
    assert _s(TOPDON, "Topdon V3000 Pro", "Topdon") == 0  # άλλη έκδοση
    assert _s(TOPDON, "Θήκη για Topdon V3000", "Topdon") < 0.5  # αξεσουάρ
    assert _s("Corsair RM850x 850W", "Corsair RM850e 850W") == 0


def test_lian_li_variants():
    for ok in ("Lian Li O11 Dynamic EVO XL Black", "LIAN LI PC-O11 Dynamic EVO XL",
               "Κουτί Lian Li O11 Dynamic EVO XL Full Tower Μαύρο", "Lian Li O11D EVO XL Full Tower White"):
        assert _s(LIAN, ok) >= 0.8, ok
    for bad in ("Lian Li O11 Dynamic Mini", "Lian-Li O11 Vision", "Lian Li O11 Air Mini", "Lian Li O11 Dynamic EVO"):
        assert _s(LIAN, bad) < 0.5, bad


def test_review_regressions():
    assert _s("Corsair RM850x 850W", "Corsair RM850x 850W 80 Plus Gold") >= 0.8  # «Plus» δεν είναι έκδοση εδώ
    assert _s("Noctua NH-D15", "Noctua NH-D15 CPU Air Cooler") >= 0.8
    assert _s("Topdon V300", "Topdon V3000 Diagnostic", "Topdon") == 0  # V300 ≠ V3000
    assert _s("Anker PowerCore 20000mAh", "Anker PowerCore 10000mAh") < 0.5
    assert _s("Samsung Galaxy S24 256GB", "Samsung Galaxy S24 512GB") < 0.5
    assert _s("Samsung Galaxy S24 256GB", "Samsung Galaxy S24 Ultra 256GB") == 0
    assert _s("Κουτί Υπολογιστή Lian Li O11 Dynamic EVO XL Full Tower", "Lian Li O11 Dynamic EVO XL ATX Case Black") >= 0.5
    assert _s("Intel Core i5 13600K", "Intel Core i5-13600K Box") >= 0.8
    assert model_codes("Intel Core i5 13600K") == ["13600k"] and model_codes("APC Back-UPS 1500VA") == []
    assert build_queries("Lian Li O11 Dynamic EVO XL", brand="Lian Li")[0] == "Lian Li O11 Dynamic EVO XL"
    assert model_codes("AMD Ryzen 7 5800X3D") == ["5800x3d"]
    assert _s("AMD Ryzen 7 5800X3D", "AMD Ryzen 7 7800X3D") == 0 and _s("Intel Core i5 13600K", "Intel Core i5 14600K") == 0
    assert _s("Samsung Galaxy S24", "Samsung Galaxy S2 4GB") < 0.5
    assert _s("Corsair RM850x", "Corsair RM 850x 2021") >= 0.8
    assert build_queries("Τροφοδοτικό 850W RM850x")[0] == "RM850x"
    assert build_queries("") == []
    assert "(2x8GB)" not in build_queries("Kingston FURY Beast 16GB (2x8GB) DDR4 3200MHz", brand="Kingston")[0]


def test_build_queries_short_model_first():
    assert build_queries(LIAN)[0] == LIAN
    assert build_queries(TOPDON, brand="Topdon")[0] == "Topdon V3000"
    assert build_queries("Τροφοδοτικό Corsair RM850x 850W")[0] == "Corsair RM850x"
    assert build_queries("Samsung Galaxy S24 Ultra 256GB", brand="Samsung")[0] == "Samsung Galaxy S24 Ultra"


def _page(canonical, body, title="You.gr"):
    return (f'<html><head><title>{title}</title><link rel="canonical" href="{canonical}">'
            f"</head><body>{body}{'x' * 600}</body></html>")


def test_search_that_returns_home_is_detected():
    home = _page("https://www.you.gr/", "<a>lian li</a> <a>lian li</a> <a>samsung</a>")
    assert returns_home(home, "https://www.you.gr/?s=lian+li&post_type=product", "https://www.you.gr/")
    assert not looks_like_search_results(home, "https://www.you.gr/?s=lian+li", "lian li", "https://www.you.gr/")
    res = _page("https://www.you.gr/search?q=lian", "<a>lian li</a>" * 5)
    assert not returns_home(res, "https://www.you.gr/search?q=lian", "https://you.gr/")
    assert looks_like_search_results(res, "https://www.you.gr/search?q=lian", "lian li", "https://www.you.gr/")
    # Κανονική αναζήτηση WordPress («/?s=…») χωρίς canonical: δεν είναι η αρχική
    assert not returns_home("<html></html>", "https://shop.gr/?s=foo&post_type=product", "https://shop.gr/")
    # Ανακατεύθυνση στην αρχική μιας γλώσσας
    assert returns_home("<html></html>", "https://shop.gr/el/", "https://shop.gr/")
    # Το og:url ίδιο σε όλο το site δεν σημαίνει αρχική
    og = ('<html><head><meta property="og:url" content="https://www.you.gr/"></head><body>'
          + "<a>lian li</a>" * 3 + "x" * 600 + "</body></html>")
    assert not returns_home(og, "https://www.you.gr/search?q=lian", "https://www.you.gr/")


def _browser(pages):
    class Page:
        def close(self):
            pass

    class Br:
        headless = True

        def open(self, url, **k):
            html, final = pages(url)
            return Page(), PageResult(url, final, html, 200)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return Br()


def test_broken_search_url_shows_in_health_and_clears(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    shop = next(s for s in db.list_shops() if s["key"] == "you_gr")
    db.update_shop(shop["id"], search_url="https://www.you.gr/?s={q}&post_type=product", search_enabled=1)
    for s in db.list_shops():
        if s["id"] != shop["id"]:
            db.update_shop(s["id"], search_enabled=0)
    pid = db.create_product(LIAN)
    home = _page("https://www.you.gr/", "<a>Αρχική</a>")
    monkeypatch.setattr(w, "_browser", lambda: _browser(lambda url: (home, url)))
    assert w.search_product(pid) == []
    assert "you_gr" in db.get_settings()["search_broken"]

    class FW:
        status = {"running": False}
        health = {}

        def enqueue(self, *a, **k):
            return True

        def pending(self):
            return 0

    client = create_app(db, FW(), tmp_path).test_client()
    health = client.get("/api/status").get_json()["health"]
    assert any("You.gr" in h["text"] and "αρχική σελίδα" in h["text"] for h in health)
    # Άλλαξες το URL: η ειδοποίηση φεύγει αμέσως, πριν καν ξαναγίνει αναζήτηση
    db.update_shop(shop["id"], search_url="https://www.you.gr/search?q={q}")
    assert not any("αρχική σελίδα" in h["text"] for h in client.get("/api/status").get_json()["health"])
    # Και η επόμενη αναζήτηση που βρίσκει προϊόν το σβήνει από τις ρυθμίσεις
    res = ('<html><body><div class="product"><a href="/p/o11-evo-xl">Lian Li O11 Dynamic EVO XL Black</a>'
           '<span class="price">199,90 €</span></div>' + "x" * 600 + "</body></html>")
    monkeypatch.setattr(w, "_browser", lambda: _browser(lambda url: (res, url)))
    w.search_product(pid)
    assert "you_gr" not in db.get_settings()["search_broken"]


def test_find_candidates_skips_other_variants():
    html = ('<html><body>'
            '<div class="product"><a href="/p/1">Lian Li O11 Dynamic Mini Black</a></div>'
            '<div class="product"><a href="/p/2">Lian Li O11 Vision White</a></div>'
            '<div class="product"><a href="/p/3">Lian Li O11 Dynamic EVO XL Black</a></div>'
            "</body></html>")
    got = find_candidates(html, "https://shop.gr/search?q=o11", LIAN)
    assert [c["url"] for c in got] == ["https://shop.gr/p/3"]


def test_you_gr_search_url_fixed_on_upgrade(tmp_path):
    good = "https://www.you.gr/special-pages/searchq?query={q}"
    db = Database(tmp_path / "db.sqlite")
    sid = next(s["id"] for s in db.list_shops() if s["key"] == "you_gr")
    assert db.get_shop(sid)["search_url"] == good
    db.update_shop(sid, search_url="https://www.you.gr/?s={q}&post_type=product", search_enabled=1)
    db = Database(tmp_path / "db.sqlite")  # επόμενο άνοιγμα της εφαρμογής
    assert db.get_shop(sid)["search_url"] == good
    db.update_shop(sid, search_url="https://www.you.gr/my?x={q}")  # δικό του URL: δεν αλλάζει
    assert Database(tmp_path / "db.sqlite").get_shop(sid)["search_url"] == "https://www.you.gr/my?x={q}"


def test_you_gr_results_without_product_url_hints():
    html = ('<html><head><title>Αναζήτηση | You.gr</title></head><body>'
            '<a href="/anavathmisi-diktyaka/anavathmisi/koutia-ypologisti-pc-cases">Κουτιά Υπολογιστή PC Cases</a>'
            '<a href="/anavathmisi-diktyaka/anavathmisi/koutia-ypologisti-pc-cases/lian-li-pc-o11-dynamic-evo-xl-full-'
            'tower-black?query=LIAN-LI%20O11%20DYNAMIC%20EVO%20XL">Lian Li PC-O11 Dynamic EVO XL Full Tower Black</a>'
            '<a href="/anavathmisi-diktyaka/anavathmisi/koutia-ypologisti-pc-cases/lian-li-o11-dynamic-mini-black'
            '?query=x">Lian Li O11 Dynamic Mini Black</a>'
            "</body></html>")
    got = find_candidates(html, "https://www.you.gr/special-pages/searchq?query=LIAN-LI+O11", LIAN)
    assert [c["url"] for c in got] == ["https://www.you.gr/anavathmisi-diktyaka/anavathmisi/koutia-ypologisti-pc-"
                                       "cases/lian-li-pc-o11-dynamic-evo-xl-full-tower-black"]


def test_slug_fallback_rejects_brand_and_category_pages():
    from pricetracker.search import _drop_search_params, _slug_matches, _slug_words

    words = _slug_words(tokens(LIAN), None)
    assert not _slug_matches("https://x.gr/cases/lian-li-fans-rgb", [], words)
    assert not _slug_matches("https://x.gr/lian-li-o11-series", ["o11"], words)
    assert _slug_matches("https://x.gr/c/lian-li-o11-dynamic-evo-xl", ["o11"], words)
    words = _slug_words(tokens("Corsair Corsair Gold RM850x"), "Corsair")
    assert not _slug_matches("https://x.gr/corsair-gold-x", [], words)
    assert _drop_search_params("https://x.gr/p-a-b?id=1&query=foo&q=2") == "https://x.gr/p-a-b?id=1"


def test_failed_detection_keeps_search_url(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    sid = next(s["id"] for s in db.list_shops() if s["key"] == "you_gr")
    good = db.get_shop(sid)["search_url"]

    class Br:
        def open(self, url, **k):
            raise RuntimeError("offline")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(w, "_browser", lambda: Br())
    w.detect_search(sid, replace=True)
    assert db.get_shop(sid)["search_url"] == good  # η αποτυχία δεν σβήνει το URL
    # Κενό URL σε προεπιλεγμένο κατάστημα με γνωστό URL: μπαίνει κατευθείαν, χωρίς εύρεση
    db.update_shop(sid, search_url="", search_enabled=0)
    queued = []
    monkeypatch.setattr(w, "enqueue", lambda *a, **k: queued.append(k.get("shop_id")))
    w.detect_new_default_shops()
    assert db.get_shop(sid)["search_url"] == good and sid not in queued


def test_import_does_not_restore_bad_you_gr_url():
    from pricetracker.shops import fixed_search_url

    good = "https://www.you.gr/special-pages/searchq?query={q}"
    assert fixed_search_url("you.gr", "https://www.you.gr/?s={q}&post_type=product") == good
    assert fixed_search_url("you.gr", "") == good
    assert fixed_search_url("you.gr", "https://www.you.gr/x?q={q}") == "https://www.you.gr/x?q={q}"
    assert fixed_search_url("plaisio.gr", "") == ""


def test_detect_job_passes_replace_through_queue(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    got = []
    monkeypatch.setattr(w, "detect_search", lambda sid, replace=False: got.append((sid, replace)))
    w.enqueue("detect_search", shop_id=7, replace=True)
    w.q.put(None)
    w._loop()
    assert got == [(7, True)]


def test_found_url_without_replace_keeps_existing(tmp_path, monkeypatch):
    import pricetracker.worker as wm

    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    sid = db.create_shop("Shop", "GR", "shop-x.gr", "https://www.shop-x.gr/s?q={q}", 1)

    class Res:
        html, status, final_url = "<html></html>", 200, "https://www.shop-x.gr/"

    class Br:
        def open(self, url, **k):
            class P:
                def close(self):
                    pass
            return P(), Res()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(w, "_browser", lambda: Br())
    monkeypatch.setattr(wm, "search_url_candidates", lambda h, u: ["https://www.shop-x.gr/find?k={q}"])
    monkeypatch.setattr(wm, "looks_like_search_results", lambda *a: True)
    w.detect_search(sid, word="x")
    assert db.get_shop(sid)["search_url"] == "https://www.shop-x.gr/s?q={q}"
    assert w.detect[sid]["state"] == "kept"
    w.detect_search(sid, word="x", replace=True)
    assert db.get_shop(sid)["search_url"] == "https://www.shop-x.gr/find?k={q}"
    assert w.detect[sid]["state"] == "found"


def test_search_runs_shops_in_parallel_batches(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    w = Worker(db, tmp_path)
    keep = [s for s in db.list_shops() if s.get("search_url")][:5]
    for s in db.list_shops():
        db.update_shop(s["id"], search_enabled=1 if s in keep else 0)
    pid = db.create_product(LIAN)
    hit_host = keep[2]["domain"]
    batches = []
    hit = ('<html><body><a href="/p/123456">Lian Li O11 Dynamic EVO XL Black</a>' + "x" * 600 + "</body></html>")
    empty = "<html><body>" + "x" * 600 + "</body></html>"

    class Page:
        def close(self):
            pass

    class Br:
        def open_many(self, urls, stop=None):
            batches.append(urls)
            return [(Page(), PageResult(u, u, hit if hit_host in u else empty, 200)) for u in urls]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(w, "_browser", lambda: Br())
    w.search_product(pid)
    assert len(batches[0]) == 4  # 4 καταστήματα μαζί
    assert all(len(b) <= 4 for b in batches)
    # 2 αναζητήσεις στα 4 που δεν το έχουν, 1 σε αυτό που το βρήκε αμέσως
    assert sum(len(b) for b in batches) == 4 * 2 + 1
    assert any(hit_host in l["url"] for l in db.list_listings(product_id=pid, status="candidate"))
