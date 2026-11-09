"""Τοπική βάση δεδομένων SQLite."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import shops as shops_mod

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    domain TEXT NOT NULL,
    search_url TEXT,
    search_enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    target_price REAL,
    notes TEXT,
    ean TEXT,
    mpn TEXT,
    brand TEXT,
    image_url TEXT,
    purchased INTEGER NOT NULL DEFAULT 0,
    purchased_at TEXT,
    purchased_price REAL,
    purchased_shop TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    shop_id INTEGER REFERENCES shops(id),
    url TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'active',   -- active | candidate | rejected
    last_price REAL,
    last_currency TEXT,
    last_in_stock INTEGER,
    last_checked TEXT,
    last_error TEXT,
    last_screenshot TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(product_id, url)
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    checked_at TEXT NOT NULL,
    price REAL,
    currency TEXT,
    in_stock INTEGER,
    screenshot TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_history_listing ON price_history(listing_id, checked_at);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY,
    file TEXT NOT NULL,              -- σχετική διαδρομή μέσα στον φάκελο δεδομένων
    original_name TEXT,
    shop TEXT,
    date TEXT,
    created_at TEXT NOT NULL
);
"""

# Στήλες που προστέθηκαν σε νεότερες εκδόσεις (για βάσεις που υπάρχουν ήδη)
MIGRATIONS = {
    "products": [("receipt_id", "INTEGER REFERENCES receipts(id) ON DELETE SET NULL"),
                 ("warranty_months", "INTEGER")],
    "shops": [("country", "TEXT")],
}

