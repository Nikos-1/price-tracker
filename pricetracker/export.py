"""Εξαγωγή σε Excel (.xlsx) με σύνοψη, πλήρες ιστορικό και γράφημα ανά προϊόν."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import analysis, warranty
from .shops import REGION_LABELS

HEADER_FILL = PatternFill("solid", fgColor="1F2937")
HEADER_FONT = Font(color="FFFFFF", bold=True)
VERDICT_LABELS = {"real": "Πραγματική", "good": "Καλή", "fake": "Ύποπτη", "normal": "Συνηθισμένη",
                  "high": "Ακριβή", "few": "Λίγα δεδομένα", "nodata": "-"}


def _header(ws, cols):
    ws.append(cols)
    for c in ws[1]:
        c.fill, c.font = HEADER_FILL, HEADER_FONT
        c.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"


def _autowidth(ws, maxw=60):
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(10, width + 2), maxw)


def _sheet_name(name: str, used: set) -> str:
    base = re.sub(r"[\[\]:*?/\\]", "", name)[:28] or "Προϊόν"
    n, i = base, 2
    while n in used:
        n = f"{base[:25]}~{i}"
        i += 1
    used.add(n)
    return n


def export_xlsx(db, data_dir: Path, out_path: Path, product_ids: list[int] | None = None) -> Path:
    products = db.list_products()
    if product_ids:
        products = [p for p in products if p["id"] in product_ids]
    wb = Workbook()
    ws = wb.active
    ws.title = "Σύνοψη"
    _header(ws, ["Προϊόν", "Κατηγορία", "Κατάσταση", "Κατάστημα", "Περιοχή", "Τρέχουσα τιμή", "Διαθέσιμο",
                 "Χαμηλότερη 30 ημ.", "Συνήθης 30 ημ.", "Χαμηλότερη ποτέ", "Τιμή-στόχος", "Εκτίμηση προσφοράς",
                 "Τελευταίος έλεγχος", "Link"])
    hist_ws = wb.create_sheet("Ιστορικό")
    _header(hist_ws, ["Ημερομηνία", "Προϊόν", "Κατηγορία", "Κατάστημα", "Περιοχή", "Τιμή", "Νόμισμα",
                      "Διαθέσιμο", "Σφάλμα", "Screenshot", "Link"])
    used = {"Σύνοψη", "Ιστορικό"}
    for p in products:
        listings = db.list_listings(product_id=p["id"], status="active")
        status = "Αγοράστηκε" if p["purchased"] else "Παρακολούθηση"
        for l in listings:
            h = db.history(listing_id=l["id"])
            a = analysis.analyze(h)
            ws.append([p["name"], p.get("category_name") or "", status, l.get("shop_name"),
                       REGION_LABELS.get(l.get("region"), ""), l.get("last_price"),
                       "Ναι" if l.get("last_in_stock") else "Όχι", a.get("min_30"), a.get("median_30"),
                       a.get("min_all"), p.get("target_price"), a.get("label", ""), l.get("last_checked"),
                       l["url"]])
            ws.cell(ws.max_row, 14).hyperlink = l["url"]
            for r in h:
                hist_ws.append([datetime.fromisoformat(r["checked_at"]), p["name"], p.get("category_name") or "",
                                l.get("shop_name"), REGION_LABELS.get(l.get("region"), ""), r["price"],
                                r["currency"], None if r["in_stock"] is None else ("Ναι" if r["in_stock"] else "Όχι"),
                                r["error"] or "", r["screenshot"] or "", l["url"]])
                if r["screenshot"]:
                    cell = hist_ws.cell(hist_ws.max_row, 10)
                    cell.hyperlink = (Path(data_dir) / r["screenshot"]).as_uri()
                    cell.font = Font(color="2563EB", underline="single")
                hist_ws.cell(hist_ws.max_row, 1).number_format = "dd/mm/yyyy hh:mm"
        _product_sheet(wb, db, p, listings, used)
    purchased = [p for p in db.list_purchased() if not product_ids or p["id"] in product_ids]
    if purchased:
        wws = wb.create_sheet("Εγγυήσεις", 2)
        _header(wws, ["Προϊόν", "Κατηγορία", "Ημερομηνία αγοράς", "Κατάστημα", "Τιμή", "Εγγύηση", "Λήξη εγγύησης",
                      "Μέρες που απομένουν", "Απόδειξη"])
        for p in purchased:
            w = warranty.warranty_info(p)
            wws.append([p["name"], p.get("category_name") or "", p.get("purchased_at"), p.get("purchased_shop"),
                        p.get("purchased_price"), warranty.months_label(p.get("warranty_months")),
                        w["warranty_end"], w["warranty_days_left"], p.get("receipt_name") or ""])
            if p.get("receipt_file"):
                c = wws.cell(wws.max_row, 9)
                c.hyperlink = (Path(data_dir) / p["receipt_file"]).as_uri()
                c.font = Font(color="2563EB", underline="single")
        used.add("Εγγυήσεις")
        _autowidth(wws)
        for row in wws.iter_rows(min_row=2, min_col=5, max_col=5):
            for c in row:
                if isinstance(c.value, float):
                    c.number_format = "#,##0.00 €"
    for sheet in (ws, hist_ws):
        _autowidth(sheet)
        for row in sheet.iter_rows(min_row=2):
            for c in row:
                if isinstance(c.value, float):
                    c.number_format = "#,##0.00 €"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def _product_sheet(wb, db, product, listings, used):
    """Φύλλο με πίνακα ημερομηνία × κατάστημα και γράφημα γραμμών."""
    hist = db.history(product_id=product["id"])
    hist = [h for h in hist if h["price"] and any(l["id"] == h["listing_id"] for l in listings)]
    if not hist:
        return
    ws = wb.create_sheet(_sheet_name(product["name"], used))
    cols = {l["id"]: i + 2 for i, l in enumerate(listings)}
    ws.append(["Ημερομηνία"] + [f"{l.get('shop_name')} ({REGION_LABELS.get(l.get('region'), '')})" for l in listings])
    for c in ws[1]:
        c.fill, c.font = HEADER_FILL, HEADER_FONT
    by_time: dict[str, dict] = {}
    for h in hist:
        key = h["checked_at"][:13]  # ανά ώρα
        by_time.setdefault(key, {})[h["listing_id"]] = h["price"]
    for key in sorted(by_time):
        row = [datetime.fromisoformat(key + ":00")] + [None] * len(listings)
        for lid, price in by_time[key].items():
            row[cols[lid] - 1] = price
        ws.append(row)
        ws.cell(ws.max_row, 1).number_format = "dd/mm/yyyy hh:mm"
    for row in ws.iter_rows(min_row=2, min_col=2):
        for c in row:
            if c.value is not None:
                c.number_format = "#,##0.00 €"
    _autowidth(ws, 30)
    ch = LineChart()
    ch.title = product["name"][:60]
    ch.y_axis.title = "Τιμή (€)"
    ch.x_axis.title = "Ημερομηνία"
    ch.x_axis.number_format = "dd/mm"
    ch.height, ch.width = 10, 24
    data = Reference(ws, min_col=2, max_col=1 + len(listings), min_row=1, max_row=ws.max_row)
    ch.add_data(data, titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=ws.max_row))
    ws.add_chart(ch, f"{get_column_letter(len(listings) + 3)}2")


# ---------------------------------------------------------------- ρυθμίσεις και καταστήματα
SETTING_LABELS = {
    "check_interval_hours": "Έλεγχος κάθε (ώρες)",
    "bf_interval_hours": "Έλεγχος κάθε (ώρες) στην περίοδο Black Friday",
    "bf_window_auto": "Περίοδος Black Friday αυτόματα (20/11 έως 2/12)",
    "bf_start": "Αρχή περιόδου Black Friday (ΜΜ-ΗΗ)",
    "bf_end": "Τέλος περιόδου Black Friday (ΜΜ-ΗΗ)",
    "screenshot_mode": "Screenshots",
    "browser_mode": "Παράθυρο browser κατά τους ελέγχους",
    "email_enabled": "Αποστολή email",
    "smtp_provider": "Πάροχος email",
    "smtp_host": "SMTP server",
    "smtp_port": "Θύρα SMTP",
    "smtp_user": "Λογαριασμός αποστολής",
    "email_to": "Αποστολή ειδοποιήσεων σε",
    "notify_back_in_stock": "Ειδοποίηση όταν γίνει διαθέσιμο",
    "notify_target_price": "Ειδοποίηση όταν φτάσει την τιμή-στόχο",
    "notify_new_low": "Ειδοποίηση για νέα χαμηλότερη τιμή",
    "close_action": "Όταν πατάω το Χ",
    "start_with_windows": "Εκκίνηση μαζί με τα Windows",
    "auto_update": "Αυτόματη ενημέρωση",
    "warranty_reminder_days": "Υπενθύμιση πριν λήξει η εγγύηση (μέρες)",
}
VALUE_LABELS = {
    "screenshot_mode": {"changes": "Όταν αλλάζει τιμή ή διαθεσιμότητα", "always": "Σε κάθε έλεγχο"},
    "browser_mode": {"auto": "Κρυφό, ανοίγει μόνο όταν χρειάζεται ενέργεια", "hidden": "Πάντα κρυφό",
                     "visible": "Πάντα ορατό"},
    "close_action": {"ask": "Να ρωτάει", "tray": "Να μένει στο system tray", "exit": "Να κλείνει"},
    "smtp_provider": {"gmail": "Gmail", "outlook": "Outlook / Hotmail", "custom": "Άλλο (SMTP)"},
}


def _setting_value(key, value):
    if isinstance(value, bool):
        return "Ναι" if value else "Όχι"
    return VALUE_LABELS.get(key, {}).get(value, value)


def export_settings_xlsx(settings: dict, shops: list[dict], out_path: Path) -> Path:
    """Φύλλα «Ρυθμίσεις» και «Καταστήματα». Ο κωδικός email δεν γράφεται ποτέ."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Ρυθμίσεις"
    _header(ws, ["Ρύθμιση", "Τιμή"])
    for key, label in SETTING_LABELS.items():
        if key in settings:
            ws.append([label, _setting_value(key, settings[key])])
    ws.append([])
    ws.append([f"Εξαγωγή: {datetime.now():%d/%m/%Y %H:%M}. Ο κωδικός email δεν περιλαμβάνεται."])
    _autowidth(ws, 70)

    ws = wb.create_sheet("Καταστήματα")
    _header(ws, ["Όνομα", "Διεύθυνση", "Περιοχή", "URL αναζήτησης", "Αναζήτηση", "Προϊόντα"])
    for s in shops:
        ws.append([s["name"], s["domain"], REGION_LABELS.get(s["region"], s["region"]), s.get("search_url") or "",
                   "Ναι" if s.get("search_enabled") else "Όχι", s.get("used", 0)])
    ws.auto_filter.ref = ws.dimensions
    _autowidth(ws, 80)
    wb.save(out_path)
    return out_path
