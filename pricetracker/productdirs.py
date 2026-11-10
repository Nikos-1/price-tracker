"""Φάκελοι προϊόντων στα Έγγραφα\\PriceTracker\\Προϊόντα, με τα screenshots τους.

Κάθε προϊόν έχει έναν φάκελο με την ημερομηνία που προστέθηκε στην εφαρμογή και το όνομά του, π.χ.
    Προϊόντα\\2026-11-10 Κουζίνα Pitsos\\20261110_083012_Plaisio.jpg
Αν αλλάξει το όνομα του προϊόντος, αλλάζει και ο φάκελος.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path, PurePosixPath

from .receipts import clean

PRODUCTS_DIR = "Προϊόντα"
OLD_SHOTS_DIR = "screenshots"
MAX_NAME = 80


def folder_name(product: dict) -> str:
    date = (product.get("created_at") or f"{datetime.now():%Y-%m-%d}")[:10]
    name = clean(product.get("name") or "")[:MAX_NAME].strip(" .") or f"Προϊόν {product.get('id', '')}"
    return f"{date} {name}"


def _posix(rel: str) -> str:
    return (rel or "").replace("\\", "/")


def current_dir(db, product_id: int) -> str | None:
    """Ο φάκελος (σχετική διαδρομή) όπου είναι ήδη τα screenshots του προϊόντος, αν υπάρχουν."""
    for rel in sorted(db.product_screenshots(product_id)):
        parent = str(PurePosixPath(_posix(rel)).parent)
        if parent and parent != ".":
            return parent
    return None


def target_dir(data_dir: Path, product: dict, current: str | None = None) -> str:
    rel = f"{PRODUCTS_DIR}/{folder_name(product)}"
    if rel != current and (Path(data_dir) / rel).exists():
        rel = f"{PRODUCTS_DIR}/{folder_name(product)} ({product['id']})"  # ίδιο όνομα και ημερομηνία με άλλο
    return rel


def product_dir(db, data_dir: Path, product: dict) -> Path:
    """Πού μπαίνει το επόμενο screenshot του προϊόντος (φτιάχνει/μετονομάζει τον φάκελο αν χρειάζεται)."""
    return Path(data_dir) / sync(db, data_dir, product["id"])


def sync(db, data_dir: Path, product_id: int) -> str:
    """Φέρνει τον φάκελο του προϊόντος στο σωστό όνομα και ενημερώνει τις διαδρομές στη βάση."""
    data_dir = Path(data_dir)
    product = db.get_product(product_id)
    if not product:
        return ""
    cur = current_dir(db, product_id)
    want = f"{PRODUCTS_DIR}/{folder_name(product)}"
    if cur in (want, f"{want} ({product_id})"):
        return cur
    target = target_dir(data_dir, product, cur)
    if cur is None:
        return target
    src, dst = data_dir / cur, data_dir / target
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                for f in src.iterdir():
                    shutil.move(str(f), str(dst / f.name))
                src.rmdir()
            else:
                shutil.move(str(src), str(dst))
    except OSError:
        return cur  # π.χ. ανοιχτό αρχείο στον Explorer· ξαναδοκιμάζει την επόμενη φορά
    db.move_screenshots(product_id, cur, target)
    _remove_if_empty(src.parent, data_dir)
    return target


def _remove_if_empty(d: Path, root: Path):
    try:
        while d != root and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent
    except OSError:
        pass


def sync_all(db, data_dir: Path):
    """Στην εκκίνηση: παλιοί φάκελοι screenshots\\<id>_<όνομα> → Προϊόντα\\<ημερομηνία> <όνομα>."""
    for p in db.list_products():
        try:
            sync(db, data_dir, p["id"])
        except Exception:  # noqa: BLE001
            pass
    _remove_if_empty(Path(data_dir) / OLD_SHOTS_DIR, Path(data_dir))


def remove(db, data_dir: Path, product_id: int):
    cur = current_dir(db, product_id)
    if cur:
        shutil.rmtree(Path(data_dir) / cur, ignore_errors=True)
