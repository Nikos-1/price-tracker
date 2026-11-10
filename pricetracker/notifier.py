"""Ειδοποιήσεις με email (Gmail / Outlook / άλλο SMTP)."""
from __future__ import annotations

import html
import logging
import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from . import fx

log = logging.getLogger(__name__)

KEYRING_SERVICE = "PriceTracker-SMTP"

SMTP_PRESETS = {
    "gmail": {"smtp_host": "smtp.gmail.com", "smtp_port": 587},
    "outlook": {"smtp_host": "smtp-mail.outlook.com", "smtp_port": 587},
}


def _keyring():
    try:
        import keyring  # Windows Credential Manager

        return keyring
    except Exception:  # noqa: BLE001
        return None


def _host(db) -> str:
    return (db.get_settings().get("smtp_host") or "smtp.gmail.com").strip().lower()


def save_password(db, user: str, password: str):
    """Ο κωδικός μένει ΜΟΝΟ στο Windows Credential Manager, ποτέ σε απλό κείμενο στη βάση (που
    συγχρονίζεται στο OneDrive). Δένεται με τον SMTP server: αν αλλάξει ο server, ο κωδικός δεν
    στέλνεται εκεί μέχρι να τον ξαναγράψεις."""
    kr = _keyring()
    if kr is None:
        raise RuntimeError("Δεν είναι διαθέσιμο το Windows Credential Manager, ο κωδικός δεν αποθηκεύτηκε")
    try:
        kr.set_password(KEYRING_SERVICE, user, password)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Ο κωδικός δεν αποθηκεύτηκε στο Windows Credential Manager: {e}") from e
    db.set_settings({"smtp_password_plain": "", "smtp_password_host": _host(db)})
    return "keyring"


def load_password(db, user: str) -> str:
    settings = db.get_settings()
    bound = settings.get("smtp_password_host")
    host = _host(db)
    if bound is None:  # από παλιότερη έκδοση: δένεται με τον server που υπάρχει τώρα
        db.set_settings({"smtp_password_host": host})
    elif bound != host:
        log.warning("SMTP server changed since the password was saved; not sending the password")
        return ""
    kr = _keyring()
    plain = settings.get("smtp_password_plain") or ""
    if kr is not None:
        try:
            pw = kr.get_password(KEYRING_SERVICE, user)
            if pw:
                return pw
            if plain:  # παλιός κωδικός σε απλό κείμενο: μεταφορά στο Credential Manager
                kr.set_password(KEYRING_SERVICE, user, plain)
                db.set_settings({"smtp_password_plain": ""})
                return plain
        except Exception:  # noqa: BLE001
            pass
    return plain


def has_password(db, user: str) -> bool:
    return bool(user and load_password(db, user))


def send_email(settings: dict, password: str, subject: str, text: str, html_body: str | None = None,
               attachments: list[Path] | None = None):
    to = settings.get("email_to") or settings.get("smtp_user")
    user = settings.get("smtp_user")
    if not (user and password and to):
        raise RuntimeError("Λείπουν στοιχεία email (χρήστης, κωδικός εφαρμογής ή παραλήπτης)")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Price Tracker <{user}>"
    msg["To"] = to
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    total = 0
    for p in attachments or []:
        p = Path(p)
        if not p.exists():
            continue
        data = p.read_bytes()
        total += len(data)
        if total > 18 * 1024 * 1024:
            break
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=p.name)
    host = settings.get("smtp_host") or "smtp.gmail.com"
    port = int(settings.get("smtp_port") or 587)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)


def format_alerts(alerts: list[dict]) -> tuple[str, str, str]:
    """alerts: [{kind, product, shop, region, price, currency, url, target, screenshot}]"""
    kinds = {"in_stock": "Διαθέσιμο ξανά", "target": "Έφτασε την τιμή-στόχο", "new_low": "Νέα χαμηλότερη τιμή"}
    if len(alerts) == 1:
        a = alerts[0]
        subject = (f"[Price Tracker] {kinds.get(a['kind'], 'Ειδοποίηση')}: {a['product']} – "
                   f"{fx.fmt_with_eur(a.get('price'), a.get('currency'), a.get('price_eur'))} ({a['shop']})")
    else:
        subject = f"[Price Tracker] {len(alerts)} ειδοποιήσεις για τα προϊόντα σου"
    lines, rows = [], []
    for a in alerts:
        price = fx.fmt_with_eur(a.get("price"), a.get("currency"), a.get("price_eur"))
        extra = f" (στόχος {fx.fmt(a['target'])})" if a.get("target") else ""
        lines.append(f"• {kinds.get(a['kind'], a['kind'])}: {a['product']} – {price}{extra} στο {a['shop']} "
                     f"[{a.get('region_label', '')}]\n  {a['url']}")
        rows.append(
            "<tr>"
            f"<td style='padding:6px 10px'><b>{html.escape(kinds.get(a['kind'], a['kind']))}</b></td>"
            f"<td style='padding:6px 10px'>{html.escape(a['product'])}</td>"
            f"<td style='padding:6px 10px'>{html.escape(a['shop'])} ({html.escape(a.get('region_label', ''))})</td>"
            f"<td style='padding:6px 10px;color:#15803d'><b>{html.escape(price)}</b>{html.escape(extra)}</td>"
            f"<td style='padding:6px 10px'><a href='{html.escape(a['url'])}'>Άνοιγμα</a></td>"
            "</tr>"
        )
    text = "Ο Price Tracker βρήκε:\n\n" + "\n\n".join(lines) + "\n\nΤα screenshots είναι συνημμένα."
    body = ("<div style='font-family:Segoe UI,Arial,sans-serif'><h2>Ο Price Tracker βρήκε:</h2>"
            "<table style='border-collapse:collapse' border='1' cellspacing='0'>" + "".join(rows) +
            "</table><p style='color:#6b7280'>Τα screenshots είναι συνημμένα.</p></div>")
    return subject, text, body
