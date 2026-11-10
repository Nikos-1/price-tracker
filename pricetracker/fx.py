"""Ισοτιμίες για μετατροπή τιμών σε €.

Πηγή: οι επίσημες ημερήσιες ισοτιμίες αναφοράς της Ευρωπαϊκής Κεντρικής Τράπεζας (ΕΚΤ), δωρεάν και χωρίς
λογαριασμό. Ενημερώνονται μία φορά την εργάσιμη μέρα (γύρω στις 16:00 ώρα Ελλάδας). Η εφαρμογή τις ξαναδιαβάζει
σε κάθε έλεγχο τιμών αν έχει περάσει πάνω από μία ώρα, και κρατάει τις τελευταίες για όταν δεν υπάρχει σύνδεση.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request

from . import __version__

log = logging.getLogger(__name__)

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FALLBACK_URL = "https://api.frankfurter.app/latest?from=EUR"  # τα ίδια στοιχεία της ΕΚΤ σε JSON
MAX_AGE_S = 3600

SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "CNY": "¥", "CHF": "CHF", "PLN": "zł",
           "CZK": "Kč", "SEK": "kr", "DKK": "kr", "NOK": "kr", "HUF": "Ft", "RON": "lei", "BGN": "лв",
           "CAD": "C$", "AUD": "A$", "TRY": "₺"}


def parse_ecb_xml(text: str) -> tuple[str | None, dict[str, float]]:
    date = re.search(r"time=['\"](\d{4}-\d{2}-\d{2})['\"]", text or "")
    rates = {m.group(1): float(m.group(2))
             for m in re.finditer(r"currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([\d.]+)['\"]", text or "")}
    return (date.group(1) if date else None), rates


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"PriceTracker/{__version__}"})
    with urllib.request.urlopen(req, timeout=8) as r:  # χωρίς δίκτυο να μην κολλά το παράθυρο
        return r.read().decode("utf-8")


def fetch_rates() -> tuple[str | None, dict[str, float]]:
    try:
        date, rates = parse_ecb_xml(_get(ECB_URL))
        if rates:
            return date, rates
    except Exception as e:  # noqa: BLE001
        log.info("ECB rates failed: %s", e)
    data = json.loads(_get(FALLBACK_URL))
    return data.get("date"), {k: float(v) for k, v in (data.get("rates") or {}).items()}


def get_rates(db, max_age_s: int = MAX_AGE_S, fetch=fetch_rates, force: bool = False) -> dict:
    """{"date": "YYYY-MM-DD", "ts": epoch, "rates": {"USD": 1.08, ...}} (1 € = rate νομίσματος)."""
    cached = db.get_settings().get("fx") or {}
    if not force and cached.get("rates") and time.time() - float(cached.get("ts") or 0) < max_age_s:
        return cached
    try:
        date, rates = fetch()
        if rates:
            fresh = {"date": date, "ts": time.time(), "rates": rates}
            db.set_settings({"fx": fresh})
            return fresh
    except Exception as e:  # noqa: BLE001
        log.info("rates refresh failed: %s", e)
    return cached


def to_eur(amount, currency, fx: dict | None) -> float | None:
    if amount is None or not currency:
        return None  # άγνωστο νόμισμα
    cur = currency.upper()
    if cur == "EUR":
        return float(amount)
    rate = ((fx or {}).get("rates") or {}).get(cur)
    if not rate:
        return None
    return round(float(amount) / rate, 2)


def fmt(amount, currency="EUR") -> str:
    """1234.5, "USD" → "1.234,50 $" (ελληνική μορφή)."""
    if amount is None:
        return "-"
    s = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {SYMBOLS.get((currency or 'EUR').upper(), currency)}"


def fmt_with_eur(amount, currency, amount_eur) -> str:
    """"450,00 $ / ≈401,28 €" για ξένο νόμισμα, αλλιώς σκέτο "401,28 €"."""
    if amount is None:
        return "-"
    if not currency:
        return fmt(amount, "EUR")[:-2] + " (άγνωστο νόμισμα)"
    if currency.upper() == "EUR" or amount_eur is None:
        return fmt(amount, currency)
    return f"{fmt(amount, currency)} / ≈{fmt(amount_eur)}"
