"""Εκκίνηση της εφαρμογής desktop: παράθυρο, system tray, server, έλεγχοι."""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__, dbfile
from .db import Database
from .web import create_app
from .updater import Updater
from .worker import Worker

APP_NAME = "Price Tracker"
PORT = int(os.environ.get("PRICETRACKER_PORT", "48231"))
log = logging.getLogger("pricetracker")


def resource_path(rel: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / rel


def documents_dir() -> Path:
    if sys.platform == "win32":
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(260)
            ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # CSIDL_PERSONAL
            if buf.value:
                return Path(buf.value)
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / "Documents"


def default_data_dir() -> Path:
    env = os.environ.get("PRICETRACKER_DATA")
    return Path(env) if env else documents_dir() / "PriceTracker"


# Στα Έγγραφα\PriceTracker μένουν μόνο: το ημερήσιο αντίγραφο της βάσης (Αντίγραφο βάσης.db), οι ρυθμίσεις
# (Ρυθμίσεις.json), τα προϊόντα (Προϊόντα\<ημερομηνία> <όνομα> με τα screenshots) και οι εγγυήσεις (Αποδείξεις).
# Η ζωντανή βάση και τα εσωτερικά αρχεία (logs, προφίλ browser, ενημερώσεις) μένουν στο %LOCALAPPDATA%\PriceTracker
# (δεν συγχρονίζεται με το OneDrive) και τα Excel/εξαγωγές πάνε στις Λήψεις\PriceTracker.
DB_NAMES = ("pricetracker.db", "pricetracker.db-wal", "pricetracker.db-shm")
INTERNAL_NAMES = ("app.log", "app.log.1", "app.log.2", "app.log.3", "webview", "browser-profile", "updates")
DISPOSABLE = INTERNAL_NAMES


def downloads_dir() -> Path:
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            import uuid

            guid = uuid.UUID("374DE290-123F-4565-9164-39C4925E467B")  # FOLDERID_Downloads
            buf = ctypes.c_wchar_p()
            GUID = ctypes.c_byte * 16
            if ctypes.windll.shell32.SHGetKnownFolderPath(GUID.from_buffer_copy(guid.bytes_le), 0, None,
                                                         ctypes.byref(buf)) == 0 and buf.value:
                path = Path(buf.value)
                ctypes.windll.ole32.CoTaskMemFree(buf)
                return path
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / "Downloads"


def default_exports_dir(data_dir: Path) -> Path:
    if os.environ.get("PRICETRACKER_DATA"):
        return data_dir / "exports"
    return downloads_dir() / "PriceTracker"


def default_app_dir(data_dir: Path) -> Path:
    if os.environ.get("PRICETRACKER_DATA"):
        return data_dir  # δοκιμές / φορητή χρήση: όλα σε έναν φάκελο
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "PriceTracker" if base else Path.home() / ".local" / "share" / "PriceTracker"


def move_internal_files(data_dir: Path, app_dir: Path, exports_dir: Path | None = None) -> list[str]:
    """Από παλιότερες εκδόσεις: εσωτερικά αρχεία έξω από τα Έγγραφα, τα Excel στις Λήψεις.
    Η βάση μεταφέρεται χωριστά (dbfile.place_live_db)."""
    import shutil

    moved = []
    data_dir, app_dir = Path(data_dir), Path(app_dir)
    if data_dir.resolve() == app_dir.resolve():
        return moved
    app_dir.mkdir(parents=True, exist_ok=True)
    old_exports = data_dir / "exports"
    if exports_dir is not None and old_exports.is_dir() and old_exports.resolve() != Path(exports_dir).resolve():
        try:
            Path(exports_dir).mkdir(parents=True, exist_ok=True)
            for f in old_exports.iterdir():
                dst = Path(exports_dir) / f.name
                if not dst.exists():
                    shutil.move(str(f), str(dst))
            if not any(old_exports.iterdir()):
                old_exports.rmdir()
            moved.append("exports")
        except OSError as e:
            print(f"Δεν μεταφέρθηκαν τα exports: {e}", file=sys.stderr)
    for name in INTERNAL_NAMES:
        src, dst = data_dir / name, app_dir / name
        if not src.exists():
            continue
        try:
            if not dst.exists():
                os.replace(src, dst)  # ίδιος δίσκος: απλή μετονομασία, χωρίς αντιγραφή
                moved.append(name)
                continue
        except OSError:
            pass
        # Δεν μετακινήθηκε (π.χ. το κλειδώνει το OneDrive) ή υπάρχει ήδη στον νέο φάκελο. Είναι προσωρινά αρχεία
        # (cache του browser, logs, ενημερώσεις), οπότε απλώς σβήνονται· ό,τι μείνει σβήνεται στην επόμενη εκκίνηση.
        remove_tree(src)
        if not src.exists():
            moved.append(name)
    return moved


def remove_tree(path: Path):
    from .productdirs import remove_tree as _rm

    _rm(path)


def setup_logging(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(data_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    if sys.stderr:
        root.addHandler(logging.StreamHandler())
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def tray_image():
    from PIL import Image

    p = resource_path("assets/icon.png")
    if p.exists():
        return Image.open(p)
    from PIL import ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), 14, fill=(37, 99, 235, 255))
    d.text((22, 16), "€", fill="white")
    return img


class Shell:
    """Γέφυρα ανάμεσα στο web περιβάλλον και στο παράθυρο / tray των Windows."""

    def __init__(self, db: Database):
        self.db = db
        self.window = None
        self.tray = None
        self.quitting = False
        self.on_quit = None
        self.hidden = False

    def show(self):
        self.hidden = False
        if self.window:
            self.window.show()
            try:
                self.window.restore()
            except Exception:  # noqa: BLE001
                pass

    def hide(self):
        if self.window and self.tray is None:
            # Χωρίς εικονίδιο στο tray το παράθυρο δεν κρύβεται ποτέ (θα γινόταν αόρατη η εφαρμογή):
            # ελαχιστοποιείται στη γραμμή εργασιών.
            log.warning("no tray icon, minimizing instead of hiding")
            try:
                self.window.minimize()
            except Exception:  # noqa: BLE001
                pass
            return
        self.hidden = True
        if self.window:
            self.window.hide()
            self.notify("Η εφαρμογή συνεχίζει στο system tray. Διπλό κλικ στο εικονίδιο για άνοιγμα.")

    def quit(self):
        self.quitting = True
        if self.on_quit:
            self.on_quit()

    def notify(self, msg: str):
        if self.tray is not None:
            try:
                self.tray.notify(msg, APP_NAME)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def open_path(path: Path):
        path = Path(path)
        if not path.suffix:
            path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    @staticmethod
    def set_autostart(enabled: bool):
        if sys.platform != "win32":
            return
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                             winreg.KEY_SET_VALUE)
        try:
            if enabled:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}" --tray'
                else:
                    pyw = Path(sys.executable).with_name("pythonw.exe")
                    exe = pyw if pyw.exists() else Path(sys.executable)
                    cmd = f'"{exe}" -m pricetracker --tray'
                winreg.SetValueEx(key, "PriceTracker", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, "PriceTracker")
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)


