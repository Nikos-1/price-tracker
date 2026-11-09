@echo off
REM Φτιάχνει το dist\PriceTracker\PriceTracker.exe
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\pyinstaller PriceTracker.spec --noconfirm
echo.
echo Έτοιμο: dist\PriceTracker\PriceTracker.exe
pause
