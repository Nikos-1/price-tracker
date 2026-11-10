"""Πού μένει η βάση και πώς προστατεύεται.

Η «ζωντανή» βάση (pricetracker.db) μένει στο %LOCALAPPDATA%\\PriceTracker, που δεν συγχρονίζεται με το OneDrive:
ένα SQLite αρχείο που το αντιγράφει το OneDrive την ώρα που γράφεται μπορεί να χαλάσει (sqlite.org/howtocorrupt).
Στα Έγγραφα\\PriceTracker μένει ένα «Αντίγραφο βάσης.db», κλειστό και πλήρες αρχείο που ανανεώνεται κάθε μέρα,
ώστε να υπάρχει πάντα σωστό αντίγραφο στο cloud. Αν λείπει ή χάλασε η τοπική βάση (π.χ. μετά από format),
επαναφέρεται μόνη της από το αντίγραφο.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

LIVE_NAME = "pricetracker.db"
SIDE_FILES = ("-wal", "-shm", "-journal")
BACKUP_NAME = "Αντίγραφο βάσης.db"
BACKUP_EVERY_HOURS = 24
PRE_UPDATE_NAME = "pricetracker-πριν-την-ενημέρωση.db"


def _files(path: Path):
    return [path] + [path.with_name(path.name + s) for s in SIDE_FILES]


def is_ok(path: Path) -> bool:
    try:
        c = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            r = c.execute("PRAGMA quick_check").fetchone()
            c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            c.close()
        return bool(r) and r[0] == "ok"
    except sqlite3.Error:
        return False


def copy_db(src: Path, dst: Path):
    """Πλήρες, συνεπές αντίγραφο με το backup API του SQLite (ασφαλές και όταν η βάση είναι ανοιχτή).
    Γράφεται πρώτα σε προσωρινό αρχείο και μετά αντικαθιστά το παλιό, ώστε να μη μείνει ποτέ μισό."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.unlink(missing_ok=True)
    s = sqlite3.connect(Path(src), timeout=30)
    try:
        d = sqlite3.connect(tmp)
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()
    os.replace(tmp, dst)


def _move_db(src: Path, dst: Path) -> bool:
    """Μεταφορά βάσης μαζί με τα βοηθητικά της αρχεία. Αν το OneDrive κρατά το αρχείο, αντιγράφεται."""
    try:
        for f, g in zip(_files(src), _files(dst)):
            if f.exists():
                shutil.move(str(f), str(g))
        return True
    except OSError:
        pass
    try:
        copy_db(src, dst)
        for f in _files(src):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        return True
    except (OSError, sqlite3.Error) as e:
        log.warning("Δεν μεταφέρθηκε η βάση %s: %s", src, e)
        return False


def place_live_db(data_dir: Path, app_dir: Path) -> tuple[Path, list[str]]:
    """Βρίσκει (και αν χρειάζεται μεταφέρει ή επαναφέρει) τη ζωντανή βάση. Επιστρέφει (διαδρομή, τι έγινε)."""
    data_dir, app_dir = Path(data_dir), Path(app_dir)
    events: list[str] = []
    if data_dir.resolve() == app_dir.resolve():  # φορητή χρήση / δοκιμές: όλα σε έναν φάκελο
        return data_dir / LIVE_NAME, events
    app_dir.mkdir(parents=True, exist_ok=True)
    live, old = app_dir / LIVE_NAME, data_dir / LIVE_NAME
    if old.exists():  # από v0.8.x: η βάση ήταν στα Έγγραφα
        if live.exists() and live.stat().st_mtime >= old.stat().st_mtime:
            # Η τοπική είναι νεότερη: η παλιά των Εγγράφων κρατιέται τοπικά μόνο ως παλιό αντίγραφο
            if _move_db(old, app_dir / f"pricetracker-παλιά-από-Έγγραφα-{datetime.now():%Y%m%d-%H%M%S}.db"):
                events.append("παλιά βάση Εγγράφων → AppData (παλιό αντίγραφο)")
        else:
            if live.exists():
                _move_db(live, app_dir / f"pricetracker-παλιότερη-{datetime.now():%Y%m%d-%H%M%S}.db")
            if _move_db(old, live):
                events.append("βάση Εγγράφων → AppData")
            else:
                return old, events  # δεν μεταφέρθηκε: συνεχίζει από τα Έγγραφα, ξαναδοκιμάζει την επόμενη φορά
    backup = data_dir / BACKUP_NAME
    if live.exists() and not is_ok(live):
        bad = app_dir / f"pricetracker-χαλασμένη-{datetime.now():%Y%m%d-%H%M%S}.db"
        _move_db(live, bad)
        events.append(f"η βάση ήταν χαλασμένη, κρατήθηκε ως {bad.name}")
    if not live.exists() and backup.exists() and is_ok(backup):
        copy_db(backup, live)
        events.append("επαναφορά από το Αντίγραφο βάσης")
    return live, events


def backup_if_due(live: Path, data_dir: Path, force: bool = False) -> bool:
    """Ημερήσιο αντίγραφο της βάσης στα Έγγραφα (κλειστό αρχείο, ασφαλές για το OneDrive)."""
    live, data_dir = Path(live), Path(data_dir)
    backup = data_dir / BACKUP_NAME
    if live.resolve() == backup.resolve() or live.parent.resolve() == data_dir.resolve():
        return False  # η βάση είναι ήδη στα Έγγραφα (φορητή χρήση): δεν χρειάζεται αντίγραφο
    if not force and backup.exists() and time.time() - backup.stat().st_mtime < BACKUP_EVERY_HOURS * 3600:
        return False
    if not live.exists():
        return False
    copy_db(live, backup)
    return True


def backup_before_update(live: Path, app_dir: Path, version: str) -> bool:
    """Πριν μια νέα έκδοση αλλάξει τη δομή της βάσης, κρατά ένα αντίγραφο (μόνο ένα, το τελευταίο)."""
    live = Path(live)
    if not live.exists():
        return False
    try:
        c = sqlite3.connect(live, timeout=30)
        try:
            r = c.execute("SELECT value FROM settings WHERE key='app_version'").fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        r = None
    prev = None
    if r and r[0]:
        import json

        try:
            prev = json.loads(r[0])
        except ValueError:
            prev = r[0]
    if prev == version:
        return False
    copy_db(live, Path(app_dir) / PRE_UPDATE_NAME)
    return True
