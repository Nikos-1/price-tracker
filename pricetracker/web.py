"""Τοπικός web server (μόνο 127.0.0.1) που σερβίρει το περιβάλλον της εφαρμογής και το API."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from . import analysis, notifier
from .export import export_xlsx
from .shops import REGION_LABELS

STATIC = Path(__file__).parent / "static"


def _num(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    from .extract import parse_price

    return parse_price(v)


def create_app(db, worker, data_dir: Path, shell=None):
    """shell: αντικείμενο με hide()/quit()/show() για το παράθυρο (desktop)."""
    app = Flask(__name__, static_folder=None)
    app.config["JSON_AS_ASCII"] = False
    data_dir = Path(data_dir)

    def err(msg, code=400):
        return jsonify({"error": msg}), code

    @app.errorhandler(ValueError)
    def _value_error(e):
        return err(str(e))

    # ------------------------------------------------------------ static
    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC, name)

    @app.get("/shots/<path:name>")
    def shots(name):
        p = (data_dir / name).resolve()
        if data_dir.resolve() not in p.parents or not p.exists():
            abort(404)
        return send_file(p)

    # ------------------------------------------------------------ helpers
    def product_summary(p):
        listings = db.list_listings(product_id=p["id"], status="active")
        cand = db.list_listings(product_id=p["id"], status="candidate")
        best = {}
        for region in ("GR", "EU"):
            avail = [l for l in listings if l.get("region") == region and l.get("last_price")
                     and l.get("last_in_stock")]
            if avail:
                b = min(avail, key=lambda l: l["last_price"])
                best[region] = {"price": b["last_price"], "shop": b["shop_name"], "url": b["url"]}
        hist = db.history(product_id=p["id"])
        prices = [h["price"] for h in hist if h["price"]]
        last_checked = max((l["last_checked"] for l in listings if l.get("last_checked")), default=None)
        errors = sum(1 for l in listings if l.get("last_error"))
        return {**p, "best": best, "listing_count": len(listings), "candidate_count": len(cand),
                "min_ever": min(prices) if prices else None, "last_checked": last_checked,
                "error_count": errors, "any_in_stock": any(l.get("last_in_stock") for l in listings)}

    # ------------------------------------------------------------ status
    @app.get("/api/status")
    def status():
        s = dict(worker.status)
        s.pop("last_run_ts", None)
        settings = db.get_settings()
        s["bf_mode"] = analysis.in_bf_window(settings)
        s["interval_hours"] = analysis.current_interval_hours(settings)
        y = datetime.now().year
        s["black_friday"] = analysis.black_friday(y).isoformat()
        s["cyber_monday"] = analysis.cyber_monday(y).isoformat()
        s["pending"] = worker.pending()
        s["data_dir"] = str(data_dir)
        return jsonify(s)

    @app.post("/api/check")
    def check_now():
        body = request.get_json(silent=True) or {}
        if body.get("product_id"):
            worker.enqueue("check_product", product_id=int(body["product_id"]))
        else:
            worker.enqueue("check_all")
        return jsonify({"ok": True})

    # ------------------------------------------------------------ categories
    @app.get("/api/categories")
    def categories():
        return jsonify(db.list_categories())

    @app.post("/api/categories")
    def create_category():
        cid = db.create_category((request.get_json() or {}).get("name", ""))
        return jsonify({"id": cid})

    @app.put("/api/categories/<int:cid>")
    def rename_category(cid):
        db.rename_category(cid, (request.get_json() or {}).get("name", ""))
        return jsonify({"ok": True})

    @app.delete("/api/categories/<int:cid>")
    def delete_category(cid):
        db.delete_category(cid, delete_products=request.args.get("products") == "1")
        return jsonify({"ok": True})

    # ------------------------------------------------------------ products
    @app.get("/api/products")
    def products():
        cat = request.args.get("category")
        purchased = request.args.get("purchased")
        rows = db.list_products(
            category_id=int(cat) if cat not in (None, "", "all") else None,
            purchased=None if purchased in (None, "", "all") else purchased == "1",
        )
        return jsonify([product_summary(p) for p in rows])

    @app.post("/api/products")
    def create_product():
        b = request.get_json() or {}
        url = (b.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return err("Επικόλλησε ένα έγκυρο link προϊόντος (ξεκινά με https://)")
        pid = db.create_product(b.get("name") or "Νέο προϊόν", _cat(b.get("category_id")),
                                _num(b.get("target_price")), b.get("notes"))
        db.add_listing(pid, url)
        worker.enqueue("new_product", product_id=pid, search=bool(b.get("search", True)))
        return jsonify({"id": pid})

    def _cat(v):
        return int(v) if v not in (None, "", 0, "0") else None

    @app.get("/api/products/<int:pid>")
    def product(pid):
        p = db.get_product(pid)
        if not p:
            abort(404)
        listings = db.list_listings(product_id=pid, status="active")
        for l in listings:
            h = db.history(listing_id=l["id"])
            l["analysis"] = analysis.analyze(h)
            l["region_label"] = REGION_LABELS.get(l.get("region"), "")
        cands = db.list_listings(product_id=pid, status="candidate")
        for l in cands:
            l["region_label"] = REGION_LABELS.get(l.get("region"), "")
        y = datetime.now().year
        return jsonify({**product_summary(p), "listings": listings, "candidates": cands,
                        "history": db.history(product_id=pid),
                        "events": [{"date": analysis.black_friday(yy).isoformat(), "label": "Black Friday"}
                                   for yy in (y - 1, y)] +
                                  [{"date": analysis.cyber_monday(yy).isoformat(), "label": "Cyber Monday"}
                                   for yy in (y - 1, y)]})

    @app.put("/api/products/<int:pid>")
    def update_product(pid):
        b = request.get_json() or {}
        fields = {}
        if "name" in b:
            if not str(b["name"]).strip():
                return err("Το όνομα δεν μπορεί να είναι κενό")
            fields["name"] = str(b["name"]).strip()
        if "category_id" in b:
            fields["category_id"] = _cat(b["category_id"])
        if "target_price" in b:
            fields["target_price"] = _num(b["target_price"])
        if "notes" in b:
            fields["notes"] = b["notes"]
        db.update_product(pid, **fields)
        return jsonify({"ok": True})

    @app.post("/api/products/<int:pid>/purchased")
    def mark_purchased(pid):
        b = request.get_json() or {}
        if b.get("purchased", True):
            db.update_product(pid, purchased=1, purchased_at=b.get("date") or datetime.now().strftime("%Y-%m-%d"),
                              purchased_price=_num(b.get("price")), purchased_shop=b.get("shop"))
        else:
            db.update_product(pid, purchased=0, purchased_at=None, purchased_price=None, purchased_shop=None)
        return jsonify({"ok": True})

    @app.delete("/api/products/<int:pid>")
    def delete_product(pid):
        db.delete_product(pid)
        return jsonify({"ok": True})

    @app.post("/api/products/<int:pid>/search")
    def search_product(pid):
        worker.enqueue("search_product", product_id=pid)
        return jsonify({"ok": True})

    @app.post("/api/products/<int:pid>/listings")
    def add_listing(pid):
        url = ((request.get_json() or {}).get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return err("Μη έγκυρο link")
        lid = db.add_listing(pid, url)
        worker.enqueue("check_listing", listing_id=lid)
        return jsonify({"id": lid})

    # ------------------------------------------------------------ listings
    @app.post("/api/listings/<int:lid>/accept")
    def accept_listing(lid):
        db.set_listing_status(lid, "active")
        worker.enqueue("check_listing", listing_id=lid)
        return jsonify({"ok": True})

    @app.post("/api/listings/<int:lid>/reject")
    def reject_listing(lid):
        db.set_listing_status(lid, "rejected")
        return jsonify({"ok": True})

    @app.delete("/api/listings/<int:lid>")
    def delete_listing(lid):
        db.delete_listing(lid)
        return jsonify({"ok": True})

    # ------------------------------------------------------------ shops
    @app.get("/api/shops")
    def shops():
        return jsonify([{**s, "region_label": REGION_LABELS.get(s["region"], "")} for s in db.list_shops()])

    @app.put("/api/shops/<int:sid>")
    def update_shop(sid):
        b = request.get_json() or {}
        if "search_enabled" in b:
            b["search_enabled"] = 1 if b["search_enabled"] else 0
        if "region" in b and b["region"] not in REGION_LABELS:
            return err("Άγνωστη περιοχή")
        db.update_shop(sid, **b)
        return jsonify({"ok": True})

    @app.post("/api/shops")
    def create_shop():
        b = request.get_json() or {}
        if not b.get("domain") or not b.get("name"):
            return err("Χρειάζεται όνομα και domain (π.χ. example.gr)")
        sid = db.create_shop(b["name"], b.get("region", "GR"), b["domain"], b.get("search_url", ""),
                             1 if b.get("search_url") else 0)
        return jsonify({"id": sid})

    # ------------------------------------------------------------ settings
    @app.get("/api/settings")
    def get_settings():
        s = db.get_settings()
        s.pop("smtp_password_plain", None)
        s["has_password"] = notifier.has_password(db, s.get("smtp_user", ""))
        return jsonify(s)

    @app.put("/api/settings")
    def put_settings():
        b = request.get_json() or {}
        pw = b.pop("smtp_password", None)
        b.pop("has_password", None)
        b.pop("smtp_password_plain", None)
        for k in ("check_interval_hours", "bf_interval_hours"):
            if k in b:
                v = float(b[k])
                if not 0.25 <= v <= 168:
                    return err("Το διάστημα ελέγχου πρέπει να είναι από 0,25 έως 168 ώρες")
                b[k] = v
        if "smtp_port" in b:
            b["smtp_port"] = int(b["smtp_port"])
        db.set_settings(b)
        if pw:
            notifier.save_password(db, b.get("smtp_user") or db.get_settings().get("smtp_user", ""), pw)
        if "start_with_windows" in b and shell is not None:
            try:
                shell.set_autostart(bool(b["start_with_windows"]))
            except Exception as e:  # noqa: BLE001
                return err(f"Δεν ρυθμίστηκε η αυτόματη εκκίνηση: {e}")
        return jsonify({"ok": True})

    @app.post("/api/settings/test-email")
    def test_email():
        s = db.get_settings()
        try:
            notifier.send_email(s, notifier.load_password(db, s.get("smtp_user", "")),
                                "[Price Tracker] Δοκιμαστικό email",
                                "Αν διαβάζεις αυτό, οι ειδοποιήσεις email δουλεύουν.")
        except Exception as e:  # noqa: BLE001
            return err(f"Αποτυχία αποστολής: {e}")
        return jsonify({"ok": True})

    # ------------------------------------------------------------ export
    @app.get("/api/export.xlsx")
    def export():
        pid = request.args.get("product")
        name = f"price-tracker_{datetime.now():%Y%m%d_%H%M}.xlsx"
        out = data_dir / "exports" / name
        export_xlsx(db, data_dir, out, [int(pid)] if pid else None)
        return send_file(out, as_attachment=True, download_name=name)

    @app.post("/api/export")
    def export_save():
        """Αποθηκεύει το xlsx στον φάκελο exports και το ανοίγει (στην εφαρμογή desktop)."""
        pid = (request.get_json(silent=True) or {}).get("product_id")
        name = f"price-tracker_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        out = export_xlsx(db, data_dir, data_dir / "exports" / name, [int(pid)] if pid else None)
        opened = False
        if shell is not None:
            try:
                shell.open_path(out)
                opened = True
            except Exception:  # noqa: BLE001
                pass
        return jsonify({"path": str(out), "opened": opened})

    @app.post("/api/open-folder")
    def open_folder():
        if shell is not None:
            shell.open_path(data_dir / ((request.get_json() or {}).get("sub") or ""))
        return jsonify({"ok": True})

    # ------------------------------------------------------------ window
    @app.post("/api/window/close-choice")
    def close_choice():
        b = request.get_json() or {}
        choice = b.get("choice")
        if b.get("remember") and choice in ("tray", "exit"):
            db.set_settings({"close_action": choice})
        if shell is not None:
            if choice == "exit":
                shell.quit()
            elif choice == "tray":
                shell.hide()
        return jsonify({"ok": True})

    @app.post("/api/window/show")
    def show_window():
        if shell is not None:
            shell.show()
        return jsonify({"ok": True})

    return app
