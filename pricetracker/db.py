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
"""

DEFAULT_SETTINGS = {
    "check_interval_hours": 6,
    "bf_interval_hours": 1,
    "bf_window_auto": True,          # 20/11 – 2/12 κάθε χρονιάς
    "bf_start": "",                  # MM-DD αν bf_window_auto = False
    "bf_end": "",
    "headless": True,
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
            self._seed(c)

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
        for s in shops_mod.DEFAULT_SHOPS:
            c.execute(
                "INSERT OR IGNORE INTO shops(key,name,region,domain,search_url) VALUES(?,?,?,?,?)",
                (s["key"], s["name"], s["region"], s["domain"], s["search_url"]),
            )

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

    def delete_category(self, cat_id: int, delete_products: bool = False):
        with self.conn() as c:
            if delete_products:
                c.execute("DELETE FROM products WHERE category_id=?", (cat_id,))
            else:
                c.execute("UPDATE products SET category_id=NULL WHERE category_id=?", (cat_id,))
            c.execute("DELETE FROM categories WHERE id=?", (cat_id,))

    # ---------- shops ----------
    def list_shops(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM shops ORDER BY region, name COLLATE NOCASE")]

    def get_shop(self, shop_id):
        with self.conn() as c:
            r = c.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
        return dict(r) if r else None

    def update_shop(self, shop_id: int, **fields):
        allowed = {"name", "region", "search_url", "search_enabled"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE shops SET {sets} WHERE id=?", (*fields.values(), shop_id))

    def create_shop(self, name, region, domain, search_url="", search_enabled=1) -> int:
        domain = domain.lower().removeprefix("www.")
        key = "custom_" + domain.replace(".", "_")
        with self.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO shops(key,name,region,domain,search_url,search_enabled) VALUES(?,?,?,?,?,?)",
                (key, name, region, domain, search_url, search_enabled),
            )
            if cur.lastrowid and cur.rowcount:
                return cur.lastrowid
            return c.execute("SELECT id FROM shops WHERE key=?", (key,)).fetchone()["id"]

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
                   "purchased", "purchased_at", "purchased_price", "purchased_shop"}
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
                """SELECT l.*, s.name AS shop_name, s.region AS region, s.key AS shop_key
                   FROM listings l LEFT JOIN shops s ON s.id = l.shop_id WHERE l.id=?""",
                (listing_id,),
            ).fetchone()
        return dict(r) if r else None

    def list_listings(self, product_id=None, status="active"):
        q = """SELECT l.*, s.name AS shop_name, s.region AS region, s.key AS shop_key
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
