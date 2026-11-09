@echo off
REM Εκκίνηση από τον κώδικα (χωρίς .exe). Την πρώτη φορά εγκαθιστά ό,τι χρειάζεται.
cd /d "%~dp0"
if not exist .venv (
  echo Δημιουργία περιβάλλοντος Python...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
start "" .venv\Scripts\pythonw.exe -m pricetracker %*
