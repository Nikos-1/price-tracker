"""Ανάλυση ιστορικού τιμών: είναι πραγματική η προσφορά; Περίοδος Black Friday."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median


def black_friday(year: int) -> date:
    """Η Black Friday είναι η επόμενη μέρα της 4ης Πέμπτης του Νοεμβρίου."""
    d = date(year, 11, 1)
    first_thu = d + timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + timedelta(days=21 + 1)


def cyber_monday(year: int) -> date:
    return black_friday(year) + timedelta(days=3)


def bf_window(settings: dict, year: int) -> tuple[date, date]:
    if not settings.get("bf_window_auto", True) and settings.get("bf_start") and settings.get("bf_end"):
        sm, sd = map(int, str(settings["bf_start"]).split("-"))
        em, ed = map(int, str(settings["bf_end"]).split("-"))
        return date(year, sm, sd), date(year, em, ed)
    return date(year, 11, 20), date(year, 12, 2)


def in_bf_window(settings: dict, when: datetime | None = None) -> bool:
    when = when or datetime.now()
    start, end = bf_window(settings, when.year)
    return start <= when.date() <= end


def current_interval_hours(settings: dict, when: datetime | None = None) -> float:
    if in_bf_window(settings, when):
        return float(settings.get("bf_interval_hours") or 1)
    return float(settings.get("check_interval_hours") or 6)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def analyze(history: list[dict], now: datetime | None = None) -> dict:
    """history: γραμμές με checked_at, price, in_stock (ταξινομημένες). Επιστρέφει στατιστικά + ετυμηγορία."""
    now = now or datetime.now()
    pts = [(_parse(h["checked_at"]), h["price"]) for h in history if h.get("price")]
    if not pts:
        return {"verdict": "nodata", "label": "Δεν υπάρχουν τιμές ακόμα"}
    cur_t, cur = pts[-1]
    prior = [(t, p) for t, p in pts if t < cur_t - timedelta(hours=12)]
    p30 = [p for t, p in prior if t >= cur_t - timedelta(days=30)]
    p90 = [p for t, p in prior if t >= cur_t - timedelta(days=90)]
    all_prices = [p for _, p in pts]
    out = {
        "current": cur,
        "min_all": min(all_prices),
        "max_all": max(all_prices),
        "min_30": min(p30) if p30 else None,
        "median_30": round(median(p30), 2) if p30 else None,
        "max_30": max(p30) if p30 else None,
        "min_90": min(p90) if p90 else None,
        "days_tracked": (cur_t - pts[0][0]).days,
        "points": len(pts),
    }
    if out["days_tracked"] < 5 or len(p30) < 3:
        out.update(verdict="few", label="Λίγα δεδομένα ακόμα για σίγουρο συμπέρασμα")
        if out["min_30"] and cur < out["min_30"]:
            out["label"] += " (πάντως είναι η χαμηλότερη τιμή που έχει καταγραφεί)"
        return out
    med = out["median_30"]
    pct = round((med - cur) / med * 100, 1) if med else 0
    out["pct_below_median_30"] = pct
    pct_s = str(abs(pct)).replace(".", ",")
    # Ανέβηκε η τιμή τις τελευταίες 2-3 εβδομάδες πάνω από τη συνήθη, πριν «πέσει»;
    recent = [p for t, p in prior if t >= cur_t - timedelta(days=21)]
    older = [p for t, p in prior if cur_t - timedelta(days=90) <= t < cur_t - timedelta(days=21)]
    inflated = bool(recent and older and max(recent) > median(older) * 1.08)
    if cur < out["min_30"] and (not out["min_90"] or cur <= out["min_90"]):
        out.update(verdict="real", label=f"Πραγματική προσφορά: χαμηλότερη τιμή 90 ημερών ({pct_s}% κάτω από τη συνήθη)")
    elif cur < out["min_30"]:
        out.update(verdict="good", label="Καλή τιμή: χαμηλότερη 30 ημερών, αλλά έχει υπάρξει και φθηνότερη")
    elif inflated and cur >= (min(older) if older else cur):
        out.update(verdict="fake", label="Ύποπτη προσφορά: η τιμή ανέβηκε πρόσφατα πριν την «έκπτωση»")
    elif pct >= 5:
        out.update(verdict="good", label=f"Καλή τιμή: {pct_s}% κάτω από τη συνήθη τιμή 30 ημερών")
    elif pct <= -5:
        out.update(verdict="high", label=f"Ακριβότερη από τη συνήθη ({pct_s}% πάνω)")
    else:
        out.update(verdict="normal", label="Συνηθισμένη τιμή, όχι ιδιαίτερη προσφορά")
    return out
