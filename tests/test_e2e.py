"""Δοκιμή από άκρη σε άκρη: τοπικό «κατάστημα», πραγματικός browser, worker και API."""
import os
import threading
import time
from pathlib import Path

import pytest
from flask import Flask, send_from_directory
from werkzeug.serving import make_server

from pricetracker.db import Database
from pricetracker.web import create_app
from pricetracker.worker import Worker

FIX = Path(__file__).parent / "fixtures"
CHROME = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"

pytestmark = pytest.mark.skipif(not (os.environ.get("PRICETRACKER_BROWSER") or Path(CHROME).exists()),
                                reason="no browser available")


@pytest.fixture(scope="module")
def shop():
    os.environ.setdefault("PRICETRACKER_BROWSER", CHROME)
    app = Flask("shop")

    @app.get("/p/<path:x>")
    def product(x):
        return send_from_directory(FIX, "shop_jsonld.html")

    @app.get("/oos")
    def oos():
        return send_from_directory(FIX, "shop_oos.html")

    @app.get("/search")
    def search():
        return send_from_directory(FIX, "generic_search.html")

    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def wait_idle(worker, timeout=90):
    t0 = time.time()
    time.sleep(0.3)
    while time.time() - t0 < timeout:
        if not worker.status["running"] and worker.pending() == 0:
            return
        time.sleep(0.3)
    raise TimeoutError("worker busy")


def test_full_flow(tmp_path, shop):
    db = Database(tmp_path / "db.sqlite")
    worker = Worker(db, tmp_path)
    worker.start(check_on_start=False)
    client = create_app(db, worker, tmp_path).test_client()

    cid = client.post("/api/categories", json={"name": "Τροφοδοτικά"}).json["id"]
    r = client.post("/api/products", json={"url": f"{shop}/p/1/corsair", "category_id": cid,
                                           "target_price": "150", "search": False})
    pid = r.json["id"]
    wait_idle(worker)
    p = client.get(f"/api/products/{pid}").json
    assert p["name"].startswith("Corsair RM850x"), p["name"]
    l = p["listings"][0]
    assert l["last_price"] == 139.9 and l["last_in_stock"] == 1, l
    assert l["last_screenshot"] and (tmp_path / l["last_screenshot"]).stat().st_size > 5000
    assert p["ean"] == "0840006640425"
    assert client.get("/shots/" + l["last_screenshot"]).status_code == 200

    # Αναζήτηση σε «άλλο κατάστημα» (localhost = διαφορετικό host από 127.0.0.1)
    port = shop.rsplit(":", 1)[1]
    db.create_shop("Local Shop", "EU", "localhost", f"http://localhost:{port}/search?q={{q}}", 1)
    for s_ in db.list_shops():
        if s_["domain"] != "localhost":
            db.update_shop(s_["id"], search_enabled=0)
    worker.search_product(pid)
    cands = client.get(f"/api/products/{pid}").json["candidates"]
    assert cands and cands[0]["url"] == f"http://localhost:{port}/p/123456/corsair-rm850x-2021-850w", cands
    client.post(f"/api/listings/{cands[0]['id']}/accept")
    wait_idle(worker)
    p = client.get(f"/api/products/{pid}").json
    assert len(p["listings"]) == 2 and p["best"]["EU"]["price"] == 139.9

    # Δεύτερο προϊόν, μη διαθέσιμο
    r2 = client.post("/api/products", json={"url": f"{shop}/oos", "search": False}).json["id"]
    wait_idle(worker)
    p2 = client.get(f"/api/products/{r2}").json
    assert p2["listings"][0]["last_in_stock"] == 0 and p2["listings"][0]["last_price"] == 109.0

    # Αγοράστηκε → δεν ελέγχεται πια
    client.post(f"/api/products/{r2}/purchased", json={"price": "105,50", "shop": "Test"})
    assert db.get_product(r2)["purchased"] == 1 and db.get_product(r2)["purchased_price"] == 105.5
    n_before = len(db.history(product_id=r2))
    client.post("/api/check")
    wait_idle(worker)
    assert len(db.history(product_id=r2)) == n_before

    # Export
    x = client.post("/api/export", json={})
    assert x.status_code == 200 and Path(x.json["path"]).exists()
    s = client.get("/api/status").json
    assert s["black_friday"].endswith("-11-27") or s["black_friday"]
    worker.stop()


def test_alerts_back_in_stock(tmp_path, shop):
    db = Database(tmp_path / "db.sqlite")
    sent = []
    worker = Worker(db, tmp_path, notify_callback=sent.append)
    pid = db.create_product("PSU", None, 145)
    lid = db.add_listing(pid, f"{shop}/p/2/x")
    db.record_check(lid, 160.0, "EUR", False)  # πριν: μη διαθέσιμο, ακριβό
    worker.check_listings(db.list_listings(pid), "test")
    assert sent and ("Διαθέσιμο" in sent[0] or "ειδοποιήσεις" in sent[0])


def test_detect_search_url(tmp_path):
    """Το κατάστημα δεν δηλώνει URL αναζήτησης: βρίσκεται από τη φόρμα της αρχικής σελίδας."""
    os.environ.setdefault("PRICETRACKER_BROWSER", CHROME)
    app = Flask("shop2")

    @app.get("/")
    def home():
        return ('<html><body><form id="nav-search" action="/anazitisi"><input type="search" name="kw">'
                '</form></body></html>')

    @app.get("/anazitisi")
    def results():
        from flask import request

        q = request.args.get("kw", "")
        return f"<html><title>Αναζήτηση: {q}</title><body><h1>Αποτελέσματα για {q}</h1>{'.' * 800}</body></html>"

    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        db = Database(tmp_path / "db.sqlite")
        sid = db.create_shop("Local", "GR", f"127.0.0.1:{srv.server_port}", "", 0)
        w = Worker(db, tmp_path)
        w.detect_search(sid)
        assert w.detect[sid]["state"] == "found", w.detect[sid]
        sh = db.get_shop(sid)
        assert sh["search_url"] == f"http://127.0.0.1:{srv.server_port}/anazitisi?kw={{q}}" and sh["search_enabled"] == 1
    finally:
        srv.shutdown()
