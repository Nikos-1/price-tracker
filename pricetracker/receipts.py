"""Οργάνωση των αρχείων αποδείξεων στα Έγγραφα\\PriceTracker\\Αποδείξεις.

Κάθε απόδειξη παίρνει όνομα που λέει ημερομηνία, κατάστημα και προϊόντα, ώστε να βρίσκεται εύκολα και
από την Εξερεύνηση αρχείων των Windows, π.χ.
    Αποδείξεις\\2026\\2026-11-27 Plaisio - Corsair RM850x, Pitsos κουζίνα.pdf
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

RECEIPTS_DIR = "Αποδείξεις"
MAX_NAME = 120


def clean(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", text or "")
    return " ".join(text.split()).strip(" .")


def receipt_name(receipt: dict, product_names: list[str], ext: str) -> str:
    date = (receipt.get("date") or receipt.get("created_at") or "")[:10]
    shop = clean(receipt.get("shop") or "")
    head = " ".join(x for x in (date, shop) if x)
    names = [clean(n) for n in product_names if clean(n)]
    tail = ", ".join(names)
    if len(tail) > MAX_NAME:
        tail = tail[:MAX_NAME].rsplit(",", 1)[0] + f" και άλλα ({len(names)})"
    base = f"{head} - {tail}" if head and tail else head or tail or f"Απόδειξη {receipt.get('id', '')}"
    return clean(base) + ext.lower()


def target_path(data_dir: Path, receipt: dict, product_names: list[str]) -> Path:
    ext = Path(receipt["file"]).suffix or ".pdf"
    year = ((receipt.get("date") or receipt.get("created_at") or "")[:4]) or "Χωρίς ημερομηνία"
    return Path(data_dir) / RECEIPTS_DIR / year / receipt_name(receipt, product_names, ext)


def organize(db, data_dir: Path, receipt_id: int) -> str | None:
    """Μετονομάζει/μετακινεί το αρχείο της απόδειξης ώστε να ταιριάζει με τα προϊόντα της. Επιστρέφει τη νέα διαδρομή."""
    r = db.get_receipt(receipt_id)
    if not r:
        return None
    data_dir = Path(data_dir)
    src = data_dir / r["file"]
    if not src.exists():
        return None
    names = [p["name"] for p in db.receipt_products(receipt_id)]
    dest = target_path(data_dir, r, names)
    if dest.resolve() == src.resolve():
        return r["file"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    final, i = dest, 2
    while final.exists():
        final = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
        i += 1
    shutil.move(str(src), str(final))
    rel = final.relative_to(data_dir).as_posix()
    db.set_receipt_file(receipt_id, rel)
    # σβήσε άδειους παλιούς φακέλους
    try:
        parent = src.parent
        while parent != data_dir and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    except OSError:
        pass
    return rel


def organize_all(db, data_dir: Path):
    for rid in db.receipt_ids():
        try:
            organize(db, data_dir, rid)
        except OSError:
            pass
