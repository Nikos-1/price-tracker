# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller PriceTracker.spec --noconfirm
from PyInstaller.utils.hooks import collect_all

datas = [("pricetracker/static", "pricetracker/static"), ("assets", "assets")]
binaries = []
hiddenimports = ["pystray._win32", "keyring.backends.Windows", "webview.platforms.winforms",
                 "webview.platforms.edgechromium"]
for pkg in ("playwright", "webview"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(["run.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=["tkinter", "pytest"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PriceTracker", console=False,
          icon="assets/icon.ico", upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="PriceTracker", upx=False)
