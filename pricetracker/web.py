"""Τοπικός web server (μόνο 127.0.0.1) που σερβίρει το περιβάλλον της εφαρμογής και το API."""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import json
import re

from . import analysis, fx, notifier, receipts, warranty
from . import __version__
from .db import DEFAULT_SETTINGS
from .export import export_settings_xlsx, export_xlsx
from .shops import COUNTRIES, CURRENCY_CHOICES, REGION_LABELS, country_label, guess_country, region_of_country

STATIC = Path(__file__).parent / "static"
RECEIPT_TYPES = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".bmp", ".tif", ".tiff"}
MAX_RECEIPT_BYTES = 30 * 1024 * 1024


def _num(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    from .extract import parse_price

    return parse_price(v)


def create_app(db, worker, data_dir: Path, shell=None, updater=None):
    """shell: αντικείμενο με hide()/quit()/show() για το παράθυρο (desktop)."""
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_RECEIPT_BYTES + 1024 * 1024
    app.config["JSON_AS_ASCII"] = False
    data_dir = Path(data_dir)
    receipts.organize_all(db, data_dir)  # παλιές αποδείξεις → Αποδείξεις\<έτος>\<ημ/νία> <κατάστημα> - <προϊόντα>

    def err(msg, code=400):
        return jsonify({"error": msg}), code

    def remove_files(rel_paths):
        """Σβήνει αρχεία (σχετικές διαδρομές) μέσα στον φάκελο δεδομένων και τους άδειους φακέλους τους."""
        root = data_dir.resolve()
        dirs = set()
        for rel in rel_paths:
            if not rel:
                continue
            p = (data_dir / rel).resolve()
            if root not in p.parents:
                continue
            try:
                p.unlink()
            except OSError:
                pass
            dirs.add(p.parent)
        for d in dirs:
            try:
                if d != root and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

    def purge_search_data(pid: int):
        """Σβήνει μόνο τα δεδομένα αναζήτησης: καταστήματα του προϊόντος, τιμές, ιστορικό και screenshots."""
        shots = db.product_screenshots(pid)
        for l in db.list_listings(product_id=pid, status=None):
            db.delete_listing(l["id"])  # το ιστορικό σβήνεται μαζί (ON DELETE CASCADE)
        remove_files(shots)
        shots_root = data_dir / "screenshots"
        if shots_root.is_dir():
            for d in shots_root.glob(f"{pid}_*"):
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)

    def purge_product(pid: int):
        """Διαγραφή προϊόντος. Αν έχει αγοραστεί, μένει στα αγορασμένα με αγορά, εγγύηση και απόδειξη,
        και σβήνονται μόνο τα δεδομένα αναζήτησης. Αλλιώς σβήνεται εντελώς."""
        p = db.get_product(pid)
        if not p:
            return
        if p.get("purchased"):
            purge_search_data(pid)
            return
        shots = db.product_screenshots(pid)
        receipt = db.get_receipt(p["receipt_id"]) if p.get("receipt_id") else None
        db.delete_product(pid)  # listings και price_history σβήνονται μαζί (ON DELETE CASCADE)
        remove_files(shots)
        shots_root = data_dir / "screenshots"
        if shots_root.is_dir():
            for d in shots_root.glob(f"{pid}_*"):
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
        if receipt and not db.receipt_products(receipt["id"]):
            db.delete_receipt(receipt["id"])
            remove_files([receipt["file"]])
        notified = db.get_settings().get("warranty_notified")
        if isinstance(notified, list) and any(str(k).split(":")[0] == str(pid) for k in notified):
            db.set_settings({"warranty_notified": [k for k in notified if str(k).split(":")[0] != str(pid)]})

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
    def fill_eur(rows, price_key, cur_key, eur_key, rates=None):
        """Συμπληρώνει την τιμή σε € όπου λείπει (παλιές εγγραφές), με τις τρέχουσες ισοτιμίες."""
        rates = rates if rates is not None else (db.get_settings().get("fx") or {})
        for r in rows:
            if r.get(eur_key) is None and r.get(price_key) is not None:
                r[eur_key] = fx.to_eur(r[price_key], r.get(cur_key), rates)
        return rows

    def product_summary(p):
        rates = db.get_settings().get("fx") or {}
        listings = fill_eur(db.list_listings(product_id=p["id"], status="active"),
                            "last_price", "last_currency", "last_price_eur", rates)
        cand = db.list_listings(product_id=p["id"], status="candidate")
        best = {}
        for region in ("GR", "EU", "OTHER"):
            # σύγκριση σε €, ώστε π.χ. $ και £ να συγκρίνονται σωστά με €
            avail = [l for l in listings if l.get("region") == region and l.get("last_price_eur")
                     and l.get("last_in_stock")]
            if avail:
                b = min(avail, key=lambda l: l["last_price_eur"])
                best[region] = {"price": b["last_price_eur"], "orig_price": b["last_price"],
                                "currency": b.get("last_currency") or "EUR", "shop": b["shop_name"], "url": b["url"],
                                "country": country_label(b.get("country"))}
        hist = fill_eur(db.history(product_id=p["id"]), "price", "currency", "price_eur", rates)
        prices = [h["price_eur"] for h in hist if h.get("price_eur")]
        last_checked = max((l["last_checked"] for l in listings if l.get("last_checked")), default=None)
        errors = sum(1 for l in listings if l.get("last_error"))
        need_currency = sum(1 for l in listings if l.get("last_price") is not None and not l.get("last_currency"))
        return {**p, "best": best, "listing_count": len(listings), "candidate_count": len(cand),
                "need_currency": need_currency,
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
        fxs = settings.get("fx") or {}
        s["fx_date"] = fxs.get("date")
        from . import __version__

        s["version"] = __version__
        s["update"] = updater.status() if updater is not None else {"supported": False}
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
        b = request.get_json() or {}
        if "name" in b:
            db.rename_category(cid, b.get("name", ""))
        if "sector_id" in b:
            db.set_category_sector(cid, _cat(b["sector_id"]))
        return jsonify({"ok": True})

    # ------------------------------------------------------------ γενικές κατηγορίες καταστημάτων
    @app.get("/api/sectors")
    def sectors():
        return jsonify(db.list_sectors())

    @app.post("/api/sectors")
    def create_sector():
        return jsonify({"id": db.create_sector((request.get_json() or {}).get("name", ""))})

    @app.put("/api/sectors/<int:sid>")
    def rename_sector(sid):
        db.rename_sector(sid, (request.get_json() or {}).get("name", ""))
        return jsonify({"ok": True})

    @app.delete("/api/sectors/<int:sid>")
    def delete_sector(sid):
        db.delete_sector(sid)
        return jsonify({"ok": True})

    @app.delete("/api/categories/<int:cid>")
    def delete_category(cid):
        for pid in db.delete_category(cid):
            purge_product(pid)
        return jsonify({"ok": True})

    @app.get("/api/categories/<int:cid>/usage")
    def category_usage(cid):
        return jsonify(db.category_usage(cid))

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
        cat = _cat(b.get("category_id"))
        pid = db.create_product(b.get("name") or "Νέο προϊόν", cat, _num(b.get("target_price")), b.get("notes"))
        if "sector_id" in b:
            apply_sector(pid, cat, b.get("sector_id"))
        db.add_listing(pid, url)
        worker.enqueue("new_product", product_id=pid, search=bool(b.get("search", True)))
        return jsonify({"id": pid})

    def apply_sector(pid, cat, sector_id):
        """Γενική κατηγορία προϊόντος. Αν η κατηγορία του δεν έχει ακόμα, τη «μαθαίνει» κι αυτή για την επόμενη φορά."""
        sid = _cat(sector_id)
        db.update_product(pid, sector_id=sid)
        if sid and cat:
            c = next((x for x in db.list_categories() if x["id"] == cat), None)
            if c and not c.get("sector_id"):
                db.set_category_sector(cat, sid)

    def _cat(v):
        return int(v) if v not in (None, "", 0, "0") else None

    @app.get("/api/products/<int:pid>")
    def product(pid):
        p = db.get_product(pid)
        if not p:
            abort(404)
        rates = db.get_settings().get("fx") or {}
        listings = fill_eur(db.list_listings(product_id=pid, status="active"),
                            "last_price", "last_currency", "last_price_eur", rates)
        for l in listings:
            h = fill_eur(db.history(listing_id=l["id"]), "price", "currency", "price_eur", rates)
            # η εκτίμηση προσφοράς γίνεται σε €, για να συγκρίνεται με τα υπόλοιπα καταστήματα
            l["analysis"] = analysis.analyze([{**x, "price": x.get("price_eur")} for x in h])
            l["region_label"] = REGION_LABELS.get(l.get("region"), "")
            l["country_label"] = country_label(l.get("country"))
        cands = db.list_listings(product_id=pid, status="candidate")
        for l in cands:
            l["region_label"] = REGION_LABELS.get(l.get("region"), "")
            l["country_label"] = country_label(l.get("country"))
        y = datetime.now().year
        return jsonify({**product_summary(p), "listings": listings, "candidates": cands,
                        "history": fill_eur(db.history(product_id=pid), "price", "currency", "price_eur"),
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
        if "sector_id" in b:
            cur = db.get_product(pid) or {}
            apply_sector(pid, fields.get("category_id", cur.get("category_id")), b["sector_id"])
        if "target_price" in b:
            fields["target_price"] = _num(b["target_price"])
        if "notes" in b:
            fields["notes"] = b["notes"]
        db.update_product(pid, **fields)
        p = db.get_product(pid)
        if "name" in fields and p and p.get("receipt_id"):
            receipts.organize(db, data_dir, p["receipt_id"])
        return jsonify({"ok": True})

    @app.post("/api/products/<int:pid>/purchased")
    def mark_purchased(pid):
        b = request.get_json() or {}
        if b.get("purchased", True):
            db.update_product(pid, purchased=1, purchased_at=b.get("date") or datetime.now().strftime("%Y-%m-%d"),
                              purchased_price=_num(b.get("price")), purchased_shop=b.get("shop"))
        else:
            db.update_product(pid, purchased=0, purchased_at=None, purchased_price=None, purchased_shop=None,
                              receipt_id=None, warranty_months=None)
        return jsonify({"ok": True})

    @app.delete("/api/products/<int:pid>")
    def delete_product(pid):
        purge_product(pid)
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

    @app.put("/api/listings/<int:lid>/currency")
    def set_listing_currency(lid):
        cur = ((request.get_json() or {}).get("currency") or "").upper()
        if cur not in CURRENCY_CHOICES:
            return err("Άγνωστο νόμισμα")
        if not db.get_listing(lid):
            abort(404)
        rates = fx.get_rates(db)
        db.set_listing_currency(lid, cur, lambda price, c: fx.to_eur(price, c, rates))
        return jsonify({"ok": True})

    @app.get("/api/currencies")
    def currencies():
        return jsonify(CURRENCY_CHOICES)

    @app.delete("/api/listings/<int:lid>")
    def delete_listing(lid):
        shots = db.listing_screenshots([lid])
        db.delete_listing(lid)
        remove_files(shots)
        return jsonify({"ok": True})

    # ------------------------------------------------------------ shops
    @app.get("/api/shops")
    def shops():
        sec = db.shop_sector_ids()
        return jsonify([{**s, "region_label": REGION_LABELS.get(s["region"], ""),
                         "country_label": country_label(s.get("country")), "used": db.shop_usage(s["id"]),
                         "sector_ids": sec.get(s["id"], [])}
                        for s in db.list_shops()])

    @app.post("/api/fx/refresh")
    def fx_refresh():
        f = fx.get_rates(db, force=True)
        if not f.get("rates"):
            return err("Δεν ήταν δυνατή η λήψη ισοτιμιών (έλεγξε τη σύνδεση στο internet)")
        return jsonify({"ok": True, "date": f.get("date"), "usd": f["rates"].get("USD"), "gbp": f["rates"].get("GBP")})

    @app.get("/api/countries")
    def countries():
        return jsonify([{"code": k, "name": v, "region": region_of_country(k)} for k, v in COUNTRIES.items()])

    @app.put("/api/shops/<int:sid>")
    def update_shop(sid):
        b = request.get_json() or {}
        if "sector_ids" in b:
            db.set_shop_sectors(sid, b.pop("sector_ids") or [])
        if "search_enabled" in b:
            b["search_enabled"] = 1 if b["search_enabled"] else 0
        if "country" in b:
            if b["country"] not in COUNTRIES:
                return err("Άγνωστη χώρα")
            b["region"] = region_of_country(b["country"])  # η περιοχή ακολουθεί τη χώρα
        if "region" in b and b["region"] not in REGION_LABELS:
            return err("Άγνωστη περιοχή")
        db.update_shop(sid, **b)
        return jsonify({"ok": True})

    @app.post("/api/shops")
    def create_shop():
        b = request.get_json() or {}
        name = (b.get("name") or "").strip()
        raw = (b.get("domain") or "").strip()
        if not name or not raw:
            return err("Χρειάζεται όνομα και διεύθυνση καταστήματος (π.χ. www.example.gr)")
        from .shops import host_of

        domain = host_of(raw if "://" in raw else "https://" + raw)
        if not domain or "." not in domain:
            return err("Μη έγκυρη διεύθυνση καταστήματος")
        if db.shop_by_domain(domain):
            return err(f"Υπάρχει ήδη κατάστημα για το {domain}")
        country = b.get("country")
        if country is None:
            country = guess_country(domain) or ("GR" if not b.get("region") or b.get("region") == "GR" else "")
        if country not in COUNTRIES:
            return err("Άγνωστη χώρα")
        region = region_of_country(country) if country else (b.get("region") or "OTHER")
        if region not in REGION_LABELS:
            return err("Άγνωστη περιοχή")
        url = (b.get("search_url") or "").strip()
        if url and "{q}" not in url:
            return err("Το URL αναζήτησης πρέπει να περιέχει {q} στη θέση του προϊόντος")
        sid = db.create_shop(name, region, domain, url, 1 if url else 0, country=country)
        if b.get("sector_ids"):
            db.set_shop_sectors(sid, b["sector_ids"])
        detecting = False
        if not url and hasattr(worker, "detect"):
            worker.detect[sid] = {"state": "queued", "message": "Σε αναμονή…"}
            detecting = worker.enqueue("detect_search", shop_id=sid)
        return jsonify({"id": sid, "detecting": detecting})

    @app.post("/api/shops/<int:sid>/detect")
    def detect_shop_search(sid):
        if not db.get_shop(sid):
            abort(404)
        if not hasattr(worker, "detect"):
            return err("Δεν είναι διαθέσιμο")
        db.update_shop(sid, search_url="", search_enabled=0)
        worker.detect[sid] = {"state": "queued", "message": "Σε αναμονή…"}
        worker.enqueue("detect_search", shop_id=sid)
        return jsonify({"ok": True})

    @app.get("/api/shops/<int:sid>/detect")
    def detect_shop_status(sid):
        sh = db.get_shop(sid)
        if not sh:
            abort(404)
        st = dict(getattr(worker, "detect", {}).get(sid) or {"state": "none"})
        st["search_url"] = sh.get("search_url") or ""
        st["search_enabled"] = sh.get("search_enabled")
        return jsonify(st)

    @app.post("/api/shops/delete")
    def delete_shops():
        """Μαζική διαγραφή: {"ids": [...]}. Σβήνει και τις τιμές/ιστορικό των καταστημάτων στα προϊόντα."""
        ids = [int(x) for x in (request.get_json() or {}).get("ids") or []]
        shots = db.listing_screenshots([lid for sid in ids for lid in db.shop_listing_ids(sid)])
        for sid in ids:
            db.delete_shop(sid, delete_listings=True)
        remove_files(shots)
        return jsonify({"ok": True, "deleted": len(ids)})

    @app.delete("/api/shops/<int:sid>")
    def delete_shop(sid):
        shots = db.listing_screenshots(db.shop_listing_ids(sid))
        db.delete_shop(sid, delete_listings=request.args.get("listings") == "1")
        remove_files(shots)
        return jsonify({"ok": True})

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
        b = clean_settings(b)
        db.set_settings(b)
        if pw:
            notifier.save_password(db, b.get("smtp_user") or db.get_settings().get("smtp_user", ""), pw)
        msg = apply_autostart(b)
        return err(msg) if msg else jsonify({"ok": True})

    def clean_settings(b: dict) -> dict:
        b = {k: v for k, v in b.items() if k in DEFAULT_SETTINGS}  # μόνο ρυθμίσεις του χρήστη
        for k in ("check_interval_hours", "bf_interval_hours"):
            if k in b:
                v = float(b[k])
                if not 0.25 <= v <= 168:
                    raise ValueError("Το διάστημα ελέγχου πρέπει να είναι από 0,25 έως 168 ώρες")
                b[k] = v
        for k in ("smtp_port", "warranty_reminder_days"):
            if k in b:
                b[k] = int(b[k] or 0)
        return b

    def apply_autostart(b: dict):
        if "start_with_windows" in b and shell is not None:
            try:
                shell.set_autostart(bool(b["start_with_windows"]))
            except Exception as e:  # noqa: BLE001
                return f"Δεν ρυθμίστηκε η αυτόματη εκκίνηση: {e}"
        return None

    def settings_payload() -> dict:
        s = db.get_settings()
        sec_names = {x["id"]: x["name"] for x in db.list_sectors()}
        shop_secs = db.shop_sector_ids()
        return {
            "app": "PriceTracker", "kind": "settings", "version": __version__,
            "exported_at": datetime.now().replace(microsecond=0).isoformat(),
            "settings": {k: s.get(k, v) for k, v in DEFAULT_SETTINGS.items()},
            "shops": [{"name": x["name"], "domain": x["domain"], "region": x["region"], "country": x.get("country") or "",
                       "search_url": x.get("search_url") or "", "search_enabled": bool(x.get("search_enabled")),
                       "sectors": [sec_names[i] for i in shop_secs.get(x["id"], []) if i in sec_names]}
                      for x in db.list_shops()],
            "sectors": sorted(sec_names.values()),
        }

    def save_export(kind: str) -> Path:
        stamp = f"{datetime.now():%Y-%m-%d_%H%M%S}"
        out_dir = data_dir / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        if kind == "xlsx":
            names = {x["id"]: x["name"] for x in db.list_sectors()}
            secs = db.shop_sector_ids()
            shops = [{**x, "used": db.shop_usage(x["id"]),
                      "sectors": ", ".join(names[i] for i in secs.get(x["id"], []) if i in names)}
                     for x in db.list_shops()]
            return export_settings_xlsx(db.get_settings(), shops, out_dir / f"Ρυθμίσεις και καταστήματα {stamp}.xlsx")
        out = out_dir / f"PriceTracker ρυθμίσεις {stamp}.json"
        out.write_text(json.dumps(settings_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    @app.get("/api/settings/export")
    def settings_export_download():
        kind = "xlsx" if request.args.get("format") == "xlsx" else "json"
        out = save_export(kind)
        return send_file(out, as_attachment=True, download_name=out.name)

    @app.post("/api/settings/export")
    def settings_export_save():
        """Αποθηκεύει στον φάκελο exports. Το xlsx ανοίγει· για το αρχείο ρυθμίσεων ανοίγει ο φάκελος."""
        kind = "xlsx" if (request.get_json(silent=True) or {}).get("format") == "xlsx" else "json"
        out = save_export(kind)
        opened = False
        if shell is not None:
            try:
                shell.open_path(out if kind == "xlsx" else out.parent)
                opened = True
            except Exception:  # noqa: BLE001
                pass
        return jsonify({"ok": True, "path": str(out), "name": out.name, "opened": opened})

    @app.post("/api/settings/import")
    def settings_import():
        f = request.files.get("file")
        if not f:
            return err("Δεν επιλέχθηκε αρχείο")
        try:
            data = json.loads(f.read().decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            return err("Το αρχείο δεν είναι αρχείο ρυθμίσεων του Price Tracker")
        if not isinstance(data, dict) or data.get("app") != "PriceTracker" or data.get("kind") != "settings":
            return err("Το αρχείο δεν είναι αρχείο ρυθμίσεων του Price Tracker")
        settings = clean_settings(data.get("settings") or {})
        db.set_settings(settings)
        from .shops import host_of

        def sector_id(name):
            name = str(name or "").strip()
            if not name:
                return None
            have = next((x["id"] for x in db.list_sectors() if x["name"].lower() == name.lower()), None)
            return have or db.create_sector(name)

        for n in data.get("sectors") or []:
            sector_id(n)
        added = updated = 0
        for x in data.get("shops") or []:
            if not isinstance(x, dict):
                continue
            domain = host_of("https://" + str(x.get("domain") or "").strip())
            name = str(x.get("name") or "").strip()
            region = x.get("region") if x.get("region") in REGION_LABELS else "OTHER"
            country = x.get("country") if x.get("country") in COUNTRIES else guess_country(domain)
            if country:
                region = region_of_country(country)
            url = str(x.get("search_url") or "").strip()
            if not domain or "." not in domain or not name or (url and "{q}" not in url):
                continue
            enabled = 1 if (x.get("search_enabled") and url) else 0
            have = db.shop_by_domain(domain)
            if have:
                db.update_shop(have["id"], name=name, region=region, search_url=url, search_enabled=enabled,
                               country=country)
                updated += 1
            else:
                db.create_shop(name, region, domain, url, enabled, country=country)
                added += 1
            if isinstance(x.get("sectors"), list):
                shop = db.shop_by_domain(domain)
                if shop:
                    db.set_shop_sectors(shop["id"], [i for i in map(sector_id, x["sectors"]) if i])
        apply_autostart(settings)
        return jsonify({"ok": True, "settings": len(settings), "shops_added": added, "shops_updated": updated})

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

    # ------------------------------------------------------------ αγορές, αποδείξεις, εγγυήσεις
    def save_receipt(file_storage, shop, when):
        name = file_storage.filename or "receipt"
        ext = Path(name).suffix.lower()
        if ext not in RECEIPT_TYPES:
            raise ValueError("Η απόδειξη πρέπει να είναι PDF ή εικόνα (JPG, PNG κ.λπ.)")
        safe = re.sub(r"[^\w\-.]+", "_", Path(name).stem, flags=re.UNICODE)[:60] or "receipt"
        year = (when or datetime.now().strftime("%Y"))[:4]
        rel = Path(receipts.RECEIPTS_DIR) / year / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}{ext}"
        dest = data_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        file_storage.save(dest)
        if dest.stat().st_size == 0:
            dest.unlink()
            raise ValueError("Το αρχείο της απόδειξης είναι κενό")
        return db.create_receipt(rel.as_posix(), name, shop, when)

    def _months(v):
        if v in (None, "", "0", 0):
            return None
        m = int(float(v))
        if not 0 < m <= 240:
            raise ValueError("Η εγγύηση πρέπει να είναι από 1 μήνα έως 20 έτη")
        return m

    @app.post("/api/purchase")
    def purchase():
        """Καταχώριση αγοράς ενός ή περισσότερων προϊόντων (ίδια απόδειξη/τιμολόγιο).

        multipart/form-data: items (JSON λίστα {product_id | name+category_id, price, warranty_months}),
        date, shop, receipt (αρχείο, προαιρετικό).
        """
        form = request.form if request.form else (request.get_json(silent=True) or {})
        try:
            items = json.loads(form.get("items") or "[]") if isinstance(form.get("items"), str) else form.get("items") or []
        except ValueError:
            return err("Μη έγκυρα προϊόντα")
        if not items:
            return err("Διάλεξε τουλάχιστον ένα προϊόν")
        when = form.get("date") or datetime.now().strftime("%Y-%m-%d")
        shop = (form.get("shop") or "").strip() or None
        receipt_id = None
        f = request.files.get("receipt")
        if f and f.filename:
            receipt_id = save_receipt(f, shop, when)
        elif form.get("receipt_id"):
            receipt_id = int(form["receipt_id"])
        done = []
        for it in items:
            pid = it.get("product_id")
            if not pid:
                name = (it.get("name") or "").strip()
                if not name:
                    continue
                pid = db.create_product(name, _cat(it.get("category_id")))
            fields = dict(purchased=1, purchased_at=when, purchased_price=_num(it.get("price")),
                          purchased_shop=shop, warranty_months=_months(it.get("warranty_months")))
            if receipt_id:
                fields["receipt_id"] = receipt_id
            db.update_product(int(pid), **fields)
            done.append(int(pid))
        if receipt_id:
            receipts.organize(db, data_dir, receipt_id)
        return jsonify({"ok": True, "products": done, "receipt_id": receipt_id})

    @app.post("/api/products/<int:pid>/receipt")
    def attach_receipt(pid):
        p = db.get_product(pid)
        if not p:
            abort(404)
        f = request.files.get("receipt")
        if not f or not f.filename:
            return err("Δεν επιλέχθηκε αρχείο")
        rid = save_receipt(f, p.get("purchased_shop"), p.get("purchased_at"))
        others = [int(x) for x in request.form.getlist("also") if x.isdigit()]
        for x in [pid] + others:
            db.update_product(x, receipt_id=rid)
        receipts.organize(db, data_dir, rid)
        return jsonify({"ok": True, "receipt_id": rid})

    @app.put("/api/products/<int:pid>/warranty")
    def set_warranty(pid):
        b = request.get_json() or {}
        fields = {"warranty_months": _months(b.get("warranty_months"))}
        if b.get("date"):
            fields["purchased_at"] = b["date"]
        if "price" in b:
            fields["purchased_price"] = _num(b.get("price"))
        if "shop" in b:
            fields["purchased_shop"] = (b.get("shop") or "").strip() or None
        db.update_product(pid, **fields)
        return jsonify({"ok": True})

    @app.get("/api/warranties")
    def warranties():
        out = []
        cat = request.args.get("category", "all")
        for p in db.list_purchased():
            if cat not in ("all", "") and (p.get("category_id") or 0) != int(cat):
                continue
            w = warranty.warranty_info(p)
            out.append({**p, **w, "warranty_label": warranty.months_label(p.get("warranty_months"))})
        order = {"soon": 0, "active": 1, "none": 2, "expired": 3}
        out.sort(key=lambda r: (order[r["warranty_state"]], r["warranty_end"] or "9999"))
        return jsonify(out)

    @app.get("/api/receipts/<int:rid>")
    def receipt_info(rid):
        r = db.get_receipt(rid)
        if not r:
            abort(404)
        return jsonify({**r, "products": db.receipt_products(rid)})

    @app.post("/api/receipts/<int:rid>/open")
    def open_receipt(rid):
        r = db.get_receipt(rid)
        if not r:
            abort(404)
        path = data_dir / r["file"]
        if shell is not None:
            shell.open_path(path)
            return jsonify({"ok": True, "opened": True})
        return jsonify({"ok": True, "opened": False, "url": "/shots/" + r["file"]})

    @app.delete("/api/receipts/<int:rid>")
    def delete_receipt(rid):
        r = db.get_receipt(rid)
        if not r:
            abort(404)
        db.delete_receipt(rid)
        try:
            (data_dir / r["file"]).unlink()
        except OSError:
            pass
        return jsonify({"ok": True})

    # ------------------------------------------------------------ ενημερώσεις
    @app.get("/api/update")
    def update_status():
        if updater is None:
            return jsonify({"supported": False})
        return jsonify(updater.status())

    @app.post("/api/update/check")
    def update_check():
        if updater is None:
            return err("Οι ενημερώσεις δεν είναι διαθέσιμες")
        updater.check_async(force=True)
        return jsonify({"ok": True})

    @app.post("/api/update/install")
    def update_install():
        if updater is None:
            return err("Οι ενημερώσεις δεν είναι διαθέσιμες")
        updater.install_async()
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
