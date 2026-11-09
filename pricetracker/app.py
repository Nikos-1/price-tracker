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


# Τα εσωτερικά αρχεία της εφαρμογής (βάση, logs, προφίλ browser, ενημερώσεις) μένουν στο %LOCALAPPDATA%\PriceTracker.
# Στα Έγγραφα\PriceTracker μένουν μόνο όσα χρειάζεται να ανοίγει ο χρήστης: αποδείξεις, screenshots και τα Excel.
INTERNAL_NAMES = ("pricetracker.db", "pricetracker.db-wal", "pricetracker.db-shm", "app.log", "app.log.1",
                  "app.log.2", "app.log.3", "webview", "browser-profile", "updates")
DISPOSABLE = ("app.log", "app.log.1", "app.log.2", "app.log.3", "updates", "webview", "browser-profile")


def default_app_dir(data_dir: Path) -> Path:
    if os.environ.get("PRICETRACKER_DATA"):
        return data_dir  # δοκιμές / φορητή χρήση: όλα σε έναν φάκελο
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "PriceTracker" if base else Path.home() / ".local" / "share" / "PriceTracker"


def move_internal_files(data_dir: Path, app_dir: Path) -> list[str]:
    """Από παλιότερες εκδόσεις: μεταφέρει τα εσωτερικά αρχεία από τα Έγγραφα στον φάκελο της εφαρμογής."""
    import shutil

    moved = []
    if Path(data_dir).resolve() == Path(app_dir).resolve():
        return moved
    app_dir.mkdir(parents=True, exist_ok=True)
    for name in INTERNAL_NAMES:
        src, dst = data_dir / name, app_dir / name
        if not src.exists():
            continue
        try:
            if not dst.exists():
                shutil.move(str(src), str(dst))
                moved.append(name)
            elif name in DISPOSABLE:  # υπάρχει ήδη νεότερο στον νέο φάκελο· το παλιό δεν χρειάζεται
                shutil.rmtree(src) if src.is_dir() else src.unlink()
        except OSError as e:
            print(f"Δεν μεταφέρθηκε το {src}: {e}", file=sys.stderr)
    return moved


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


def already_running() -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/window/show", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
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
    moved = move_internal_files(data_dir, app_dir)
    setup_logging(app_dir)
    if moved:
        log.info("Μεταφέρθηκαν από τα Έγγραφα στο %s: %s", app_dir, ", ".join(moved))
    db = Database(app_dir / "pricetracker.db")
    shell = Shell(db)
    worker = Worker(db, data_dir, notify_callback=shell.notify, app_dir=app_dir)
    shell.hidden = args.tray
    updater = Updater(db, app_dir, worker=worker, notify=shell.notify, request_quit=lambda: shell.quit(),
                      is_hidden=lambda: shell.hidden)
    app = create_app(db, worker, data_dir, shell=shell, updater=updater)
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
