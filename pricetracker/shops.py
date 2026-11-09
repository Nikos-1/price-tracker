"""Προεπιλεγμένα καταστήματα και αναγνώριση καταστήματος από URL."""
from __future__ import annotations

from urllib.parse import urlparse

REGION_GR = "GR"
REGION_EU = "EU"
REGION_OTHER = "OTHER"

REGION_LABELS = {REGION_GR: "Ελλάδα", REGION_EU: "Ε.Ε.", REGION_OTHER: "Εκτός Ε.Ε."}

# Χώρες (κωδικός ISO → όνομα). Η περιοχή (Ελλάδα / Ε.Ε. / Εκτός Ε.Ε.) βγαίνει από τη χώρα.
COUNTRIES = {
    "GR": "Ελλάδα", "CY": "Κύπρος", "DE": "Γερμανία", "AT": "Αυστρία", "IT": "Ιταλία", "FR": "Γαλλία",
    "ES": "Ισπανία", "PT": "Πορτογαλία", "NL": "Ολλανδία", "BE": "Βέλγιο", "LU": "Λουξεμβούργο",
    "IE": "Ιρλανδία", "DK": "Δανία", "SE": "Σουηδία", "FI": "Φινλανδία", "PL": "Πολωνία", "CZ": "Τσεχία",
    "SK": "Σλοβακία", "SI": "Σλοβενία", "HR": "Κροατία", "HU": "Ουγγαρία", "RO": "Ρουμανία",
    "BG": "Βουλγαρία", "EE": "Εσθονία", "LV": "Λετονία", "LT": "Λιθουανία", "MT": "Μάλτα",
    "GB": "Ηνωμένο Βασίλειο", "US": "Η.Π.Α.", "CH": "Ελβετία", "NO": "Νορβηγία", "CN": "Κίνα",
    "JP": "Ιαπωνία", "CA": "Καναδάς", "TR": "Τουρκία", "": "Άγνωστη",
}
EU_COUNTRIES = {"CY", "DE", "AT", "IT", "FR", "ES", "PT", "NL", "BE", "LU", "IE", "DK", "SE", "FI", "PL", "CZ",
                "SK", "SI", "HR", "HU", "RO", "BG", "EE", "LV", "LT", "MT"}
# Ειδικές καταλήξεις domain → χώρα (οι υπόλοιπες βγαίνουν από το τελευταίο κομμάτι, π.χ. .de → DE)
TLD_COUNTRY = {"uk": "GB", "co.uk": "GB", "com.be": "BE", "eu": "", "com": "", "net": "", "org": "", "shop": ""}


def region_of_country(country: str) -> str:
    if country == "GR":
        return REGION_GR
    if country in EU_COUNTRIES:
        return REGION_EU
    return REGION_OTHER


def guess_country(host: str) -> str:
    parts = (host or "").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TLD_COUNTRY:
        return TLD_COUNTRY[".".join(parts[-2:])]
    tld = parts[-1].lower() if len(parts) > 1 else ""
    if tld in TLD_COUNTRY:
        return TLD_COUNTRY[tld]
    code = tld.upper()
    return code if code in COUNTRIES else ""


def country_label(code: str) -> str:
    return COUNTRIES.get(code or "", code or "")

# {q} αντικαθίσταται με το κείμενο αναζήτησης (URL-encoded).
# Τα URL αναζήτησης μπορούν να αλλάξουν από τις Ρυθμίσεις αν κάποιο κατάστημα αλλάξει.
# Προεπιλεγμένα καταστήματα που αφαιρέθηκαν σε νεότερη έκδοση (σβήνονται και από τις υπάρχουσες βάσεις)
REMOVED_DEFAULT_KEYS = ("mediamarkt_gr", "ebay_de")

def _shop(key, name, country, domain, search_url=""):
    return {"key": key, "name": name, "country": country, "region": region_of_country(country),
            "domain": domain, "search_url": search_url}


