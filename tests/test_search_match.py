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


def test_build_queries_short_model_first():
    assert build_queries(LIAN)[0] == LIAN
    assert build_queries(TOPDON, brand="Topdon")[0] == "Topdon V3000"
    assert build_queries("Τροφοδοτικό Corsair RM850x 850W")[0] == "Corsair RM850x"
    assert build_queries("Samsung Galaxy S24 Ultra 256GB", brand="Samsung")[0] == "Samsung Galaxy S24 Ultra"


def _page(canonical, body):
    return (f'<html><head><link rel="canonical" href="{canonical}"><meta property="og:url" content="{canonical}">'
            f"</head><body>{body}{'x' * 600}</body></html>")


def test_search_that_returns_home_is_detected():
    home = _page("https://www.you.gr/", "<a>lian li</a> <a>lian li</a> <a>samsung</a>")
    assert returns_home(home, "https://www.you.gr/?s=lian+li&post_type=product", "https://www.you.gr/")
    assert not looks_like_search_results(home, "https://www.you.gr/?s=lian+li", "lian li", "https://www.you.gr/")
    res = _page("https://www.you.gr/search?q=lian", "<a>lian li</a>" * 5)
    assert not returns_home(res, "https://www.you.gr/search?q=lian", "https://you.gr/")
    assert looks_like_search_results(res, "https://www.you.gr/search?q=lian", "lian li", "https://www.you.gr/", home)
    # Ίδιος αριθμός εμφανίσεων με την αρχική: δεν είναι αποτελέσματα
    same = _page("https://www.you.gr/x", "<a>lian li</a> <a>lian li</a>")
    assert not looks_like_search_results(same, "https://www.you.gr/x", "lian li", "https://www.you.gr/", home)


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
