"""Υπολογισμοί εγγύησης."""
from __future__ import annotations

import calendar
from datetime import date


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def warranty_info(product: dict, today: date | None = None) -> dict:
    today = today or date.today()
    months = product.get("warranty_months")
    bought = product.get("purchased_at")
    if not months or not bought:
        return {"warranty_end": None, "warranty_days_left": None, "warranty_state": "none"}
    try:
        start = date.fromisoformat(str(bought)[:10])
    except ValueError:
        return {"warranty_end": None, "warranty_days_left": None, "warranty_state": "none"}
    end = add_months(start, int(months))
    left = (end - today).days
    state = "expired" if left < 0 else "soon" if left <= 60 else "active"
    return {"warranty_end": end.isoformat(), "warranty_days_left": left, "warranty_state": state}


def months_label(months: int | None) -> str:
    if not months:
        return "Χωρίς εγγύηση"
    y, m = divmod(int(months), 12)
    parts = []
    if y:
        parts.append(f"{y} {'έτος' if y == 1 else 'έτη'}")
    if m:
        parts.append(f"{m} {'μήνας' if m == 1 else 'μήνες'}")
    return " και ".join(parts)