# Όσα δεν έχουν URL αναζήτησης, η εφαρμογή το βρίσκει μόνη της την πρώτη φορά (αυτόματη εύρεση).
# Νέα καταστήματα εδώ προστίθενται και στις υπάρχουσες εγκαταστάσεις με την επόμενη έκδοση,
# χωρίς να αλλάζουν ή να σβήνονται όσα έχει ήδη ο χρήστης.
DEFAULT_SHOPS = [
    # --- Ελλάδα ---
    _shop("bestprice", "BestPrice", "GR", "bestprice.gr", "https://www.bestprice.gr/search?q={q}"),
    _shop("plaisio", "Plaisio", "GR", "plaisio.gr", "https://www.plaisio.gr/search?q={q}"),
    _shop("kotsovolos", "Kotsovolos", "GR", "kotsovolos.gr", "https://www.kotsovolos.gr/search?query={q}"),
    _shop("public", "Public", "GR", "public.gr", "https://www.public.gr/search?text={q}"),
    _shop("eshop", "e-shop.gr", "GR", "e-shop.gr", "https://www.e-shop.gr/search?q={q}"),
    _shop("germanos", "Germanos", "GR", "germanos.gr"),
    _shop("electronet", "Electronet", "GR", "electronet.gr"),
    _shop("you_gr", "You.gr", "GR", "you.gr"),
    _shop("shopflix", "Shopflix", "GR", "shopflix.gr"),
    _shop("msystems", "MSystems", "GR", "msystems.gr"),
    _shop("stelioskanaras", "Stelios Kanaras", "GR", "stelioskanaras.gr"),
    # --- Ε.Ε. ---
    _shop("amazon_de", "Amazon.de", "DE", "amazon.de", "https://www.amazon.de/s?k={q}"),
    _shop("amazon_it", "Amazon.it", "IT", "amazon.it", "https://www.amazon.it/s?k={q}"),
    _shop("amazon_fr", "Amazon.fr", "FR", "amazon.fr", "https://www.amazon.fr/s?k={q}"),
    _shop("amazon_es", "Amazon.es", "ES", "amazon.es", "https://www.amazon.es/s?k={q}"),
    _shop("amazon_nl", "Amazon.nl", "NL", "amazon.nl", "https://www.amazon.nl/s?k={q}"),
    _shop("mindfactory", "Mindfactory", "DE", "mindfactory.de",
          "https://www.mindfactory.de/search_result.php?search_query={q}"),
    _shop("caseking", "Caseking", "DE", "caseking.de", "https://www.caseking.de/search?search={q}"),
    _shop("alternate_de", "Alternate", "DE", "alternate.de", "https://www.alternate.de/listing.xhtml?q={q}"),
    _shop("notebooksbilliger", "notebooksbilliger.de", "DE", "notebooksbilliger.de",
          "https://www.notebooksbilliger.de/produkte/{q}"),
    _shop("cyberport", "Cyberport", "DE", "cyberport.de"),
    _shop("computeruniverse", "computeruniverse", "DE", "computeruniverse.net"),
    _shop("proshop_de", "Proshop", "DE", "proshop.de"),
    _shop("pccomponentes", "PcComponentes", "ES", "pccomponentes.com", "https://www.pccomponentes.com/buscar/?query={q}"),
    _shop("ldlc", "LDLC", "FR", "ldlc.com"),
    # --- Εκτός Ε.Ε. (στέλνουν στην Ελλάδα· προσοχή σε δασμούς και ΦΠΑ στο τελωνείο) ---
    _shop("amazon_uk", "Amazon.co.uk", "GB", "amazon.co.uk", "https://www.amazon.co.uk/s?k={q}"),
    _shop("scan_uk", "Scan", "GB", "scan.co.uk", "https://www.scan.co.uk/search?q={q}"),
    _shop("overclockers_uk", "Overclockers UK", "GB", "overclockers.co.uk"),
    _shop("amazon_us", "Amazon.com", "US", "amazon.com", "https://www.amazon.com/s?k={q}"),
    _shop("bhphoto", "B&H Photo", "US", "bhphotovideo.com", "https://www.bhphotovideo.com/c/search?q={q}"),
    _shop("newegg", "Newegg", "US", "newegg.com", "https://www.newegg.com/p/pl?d={q}"),
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
    country = guess_country(host)
    if country:
        return region_of_country(country)
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    if tld == "gr":
        return REGION_GR
    if tld in EU_TLDS:
        return REGION_EU
    return REGION_OTHER


def guess_name(host: str) -> str:
    base = host.split(".")[0] if host else "?"
    return base[:1].upper() + base[1:]
