"""Προεπιλεγμένα καταστήματα και αναγνώριση καταστήματος από URL."""
from __future__ import annotations

from urllib.parse import urlparse

REGION_GR = "GR"
REGION_EU = "EU"
REGION_OTHER = "OTHER"

REGION_LABELS = {REGION_GR: "Ελλάδα", REGION_EU: "Ε.Ε.", REGION_OTHER: "Άλλο"}

# {q} αντικαθίσταται με το κείμενο αναζήτησης (URL-encoded).
# Τα URL αναζήτησης μπορούν να αλλάξουν από τις Ρυθμίσεις αν κάποιο κατάστημα αλλάξει.
# Προεπιλεγμένα καταστήματα που αφαιρέθηκαν σε νεότερη έκδοση (σβήνονται και από τις υπάρχουσες βάσεις)
REMOVED_DEFAULT_KEYS = ("mediamarkt_gr", "amazon_de", "amazon_it", "amazon_fr", "amazon_es", "ebay_de")

DEFAULT_SHOPS = [
    # --- Ελλάδα ---
    {"key": "bestprice", "name": "BestPrice", "region": REGION_GR, "domain": "bestprice.gr",
     "search_url": "https://www.bestprice.gr/search?q={q}"},
    {"key": "plaisio", "name": "Plaisio", "region": REGION_GR, "domain": "plaisio.gr",
     "search_url": "https://www.plaisio.gr/search?q={q}"},
    {"key": "kotsovolos", "name": "Kotsovolos", "region": REGION_GR, "domain": "kotsovolos.gr",
     "search_url": "https://www.kotsovolos.gr/search?query={q}"},
    {"key": "public", "name": "Public", "region": REGION_GR, "domain": "public.gr",
     "search_url": "https://www.public.gr/search?text={q}"},
    {"key": "eshop", "name": "e-shop.gr", "region": REGION_GR, "domain": "e-shop.gr",
     "search_url": "https://www.e-shop.gr/search?q={q}"},
    # --- Ε.Ε. ---
    {"key": "mindfactory", "name": "Mindfactory", "region": REGION_EU, "domain": "mindfactory.de",
     "search_url": "https://www.mindfactory.de/search_result.php?search_query={q}"},
    {"key": "caseking", "name": "Caseking", "region": REGION_EU, "domain": "caseking.de",
     "search_url": "https://www.caseking.de/search?search={q}"},
]

EU_TLDS = {
    "at", "be", "bg", "hr", "cy", "cz", "dk", "ee", "fi", "fr", "de", "hu", "ie", "it",
    "lv", "lt", "lu", "mt", "nl", "pl", "pt", "ro", "sk", "si", "es", "se", "eu",
}


def host_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def guess_region(host: str) -> str:
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    if tld == "gr":
        return REGION_GR
    if tld in EU_TLDS:
        return REGION_EU
    return REGION_OTHER


def guess_name(host: str) -> str:
    base = host.split(".")[0] if host else "?"
    return base[:1].upper() + base[1:]
