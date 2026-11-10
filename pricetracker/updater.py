"""Αυτόματη ενημέρωση της εφαρμογής από τα GitHub Releases του repo."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

REPO = "Nikos-1/price-tracker"
DEFAULT_API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
# Άλλη πηγή ενημερώσεων μόνο για δοκιμές από τον κώδικα, ποτέ στο εγκατεστημένο PriceTracker.exe
API_URL = (DEFAULT_API_URL if getattr(sys, "frozen", False)
           else os.environ.get("PRICETRACKER_UPDATE_URL", DEFAULT_API_URL))
ASSET_NAME = "PriceTracker-windows.zip"
CHECK_EVERY_HOURS = 12

SETUP_ASSET = "PriceTracker-Setup.exe"

# Τρέχει αφού κλείσει η εφαρμογή: είτε το πρόγραμμα εγκατάστασης αθόρυβα, είτε αντιγραφή των αρχείων (zip).
PS_SCRIPT = r"""
param([int]$ProcId, [string]$Mode, [string]$Src, [string]$Dst, [string]$Exe, [string]$ExeArgs, [string]$Log)
$ErrorActionPreference = 'Continue'
"$(Get-Date -Format s) Αναμονή για κλείσιμο της εφαρμογής ($ProcId), τρόπος: $Mode" | Out-File -Append -Encoding utf8 $Log
try { Wait-Process -Id $ProcId -Timeout 90 -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Seconds 2
if ($Mode -eq 'setup') {
  $p = Start-Process -FilePath $Src -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CURRENTUSER',"/DIR=`"$Dst`"" -Wait -PassThru
  "$(Get-Date -Format s) setup exit $($p.ExitCode)" | Out-File -Append -Encoding utf8 $Log
} else {
  robocopy $Src $Dst /E /R:10 /W:2 /NFL /NDL /NJH /NJS /NP | Out-File -Append -Encoding utf8 $Log
  "$(Get-Date -Format s) robocopy exit $LASTEXITCODE" | Out-File -Append -Encoding utf8 $Log
}
if ($ExeArgs) { Start-Process -FilePath $Exe -ArgumentList $ExeArgs } else { Start-Process -FilePath $Exe }
"""


def parse_version(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    return Path(sys.executable).parent


def installed_with_setup() -> bool:
    """Εγκατεστημένο με το PriceTracker-Setup.exe (υπάρχει πρόγραμμα απεγκατάστασης δίπλα στο exe)."""
    return any(install_dir().glob("unins*.exe"))


class Updater:
    def __init__(self, db, data_dir: Path, worker=None, notify=None, request_quit=None, is_hidden=None):
        self.db = db
        self.dir = Path(data_dir) / "updates"
        self.worker = worker
        self.notify = notify or (lambda msg: None)
        self.request_quit = request_quit
        self.is_hidden = is_hidden or (lambda: False)
        self._lock = threading.Lock()
        self._busy = False
        self.state = {"supported": is_frozen(), "current": __version__, "latest": None, "available": False,
                      "state": "idle", "message": "", "checked_at": None, "notes": ""}
        self._release = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ δημόσια
    def status(self) -> dict:
        return dict(self.state)

    def start(self):
        threading.Thread(target=self._loop, name="updater", daemon=True).start()

    def stop(self):
        self._stop.set()

    def check_async(self, force=False):
        threading.Thread(target=self._safe, args=(self.check_and_maybe_install, force), daemon=True).start()

    def install_async(self):
        threading.Thread(target=self._safe, args=(self._install_now,), daemon=True).start()

    # ------------------------------------------------------------------ εσωτερικά
    def _loop(self):
        if self._stop.wait(45):  # άφησε την εφαρμογή να ξεκινήσει πρώτα
            return
        while not self._stop.is_set():
            self._safe(self.check_and_maybe_install)
            if self._stop.wait(CHECK_EVERY_HOURS * 3600):
                return

    def _safe(self, fn, *args):
        with self._lock:
            if self._busy:
                return
            self._busy = True
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            log.exception("update failed")
            self.state.update(state="error", message=f"Η ενημέρωση απέτυχε: {e}")
        finally:
            self._busy = False

    def fetch_latest(self) -> dict | None:
        req = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json",
                                                       "User-Agent": f"PriceTracker/{__version__}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
        if ASSET_NAME not in assets and SETUP_ASSET not in assets:
            return None
        return {"version": data.get("tag_name", "").lstrip("v"), "url": assets.get(ASSET_NAME),
                "sha_url": assets.get(ASSET_NAME + ".sha256"), "setup_url": assets.get(SETUP_ASSET),
                "setup_sha_url": assets.get(SETUP_ASSET + ".sha256"), "notes": data.get("body") or ""}

    def check(self) -> bool:
        self.state.update(state="checking", message="Έλεγχος για νέα έκδοση…")
        rel = self.fetch_latest()
        self.state["checked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        if not rel:
            self.state.update(state="idle", message="Δεν βρέθηκε έκδοση για λήψη")
            return False
        self.state["latest"] = rel["version"]
        newer = parse_version(rel["version"]) > parse_version(__version__)
        self.state.update(available=newer, notes=rel["notes"][:2000])
        self._release = rel if newer else None
        self.state.update(state="idle", message=(f"Υπάρχει νέα έκδοση {rel['version']}" if newer
                                                  else "Έχεις την τελευταία έκδοση"))
        return newer

    def check_and_maybe_install(self, force=False):
        if not self.check():
            return
        if not is_frozen():
            self.state["message"] += " (η αυτόματη εγκατάσταση γίνεται μόνο στο PriceTracker.exe)"
            return
        if self.db.get_settings().get("auto_update", True):
            self._install_now()
        else:
            self.notify(f"Υπάρχει νέα έκδοση {self._release['version']}. Εγκατάσταση από τις Ρυθμίσεις.")

    def _fetch(self, url: str, sha_url: str | None, dest: Path) -> Path:
        if not sha_url:  # χωρίς έλεγχο ακεραιότητας δεν εγκαθίσταται τίποτα
            raise RuntimeError("Η νέα έκδοση δεν έχει αρχείο ελέγχου (.sha256), δεν εγκαθίσταται")
        self.dir.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": f"PriceTracker/{__version__}"})
        with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        req = urllib.request.Request(sha_url, headers={"User-Agent": f"PriceTracker/{__version__}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            expected = r.read().decode().split()[0].strip().lower()
        actual = hashlib.sha256(dest.read_bytes()).hexdigest()
        if actual != expected:
            dest.unlink(missing_ok=True)
            raise RuntimeError("Το αρχείο της ενημέρωσης δεν πέρασε τον έλεγχο ακεραιότητας")
        return dest

    def download_setup(self, rel: dict) -> Path:
        self.state.update(state="downloading", message=f"Λήψη έκδοσης {rel['version']}…")
        return self._fetch(rel["setup_url"], rel.get("setup_sha_url"),
                           self.dir / f"PriceTracker-Setup-{rel['version']}.exe")

    def download(self, rel: dict) -> Path:
        if not rel.get("url"):
            raise RuntimeError("Η νέα έκδοση δεν έχει αρχείο zip")
        self.state.update(state="downloading", message=f"Λήψη έκδοσης {rel['version']}…")
        zpath = self._fetch(rel["url"], rel.get("sha_url"), self.dir / f"PriceTracker-{rel['version']}.zip")
        target = self.dir / f"PriceTracker-{rel['version']}"
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        with zipfile.ZipFile(zpath) as z:
            for m in z.namelist():  # προστασία από διαδρομές έξω από τον φάκελο
                if m.startswith(("/", "\\")) or ".." in Path(m).parts:
                    raise RuntimeError("Μη έγκυρο αρχείο ενημέρωσης")
            z.extractall(target)
        zpath.unlink(missing_ok=True)
        if not (target / "PriceTracker.exe").exists():
            raise RuntimeError("Το πακέτο της ενημέρωσης δεν περιέχει το PriceTracker.exe")
        return target

    def _install_now(self):
        rel = self._release
        if rel is None and not self.check():
            return
        rel = self._release
        if not is_frozen():
            raise RuntimeError("Η αυτόματη εγκατάσταση γίνεται μόνο στο PriceTracker.exe")
        use_setup = installed_with_setup() and rel.get("setup_url")
        new_path = self.download_setup(rel) if use_setup else self.download(rel)
        # Περίμενε να τελειώσει τυχόν έλεγχος τιμών
        while self.worker is not None and (self.worker.status.get("running") or self.worker.pending()):
            self.state.update(state="waiting", message="Η ενημέρωση περιμένει να τελειώσει ο τρέχων έλεγχος…")
            time.sleep(10)
        self.state.update(state="installing", message=f"Εγκατάσταση έκδοσης {rel['version']}…")
        self.notify(f"Ενημέρωση σε έκδοση {rel['version']}. Η εφαρμογή θα ξανανοίξει σε λίγα δευτερόλεπτα.")
        self.launch_installer(new_path, "setup" if use_setup else "copy")
        time.sleep(2)
        if self.request_quit:
            self.request_quit()

    def launch_installer(self, src: Path, mode: str = "copy"):
        target = install_dir()
        script = self.dir / "apply_update.ps1"
        script.write_text(PS_SCRIPT, encoding="utf-8-sig")
        args = "--tray" if self.is_hidden() else ""
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
               "-File", str(script), "-ProcId", str(os.getpid()), "-Mode", mode, "-Src", str(src),
               "-Dst", str(target), "-Exe", str(target / "PriceTracker.exe"),
               "-Log", str(self.dir / "update.log")]
        if args:  # τελευταίο, ώστε ένα κενό όρισμα να μη «φάει» το επόμενο
            cmd += ["-ExeArgs", args]
        flags = 0x08000000  # CREATE_NO_WINDOW: κανένα παράθυρο κονσόλας
        subprocess.Popen(cmd, creationflags=flags, close_fds=True)
