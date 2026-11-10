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
    rel = sync(db, data_dir, product["id"])
    if not rel:
        raise RuntimeError("Το προϊόν διαγράφηκε")  # ποτέ screenshot χύμα στον φάκελο PriceTracker
    return Path(data_dir) / rel


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


def all_dirs(db, data_dir: Path, product_id: int) -> list[Path]:
    """Όλοι οι φάκελοι που αφορούν ένα προϊόν: ο τωρινός, αυτός με το όνομα που θα έπρεπε να έχει
    και οι παλιοί screenshots\\<id>_… (όσοι υπάρχουν)."""
    data_dir = Path(data_dir)
    out = []
    cur = current_dir(db, product_id)
    if cur:
        out.append(data_dir / cur)
    p = db.get_product(product_id)
    if p:
        base = data_dir / PRODUCTS_DIR / folder_name(p)
        out += [base, base.with_name(base.name + f" ({product_id})")]
    old = data_dir / OLD_SHOTS_DIR
    if old.is_dir():
        out += [d for d in old.glob(f"{product_id}_*") if d.is_dir()]
    root = data_dir.resolve()
    # Ποτέ φάκελος που έχει screenshots άλλου προϊόντος (π.χ. ίδιο όνομα και ημερομηνία).
    others = {(data_dir / rel).resolve() for rel in db.other_screenshot_dirs(product_id)}
    seen, res = set(), []
    for d in out:
        r = d.resolve()
        if r in others or any(o.is_relative_to(r) for o in others):
            continue
        if r not in seen and root in r.parents and r.exists():
            seen.add(r)
            res.append(d)
    return res


def remove_tree(path: Path):
    """Σβήσιμο φακέλου ή αρχείου που αντέχει σε αρχεία μόνο-για-ανάγνωση (π.χ. OneDrive)."""
    import os
    import stat
    import sys

    def onexc(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    path = Path(path)
    try:
        if path.is_dir() and not path.is_symlink():
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=onexc)
            else:
                shutil.rmtree(path, onerror=lambda f, p, e: onexc(f, p, e[1]))
        elif path.exists():
            try:
                path.unlink()
            except PermissionError:
                os.chmod(path, stat.S_IWRITE)
                path.unlink()
    except OSError:
        pass


def remove(dirs: list[Path], data_dir: Path):
    """Σβήνει τους φακέλους ενός προϊόντος (από all_dirs) και ό,τι άδειο μείνει πάνω τους."""
    for d in dirs:
        remove_tree(d)
        _remove_if_empty(Path(d).parent, Path(data_dir))
