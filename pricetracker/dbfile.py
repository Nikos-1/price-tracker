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


def _ro_uri(path: Path) -> str:
    # as_uri() κωδικοποιεί σωστά %, #, ?, κενά και ελληνικά (π.χ. C:\\Users\\Νίκος%20Σ\\...)
    return Path(path).resolve().as_uri() + "?mode=ro"


def is_ok(path: Path) -> bool:
    """True αν η βάση ανοίγει και περνά τον γρήγορο έλεγχο ακεραιότητας. Μια βάση που απλώς είναι
    κλειδωμένη (π.χ. την κρατά για λίγο το OneDrive ή το antivirus) ΔΕΝ θεωρείται χαλασμένη."""
    path = Path(path)
    if not path.is_file():
        return False
    try:
        c = sqlite3.connect(_ro_uri(path), uri=True, timeout=10)
        try:
            r = c.execute("PRAGMA quick_check").fetchone()
            c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            c.close()
        return bool(r) and r[0] == "ok"
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            return True
        return False
    except sqlite3.Error:
        return False


def copy_db(src: Path, dst: Path):
    """Πλήρες, συνεπές αντίγραφο με το backup API του SQLite (ασφαλές και όταν η βάση είναι ανοιχτή).
    Γράφεται πρώτα σε προσωρινό αρχείο και μετά αντικαθιστά το παλιό, ώστε να μη μείνει ποτέ μισό.
    Αν λείπει η πηγή, σφάλμα: ποτέ «αντίγραφο» μιας άδειας βάσης πάνω σε μια σωστή."""
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        raise FileNotFoundError(f"Δεν υπάρχει η βάση {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        s = sqlite3.connect(src, timeout=30)  # όχι μόνο-ανάγνωση: έτσι ένα journal από κρασάρισμα ολοκληρώνεται πρώτα
        try:
            d = sqlite3.connect(tmp)
            try:
                s.backup(d)
            finally:
                d.close()
        finally:
            s.close()
        if not is_ok(tmp):
            raise sqlite3.DatabaseError("Το αντίγραφο της βάσης δεν πέρασε τον έλεγχο")
        for attempt in range(3):  # το OneDrive μπορεί να κρατά για λίγο το παλιό αντίγραφο
            try:
                os.replace(tmp, dst)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(1)
    finally:
        try:
            tmp.unlink(missing_ok=True)  # να μη μένουν .tmp στο OneDrive
        except OSError:
            pass


def _move_db(src: Path, dst: Path) -> bool:
    """Ασφαλής μεταφορά βάσης: πλήρες αντίγραφο σε προσωρινό αρχείο, έλεγχος, και μόνο στο τέλος
    σβήσιμο της παλιάς μαζί με τα βοηθητικά της αρχεία. Αν κάτι αποτύχει, η παλιά μένει ανέγγιχτη."""
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        return False
    try:
        copy_db(src, dst)
    except (OSError, sqlite3.Error) as e:
        log.warning("Δεν μεταφέρθηκε η βάση %s: %s", src, e)
        return False
    for f in _files(src):
        try:
            f.unlink(missing_ok=True)
        except OSError as e:  # π.χ. το κρατά το OneDrive: μένει, η επόμενη εκκίνηση το βλέπει ως παλιό αντίγραφο
            log.warning("Δεν σβήστηκε το %s: %s", f, e)
    return True


def _set_aside(src: Path, dst: Path) -> bool:
    """Παραμερίζει (μετονομάζει) μια χαλασμένη βάση και τα βοηθητικά της αρχεία, χωρίς αντιγραφή."""
    try:
        os.replace(src, dst)
    except OSError as e:
        log.warning("Δεν παραμερίστηκε η χαλασμένη βάση %s: %s", src, e)
        return False
    for f, g in zip(_files(src)[1:], _files(dst)[1:]):
        try:
            if f.exists():
                os.replace(f, g)
        except OSError:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
    return True


# Μηνύματα που πρέπει να δει ο χρήστης (μπαίνουν στο «Υγεία»), όχι μόνο στο log
PROBLEM_PREFIX = "!"


def problems(events: list[str]) -> list[str]:
    return [e[len(PROBLEM_PREFIX):] for e in events if e.startswith(PROBLEM_PREFIX)]


def place_live_db(data_dir: Path, app_dir: Path) -> tuple[Path, list[str]]:
    """Βρίσκει (και αν χρειάζεται μεταφέρει ή επαναφέρει) τη ζωντανή βάση. Επιστρέφει (διαδρομή, τι έγινε).
    Τα γεγονότα που αρχίζουν με «!» είναι προβλήματα για το «Υγεία» της εφαρμογής."""
    data_dir, app_dir = Path(data_dir), Path(app_dir)
    events: list[str] = []
    if data_dir.resolve() == app_dir.resolve():  # φορητή χρήση / δοκιμές: όλα σε έναν φάκελο
        return data_dir / LIVE_NAME, events
    app_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    live, old = app_dir / LIVE_NAME, data_dir / LIVE_NAME
    if old.exists():  # από v0.8.x: η βάση ήταν στα Έγγραφα
        if not is_ok(old):
            # Χαλασμένη παλιά βάση στα Έγγραφα: δεν τη χρησιμοποιούμε, κρατιέται για κάθε ενδεχόμενο
            bad = app_dir / f"pricetracker-χαλασμένη-από-Έγγραφα-{stamp}.db"
            try:
                shutil.move(str(old), str(bad))
                for f in _files(old)[1:]:
                    f.unlink(missing_ok=True)
                events.append(f"η παλιά βάση των Εγγράφων ήταν χαλασμένη, κρατήθηκε ως {bad.name}")
            except OSError as e:
                log.warning("Δεν μετακινήθηκε η χαλασμένη βάση %s: %s", old, e)
        elif live.exists() and live.stat().st_mtime >= old.stat().st_mtime:
            # Η τοπική είναι νεότερη: η παλιά των Εγγράφων κρατιέται τοπικά μόνο ως παλιό αντίγραφο
            if _move_db(old, app_dir / f"pricetracker-παλιά-από-Έγγραφα-{stamp}.db"):
                events.append("παλιά βάση Εγγράφων → AppData (παλιό αντίγραφο)")
        else:
            if live.exists() and is_ok(live):
                if not _move_db(live, app_dir / f"pricetracker-παλιότερη-{stamp}.db"):
                    return live, events  # δεν κρατήθηκε αντίγραφο της τοπικής: δεν την αντικαθιστούμε
            if _move_db(old, live):
                events.append("βάση Εγγράφων → AppData")
            elif live.exists():
                return live, events
            else:
                return old, events  # δεν μεταφέρθηκε: συνεχίζει από τα Έγγραφα, ξαναδοκιμάζει την επόμενη φορά
    backup = data_dir / BACKUP_NAME
    if live.exists() and not is_ok(live):
        bad = app_dir / f"pricetracker-χαλασμένη-{stamp}.db"
        if _set_aside(live, bad):
            events.append(f"{PROBLEM_PREFIX}Η βάση είχε χαλάσει και κρατήθηκε ως «{bad.name}» στο {app_dir}.")
        else:
            events.append(f"{PROBLEM_PREFIX}Η βάση έχει χαλάσει και δεν μπόρεσε να παραμεριστεί. "
                          "Κλείσε την εφαρμογή, κάνε επανεκκίνηση του υπολογιστή και ξανάνοιξέ την.")
            return live, events
    if not live.exists():
        if backup.exists() and is_ok(backup):
            try:
                copy_db(backup, live)
                when = datetime.fromtimestamp(backup.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
                events.append(f"{PROBLEM_PREFIX}Έγινε επαναφορά της βάσης από το «{BACKUP_NAME}» της {when}. "
                              "Ό,τι άλλαξε μετά από τότε δεν υπάρχει.")
            except (OSError, sqlite3.Error) as e:
                log.warning("Η επαναφορά από το αντίγραφο απέτυχε: %s", e)
                events.append(f"{PROBLEM_PREFIX}Η βάση δεν βρέθηκε και η επαναφορά από το «{BACKUP_NAME}» "
                              f"απέτυχε ({e}). Η εφαρμογή ξεκίνησε με κενή βάση.")
        elif any(app_dir.glob("pricetracker-χαλασμένη-*.db")) or backup.exists():
            events.append(f"{PROBLEM_PREFIX}Η βάση δεν βρέθηκε ή είχε χαλάσει και δεν υπάρχει σωστό αντίγραφο. "
                          "Η εφαρμογή ξεκίνησε με κενή βάση.")
    return live, events


def _computer() -> str:
    return os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or ""


def backup_if_due(live: Path, data_dir: Path, force: bool = False) -> bool:
    """Ημερήσιο αντίγραφο της βάσης στα Έγγραφα (κλειστό αρχείο, ασφαλές για το OneDrive).
    Αν το ίδιο αντίγραφο το γράφει και άλλος υπολογιστής (κοινό OneDrive), σφάλμα με εξήγηση."""
    live, data_dir = Path(live), Path(data_dir)
    backup = data_dir / BACKUP_NAME
    if live.resolve() == backup.resolve() or live.parent.resolve() == data_dir.resolve():
        return False  # η βάση είναι ήδη στα Έγγραφα (φορητή χρήση): δεν χρειάζεται αντίγραφο
    if not force and backup.exists() and time.time() - backup.stat().st_mtime < BACKUP_EVERY_HOURS * 3600:
        return False
    if not live.exists():
        return False
    other = _backup_computer(backup)
    copy_db(live, backup)
    _mark_backup_computer(backup)
    if other and _computer() and other != _computer():
        raise RuntimeError(f"Το «{BACKUP_NAME}» το έγραφε και ο υπολογιστής «{other}». Με κοινό OneDrive, "
                           "κάθε υπολογιστής πρέπει να έχει δικό του φάκελο PriceTracker στα Έγγραφα.")
    return True


def _backup_computer(backup: Path) -> str:
    if not backup.is_file():
        return ""
    try:
        c = sqlite3.connect(_ro_uri(backup), uri=True, timeout=5)
        try:
            r = c.execute("SELECT value FROM settings WHERE key='backup_computer'").fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return ""
    if not r or not r[0]:
        return ""
    import json

    try:
        return str(json.loads(r[0]))
    except ValueError:
        return str(r[0])


def _mark_backup_computer(backup: Path):
    import json

    try:
        c = sqlite3.connect(backup, timeout=5)
        try:
            c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('backup_computer', ?)",
                      (json.dumps(_computer()),))
            c.commit()
        finally:
            c.close()
    except sqlite3.Error:
        pass


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