_INSTANCE_LOCK = None


def acquire_instance_lock(app_dir: Path) -> bool:
    """Μία μόνο εκτέλεση της εφαρμογής ανά φάκελο: δύο ταυτόχρονα θα έγραφαν στην ίδια βάση και στο ίδιο
    προφίλ browser («δεν βρέθηκε browser»). Το κλείδωμα λύνεται μόνο του όταν κλείσει η εφαρμογή."""
    global _INSTANCE_LOCK
    app_dir.mkdir(parents=True, exist_ok=True)
    f = open(app_dir / "instance.lock", "a+")  # noqa: SIM115 - μένει ανοιχτό όσο τρέχει η εφαρμογή
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _INSTANCE_LOCK = f
    return True


def already_running() -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/window/show", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "X-PriceTracker": "1"})
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def start_server(app):
    from werkzeug.serving import make_server

    srv = make_server("127.0.0.1", PORT, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, name="http", daemon=True)
    t.start()
    return srv


def start_tray(shell: Shell, worker: Worker, quit_cb):
    try:
        import pystray
    except Exception:  # noqa: BLE001
        log.warning("pystray not available, no tray icon")
        return None
    menu = pystray.Menu(
        pystray.MenuItem("Άνοιγμα", lambda: shell.show(), default=True),
        pystray.MenuItem("Έλεγχος τώρα", lambda: worker.enqueue("check_all")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Έξοδος", lambda: quit_cb()),
    )
    try:
        icon = pystray.Icon("PriceTracker", tray_image(), APP_NAME, menu)
        icon.run_detached()
        return icon
    except Exception:  # noqa: BLE001
        log.exception("tray icon failed")
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pricetracker")
    ap.add_argument("--tray", action="store_true", help="Εκκίνηση κρυφά στο system tray")
    ap.add_argument("--browser", action="store_true", help="Άνοιγμα στον browser αντί για παράθυρο")
    ap.add_argument("--no-check", action="store_true", help="Χωρίς έλεγχο στην εκκίνηση")
    ap.add_argument("--data", help="Φάκελος δεδομένων")
    args = ap.parse_args(argv)

    if already_running():
        return 0
    data_dir = Path(args.data) if args.data else default_data_dir()
    app_dir = data_dir if args.data else default_app_dir(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if not acquire_instance_lock(app_dir):
        already_running()  # εμφανίζει το παράθυρο της εκτέλεσης που τρέχει ήδη
        return 0
    exports_dir = data_dir / "exports" if args.data else default_exports_dir(data_dir)
    moved = move_internal_files(data_dir, app_dir, exports_dir)
    setup_logging(app_dir)
    for name in INTERNAL_NAMES:
        if (data_dir / name).exists() and data_dir != app_dir:
            log.warning("Έμεινε στα Έγγραφα (θα σβηστεί στην επόμενη εκκίνηση): %s", data_dir / name)
    if moved:
        log.info("Μεταφέρθηκαν (Έγγραφα / %s / %s): %s", app_dir, exports_dir, ", ".join(moved))
    db_path, db_events = dbfile.place_live_db(data_dir, app_dir)
    for e in db_events:
        log.warning("Βάση: %s", e)
    try:
        if dbfile.backup_before_update(db_path, app_dir, __version__):
            log.info("Αντίγραφο της βάσης πριν την ενημέρωση: %s", app_dir / dbfile.PRE_UPDATE_NAME)
    except Exception:  # noqa: BLE001
        log.exception("pre-update backup failed")
    db = Database(db_path)
    db.set_settings({"app_version": __version__})
    shell = Shell(db)
    worker = Worker(db, data_dir, notify_callback=shell.notify, app_dir=app_dir)
    worker.backup = lambda force=False: dbfile.backup_if_due(db_path, data_dir, force=force)
    shell.hidden = args.tray
    updater = Updater(db, app_dir, worker=worker, notify=shell.notify, request_quit=lambda: shell.quit(),
                      is_hidden=lambda: shell.hidden)
    app = create_app(db, worker, data_dir, shell=shell, updater=updater, exports_dir=exports_dir)
    srv = start_server(app)
    url = f"http://127.0.0.1:{PORT}/"
    log.info("Price Tracker ξεκίνησε, δεδομένα στο %s", data_dir)
    worker.start(check_on_start=not args.no_check)
    updater.start()

    stop_event = threading.Event()

    def do_quit():
        shell.quitting = True
        stop_event.set()
        if shell.window is not None:
            try:
                shell.window.destroy()
            except Exception:  # noqa: BLE001
                pass

    shell.on_quit = do_quit
    shell.tray = start_tray(shell, worker, do_quit)
    if shell.tray is None and args.tray and not args.browser:
        args.tray = False  # χωρίς εικονίδιο στο tray: το παράθυρο ανοίγει κανονικά, αλλιώς δεν θα φαινόταν πουθενά
        shell.hidden = False

    webview = None
    if not args.browser:
        try:
            import webview  # pywebview
        except Exception:  # noqa: BLE001
            log.warning("pywebview not available, opening browser")
    if webview is None:
        import webbrowser

        if not args.tray:
            webbrowser.open(url)
        shell.show = lambda: webbrowser.open(url)
        try:
            stop_event.wait()
        except KeyboardInterrupt:
            pass
    else:
        window = webview.create_window(APP_NAME, url, width=1280, height=850, min_size=(900, 600),
                                       hidden=args.tray, text_select=True)
        shell.window = window

        def on_closing():
            if shell.quitting:
                return True
            action = db.get_settings().get("close_action", "ask")
            if action == "exit":
                shell.quitting = True
                stop_event.set()
                return True
            if action == "tray" and shell.tray is not None:
                threading.Thread(target=shell.hide, daemon=True).start()
                return False
            threading.Thread(target=lambda: window.evaluate_js("window.showCloseDialog && window.showCloseDialog()"),
                             daemon=True).start()
            return False

        window.events.closing += on_closing
        webview.start(private_mode=False, storage_path=str(app_dir / "webview"))

    # ---- τερματισμός
    worker.stop()
    updater.stop()
    if shell.tray is not None:
        try:
            shell.tray.stop()
        except Exception:  # noqa: BLE001
            pass
    srv.shutdown()
    log.info("Price Tracker έκλεισε")
    return 0


if __name__ == "__main__":
    sys.exit(main())