DEFAULT_SETTINGS = {
    "check_interval_hours": 6,
    "auto_update": True,
    "warranty_reminder_days": 30,
    "bf_interval_hours": 1,
    "bf_window_auto": True,          # 20/11 – 2/12 κάθε χρονιάς
    "bf_start": "",                  # MM-DD αν bf_window_auto = False
    "bf_end": "",
    "browser_mode": "auto",          # auto | hidden | visible
    "close_action": "ask",           # ask | tray | exit
    "start_with_windows": False,
    "notify_back_in_stock": True,
    "notify_target_price": True,
    "notify_new_low": True,
    "email_enabled": False,
    "smtp_provider": "gmail",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": "",
    "email_to": "",
    "screenshot_mode": "changes",    # changes | always
    "research_daily": True,          # μία φορά τη μέρα αναζήτηση στα καταστήματα που δεν έχουν το προϊόν
    "notify_new_shop": True,
}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)
            self._seed(c)

    @staticmethod
    def _migrate(c):
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextmanager
    def conn(self):
        with self._lock:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    def _seed(self, c):
        row = c.execute("SELECT value FROM settings WHERE key='deleted_shop_keys'").fetchone()
        deleted = set(json.loads(row["value"])) if row and row["value"] else set()
        if not c.execute("SELECT 1 FROM settings WHERE key='removed_defaults_v04'").fetchone():
            # Μία φορά: eBay και MediaMarkt βγαίνουν από τη λίστα. Αν κάποιο χρησιμοποιείται ήδη σε προϊόν,
            # μένει για εκείνο το προϊόν, αλλά δεν χρησιμοποιείται πια στην αναζήτηση νέων προϊόντων.
            for key in shops_mod.REMOVED_DEFAULT_KEYS:
                r = c.execute("SELECT id FROM shops WHERE key=?", (key,)).fetchone()
                if not r:
                    continue
                if c.execute("SELECT 1 FROM listings WHERE shop_id=?", (r["id"],)).fetchone():
                    c.execute("UPDATE shops SET search_enabled=0 WHERE id=?", (r["id"],))
                else:
                    c.execute("DELETE FROM shops WHERE id=?", (r["id"],))
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('removed_defaults_v04','true')")
        have_domains = {r["domain"] for r in c.execute("SELECT domain FROM shops")}
        for s in shops_mod.DEFAULT_SHOPS:
            if s["key"] in deleted or s["key"] in shops_mod.REMOVED_DEFAULT_KEYS:
                continue
            exists = c.execute("SELECT 1 FROM shops WHERE key=?", (s["key"],)).fetchone()
            if not exists and s["domain"] in have_domains:
                continue  # ο χρήστης το έχει ήδη προσθέσει μόνος του
            c.execute(
                "INSERT OR IGNORE INTO shops(key,name,region,domain,search_url,country) VALUES(?,?,?,?,?,?)",
                (s["key"], s["name"], s["region"], s["domain"], s["search_url"], s["country"]),
            )
        # Χώρα για καταστήματα παλιότερων εκδόσεων (μόνο όπου λείπει, τίποτα άλλο δεν αλλάζει)
        defaults = {s["key"]: s["country"] for s in shops_mod.DEFAULT_SHOPS}
        for r in c.execute("SELECT id, key, domain FROM shops WHERE country IS NULL").fetchall():
            country = defaults.get(r["key"]) or shops_mod.guess_country(r["domain"])
            c.execute("UPDATE shops SET country=? WHERE id=?", (country, r["id"]))

    # ---------- settings ----------
    def get_settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        with self.conn() as c:
            for r in c.execute("SELECT key, value FROM settings"):
                try:
                    out[r["key"]] = json.loads(r["value"])
                except (TypeError, ValueError):
                    out[r["key"]] = r["value"]
        return out

    def set_settings(self, values: dict):
        with self.conn() as c:
            for k, v in values.items():
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, json.dumps(v)))

    # ---------- categories ----------
    def list_categories(self):
        with self.conn() as c:
            rows = c.execute(
                """SELECT c.id, c.name,
                          (SELECT COUNT(*) FROM products p WHERE p.category_id = c.id) AS product_count
                   FROM categories c ORDER BY c.name COLLATE NOCASE"""
            ).fetchall()
        return [dict(r) for r in rows]

    def create_category(self, name: str) -> int:
        name = name.strip()
        if not name:
            raise ValueError("Το όνομα κατηγορίας είναι κενό")
        with self.conn() as c:
            try:
                cur = c.execute("INSERT INTO categories(name, created_at) VALUES(?,?)", (name, now_iso()))
            except sqlite3.IntegrityError:
                raise ValueError(f"Υπάρχει ήδη κατηγορία «{name}»")
            return cur.lastrowid

    def rename_category(self, cat_id: int, name: str):
        name = name.strip()
        if not name:
            raise ValueError("Το όνομα κατηγορίας είναι κενό")
        with self.conn() as c:
            try:
                c.execute("UPDATE categories SET name=? WHERE id=?", (name, cat_id))
            except sqlite3.IntegrityError:
                raise ValueError(f"Υπάρχει ήδη κατηγορία «{name}»")

    def delete_category(self, cat_id: int) -> list[int]:
        """Διαγράφει την κατηγορία. Επιστρέφει τα προϊόντα (μη αγορασμένα) που πρέπει να διαγραφούν μαζί της.
        Τα αγορασμένα μένουν, χωρίς κατηγορία, για να φαίνονται η αγορά και η εγγύηση."""
        with self.conn() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM products WHERE category_id=? AND purchased=0", (cat_id,))]
            c.execute("UPDATE products SET category_id=NULL WHERE category_id=?", (cat_id,))
            c.execute("DELETE FROM categories WHERE id=?", (cat_id,))
        return ids

    def category_usage(self, cat_id: int) -> dict:
        with self.conn() as c:
            r = c.execute("""SELECT SUM(purchased=0) AS tracked, SUM(purchased=1) AS purchased
                             FROM products WHERE category_id=?""", (cat_id,)).fetchone()
        return {"tracked": r["tracked"] or 0, "purchased": r["purchased"] or 0}

    def product_screenshots(self, product_id: int) -> set[str]:
        with self.conn() as c:
            rows = c.execute(
                """SELECT h.screenshot FROM price_history h JOIN listings l ON l.id = h.listing_id
                   WHERE l.product_id=? AND h.screenshot IS NOT NULL
                   UNION SELECT last_screenshot FROM listings WHERE product_id=? AND last_screenshot IS NOT NULL""",
                (product_id, product_id)).fetchall()
        return {r[0] for r in rows}

    def listing_screenshots(self, listing_ids) -> set[str]:
        ids = list(listing_ids)
        if not ids:
            return set()
        qs = ",".join("?" * len(ids))
        with self.conn() as c:
            rows = c.execute(
                f"""SELECT screenshot FROM price_history WHERE listing_id IN ({qs}) AND screenshot IS NOT NULL
                    UNION SELECT last_screenshot FROM listings WHERE id IN ({qs}) AND last_screenshot IS NOT NULL""",
                (*ids, *ids)).fetchall()
        return {r[0] for r in rows}

    def shop_listing_ids(self, shop_id: int) -> list[int]:
        with self.conn() as c:
            return [r[0] for r in c.execute("SELECT id FROM listings WHERE shop_id=?", (shop_id,))]

    # ---------- shops ----------
    def list_shops(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM shops ORDER BY CASE region WHEN 'GR' THEN 0 WHEN 'EU' THEN 1 ELSE 2 END, country, name COLLATE NOCASE")]

    def get_shop(self, shop_id):
        with self.conn() as c:
            r = c.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
        return dict(r) if r else None

    def update_shop(self, shop_id: int, **fields):
        allowed = {"name", "region", "search_url", "search_enabled", "country"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE shops SET {sets} WHERE id=?", (*fields.values(), shop_id))

    def create_shop(self, name, region, domain, search_url="", search_enabled=1, country=None) -> int:
        domain = domain.lower().removeprefix("www.")
        key = "custom_" + domain.replace(".", "_")
        if country is None:
            country = shops_mod.guess_country(domain)
        with self.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO shops(key,name,region,domain,search_url,search_enabled,country)"
                " VALUES(?,?,?,?,?,?,?)",
                (key, name, region, domain, search_url, search_enabled, country),
            )
            if cur.lastrowid and cur.rowcount:
                return cur.lastrowid
            return c.execute("SELECT id FROM shops WHERE key=?", (key,)).fetchone()["id"]

    def shop_by_domain(self, domain: str):
        domain = domain.lower().strip().removeprefix("www.")
        with self.conn() as c:
            r = c.execute("SELECT * FROM shops WHERE domain=?", (domain,)).fetchone()
        return dict(r) if r else None

    def shop_usage(self, shop_id: int) -> int:
        with self.conn() as c:
            return c.execute("SELECT COUNT(DISTINCT product_id) FROM listings WHERE shop_id=?",
                             (shop_id,)).fetchone()[0]

    def delete_shop(self, shop_id: int, delete_listings: bool = False):
        shop = self.get_shop(shop_id)
        if not shop:
            return
        used = self.shop_usage(shop_id)
        if used and not delete_listings:
            raise ValueError(f"Το κατάστημα «{shop['name']}» χρησιμοποιείται σε {used} "
                             f"{'προϊόν' if used == 1 else 'προϊόντα'}")
        with self.conn() as c:
            c.execute("DELETE FROM listings WHERE shop_id=?", (shop_id,))
            c.execute("DELETE FROM shops WHERE id=?", (shop_id,))
            if not shop["key"].startswith("custom_"):
                row = c.execute("SELECT value FROM settings WHERE key='deleted_shop_keys'").fetchone()
                keys = set(json.loads(row["value"])) if row and row["value"] else set()
                keys.add(shop["key"])
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('deleted_shop_keys',?)",
                          (json.dumps(sorted(keys)),))

    def shop_for_url(self, url: str) -> dict:
        """Βρίσκει (ή δημιουργεί) το κατάστημα για ένα URL."""
        host = shops_mod.host_of(url)
        best = None
        for s in self.list_shops():
            if shops_mod.domain_matches(host, s["domain"]):
                if best is None or len(s["domain"]) > len(best["domain"]):
                    best = s
        if best:
            return best
        sid = self.create_shop(shops_mod.guess_name(host), shops_mod.guess_region(host), host, "", 0)
        return self.get_shop(sid)

    # ---------- products ----------
    def create_product(self, name, category_id=None, target_price=None, notes=None) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO products(name, category_id, target_price, notes, created_at) VALUES(?,?,?,?,?)",
                (name.strip() or "Νέο προϊόν", category_id, target_price, notes, now_iso()),
            )
            return cur.lastrowid

    def update_product(self, product_id: int, **fields):
        allowed = {"name", "category_id", "target_price", "notes", "ean", "mpn", "brand", "image_url",
                   "purchased", "purchased_at", "purchased_price", "purchased_shop", "receipt_id",
                   "warranty_months"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE products SET {sets} WHERE id=?", (*fields.values(), product_id))

    def delete_product(self, product_id: int):
        with self.conn() as c:
            c.execute("DELETE FROM products WHERE id=?", (product_id,))

    def get_product(self, product_id: int):
        with self.conn() as c:
            r = c.execute(
                """SELECT p.*, c.name AS category_name FROM products p
                   LEFT JOIN categories c ON c.id = p.category_id WHERE p.id=?""",
                (product_id,),
            ).fetchone()
        return dict(r) if r else None

    def list_products(self, category_id=None, purchased=None):
        q = """SELECT p.*, c.name AS category_name FROM products p
               LEFT JOIN categories c ON c.id = p.category_id WHERE 1=1"""
        args = []
        if category_id is not None:
            if category_id == 0:
                q += " AND p.category_id IS NULL"
            else:
                q += " AND p.category_id = ?"
                args.append(category_id)
        if purchased is not None:
            q += " AND p.purchased = ?"
            args.append(1 if purchased else 0)
        q += " ORDER BY p.created_at DESC"
        with self.conn() as c:
            return [dict(r) for r in c.execute(q, args)]

    # ---------- listings ----------
    def add_listing(self, product_id: int, url: str, status="active", title=None):
        shop = self.shop_for_url(url)
        with self.conn() as c:
            existing = c.execute("SELECT id, status FROM listings WHERE product_id=? AND url=?",
                                 (product_id, url)).fetchone()
            if existing:
                if status == "active" and existing["status"] != "active":
                    c.execute("UPDATE listings SET status='active' WHERE id=?", (existing["id"],))
                return existing["id"]
            cur = c.execute(
                "INSERT INTO listings(product_id, shop_id, url, title, status, created_at) VALUES(?,?,?,?,?,?)",
                (product_id, shop["id"], url, title, status, now_iso()),
            )
            return cur.lastrowid

    def get_listing(self, listing_id: int):
        with self.conn() as c:
            r = c.execute(
                """SELECT l.*, s.name AS shop_name, s.region AS region, s.key AS shop_key, s.country AS country
                   FROM listings l LEFT JOIN shops s ON s.id = l.shop_id WHERE l.id=?""",
                (listing_id,),
            ).fetchone()
        return dict(r) if r else None

    def list_listings(self, product_id=None, status="active"):
        q = """SELECT l.*, s.name AS shop_name, s.region AS region, s.key AS shop_key, s.country AS country
               FROM listings l LEFT JOIN shops s ON s.id = l.shop_id WHERE 1=1"""
        args = []
        if product_id is not None:
            q += " AND l.product_id=?"
            args.append(product_id)
        if status is not None:
            q += " AND l.status=?"
            args.append(status)
        q += " ORDER BY s.region, l.last_price IS NULL, l.last_price"
        with self.conn() as c:
            return [dict(r) for r in c.execute(q, args)]

    def set_listing_status(self, listing_id: int, status: str):
        with self.conn() as c:
            c.execute("UPDATE listings SET status=? WHERE id=?", (status, listing_id))

    def delete_listing(self, listing_id: int):
        with self.conn() as c:
            c.execute("DELETE FROM listings WHERE id=?", (listing_id,))

    def record_check(self, listing_id, price, currency, in_stock, title=None, screenshot=None, error=None):
        ts = now_iso()
        with self.conn() as c:
            c.execute(
                "INSERT INTO price_history(listing_id, checked_at, price, currency, in_stock, screenshot, error)"
                " VALUES(?,?,?,?,?,?,?)",
                (listing_id, ts, price, currency, None if in_stock is None else int(in_stock), screenshot, error),
            )
            if error:
                c.execute("UPDATE listings SET last_checked=?, last_error=? WHERE id=?", (ts, error, listing_id))
            else:
                c.execute(
                    """UPDATE listings SET last_price=?, last_currency=?, last_in_stock=?, last_checked=?,
                       last_error=NULL, title=COALESCE(?, title),
                       last_screenshot=COALESCE(?, last_screenshot) WHERE id=?""",
                    (price, currency, None if in_stock is None else int(in_stock), ts, title, screenshot,
                     listing_id),
                )
        return ts

    def history(self, listing_id=None, product_id=None, since=None):
        q = """SELECT h.*, l.product_id, l.url, s.name AS shop_name, s.region AS region
               FROM price_history h JOIN listings l ON l.id = h.listing_id
               LEFT JOIN shops s ON s.id = l.shop_id WHERE 1=1"""
        args = []
        if listing_id is not None:
            q += " AND h.listing_id=?"
            args.append(listing_id)
        if product_id is not None:
            q += " AND l.product_id=?"
            args.append(product_id)
        if since:
            q += " AND h.checked_at>=?"
            args.append(since)
        q += " ORDER BY h.checked_at"
        with self.conn() as c:
            return [dict(r) for r in c.execute(q, args)]

    # ---------- αποδείξεις ----------
    def create_receipt(self, file: str, original_name: str | None, shop: str | None, date: str | None) -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO receipts(file, original_name, shop, date, created_at) VALUES(?,?,?,?,?)",
                            (file, original_name, shop, date, now_iso()))
            return cur.lastrowid

    def get_receipt(self, receipt_id):
        with self.conn() as c:
            r = c.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        return dict(r) if r else None

    def receipt_products(self, receipt_id):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT id, name FROM products WHERE receipt_id=? ORDER BY name",
                                               (receipt_id,))]

    def delete_receipt(self, receipt_id):
        with self.conn() as c:
            c.execute("UPDATE products SET receipt_id=NULL WHERE receipt_id=?", (receipt_id,))
            c.execute("DELETE FROM receipts WHERE id=?", (receipt_id,))

    def list_purchased(self):
        with self.conn() as c:
            rows = c.execute(
                """SELECT p.*, c.name AS category_name, r.file AS receipt_file, r.original_name AS receipt_name
                   FROM products p LEFT JOIN categories c ON c.id = p.category_id
                   LEFT JOIN receipts r ON r.id = p.receipt_id
                   WHERE p.purchased = 1 ORDER BY p.purchased_at DESC, p.name"""
            ).fetchall()
        return [dict(r) for r in rows]

    def set_receipt_file(self, receipt_id: int, file: str):
        with self.conn() as c:
            c.execute("UPDATE receipts SET file=? WHERE id=?", (file, receipt_id))

    def receipt_ids(self) -> list[int]:
        with self.conn() as c:
            return [r[0] for r in c.execute("SELECT id FROM receipts")]
