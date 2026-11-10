"""Εξαγωγή τιμής, διαθεσιμότητας και στοιχείων προϊόντος από HTML σελίδας."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .shops import host_of


@dataclass
class ProductInfo:
    title: str | None = None
    price: float | None = None
    currency: str | None = None
    in_stock: bool | None = None
    ean: str | None = None
    mpn: str | None = None
    brand: str | None = None
    image: str | None = None
    blocked: str | None = None  # π.χ. captcha
    source: list[str] = field(default_factory=list)

    def merge(self, other: "ProductInfo", tag: str):
        changed = False
        for k in ("title", "price", "currency", "in_stock", "ean", "mpn", "brand", "image"):
            if getattr(self, k) is None and getattr(other, k) is not None:
                setattr(self, k, getattr(other, k))
                changed = True
        if changed:
            self.source.append(tag)


CURRENCY_SYMBOLS = {"€": "EUR", "eur": "EUR", "£": "GBP", "$": "USD", "zł": "PLN", "pln": "PLN",
                    "kč": "CZK", "czk": "CZK", "sek": "SEK", "kr": "SEK", "chf": "CHF", "ron": "RON",
                    "lei": "RON", "huf": "HUF", "ft": "HUF", "bgn": "BGN", "лв": "BGN", "dkk": "DKK"}


# Νομίσματα με ισοτιμία της ΕΚΤ: μόνο αυτά δέχεται η εφαρμογή ως κωδικό νομίσματος
ISO_CODES = {"EUR", "USD", "JPY", "BGN", "CZK", "DKK", "GBP", "HUF", "PLN", "RON", "SEK", "CHF", "ISK", "NOK",
             "TRY", "AUD", "BRL", "CAD", "CNY", "HKD", "IDR", "ILS", "INR", "KRW", "MXN", "MYR", "NZD", "PHP",
             "SGD", "THB", "ZAR"}
_ALPHA_SYMBOLS = {k: v for k, v in CURRENCY_SYMBOLS.items() if k.isalpha()}
_SIGN_SYMBOLS = {k: v for k, v in CURRENCY_SYMBOLS.items() if not k.isalpha()}


def normalize_currency(value) -> str | None:
    """«€», «eur», «EUR» → «EUR». Ό,τι δεν είναι γνωστό νόμισμα → None (θα ρωτηθεί ο χρήστης)."""
    if not value:
        return None
    v = str(value).strip()
    code = CURRENCY_SYMBOLS.get(v.lower()) or v.upper()
    return code if code in ISO_CODES else None


_SPACES = re.compile(r"[\s\u00a0\u1680\u2000-\u200b\u202f\u205f\u3000]+")
_NUM = r"\d+(?:[ .,'’]\d{3}(?!\d))*(?:[.,]\d{1,2}(?!\d))?"
_SYM = r"€|EUR|£|GBP|\$|USD|zł|PLN|Kč|CZK|SEK|CHF|lei|RON|Ft|HUF|лв|BGN|DKK|kr"
_PCT = re.compile(r"[-−–]?\s*\d+(?:[.,]\d+)?\s*%")


def parse_price(text) -> float | None:
    """Μετατρέπει «1.234,56 €», «€1,234.56», «1 234,56», «349» σε float.
    Αγνοεί ποσοστά («-20%») και προτιμά τον αριθμό που έχει δίπλα του νόμισμα («Από 3 δόσεις των 99 €»)."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    s = _SPACES.sub(" ", str(text)).strip()
    s = _PCT.sub(" ", s)
    m = re.search(rf"(?:{_SYM})\s?({_NUM})|({_NUM})\s?(?:,-)?\s?(?:{_SYM})", s, re.I)
    if m:
        num = m.group(1) or m.group(2)
    else:
        m = re.search(_NUM, s)
        if not m:
            return None
        num = m.group(0)
    num = num.strip().replace(" ", "").replace("'", "").replace("’", "")
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num or "." in num:
        sep = "," if "," in num else "."
        head, _, tail = num.rpartition(sep)
        if num.count(sep) > 1 or len(tail) == 3:
            num = num.replace(sep, "")          # διαχωριστικό χιλιάδων: 1.234 / 1,234
        else:
            num = head + "." + tail             # δεκαδικά: 12,50 / 12.50
    try:
        v = float(num)
    except ValueError:
        return None
    return v if v > 0 else None


# Νόμισμα όταν η σελίδα δεν το λέει: από τη διεύθυνση του καταστήματος
HOST_CURRENCY = {".co.uk": "GBP", ".uk": "GBP", "amazon.com": "USD", "bhphotovideo.com": "USD",
                 "newegg.com": "USD", ".us": "USD", ".ch": "CHF", ".pl": "PLN", ".cz": "CZK", ".se": "SEK",
                 ".dk": "DKK", ".hu": "HUF", ".ro": "RON", ".bg": "BGN"}


EURO_TLDS = {"gr", "de", "it", "fr", "es", "nl", "be", "at", "ie", "fi", "pt", "sk", "si", "ee", "lv", "lt",
             "lu", "mt", "cy", "hr", "bg", "eu"}


def currency_for_host(host: str) -> str | None:
    """Νόμισμα από τη διεύθυνση, όταν η σελίδα δεν το λέει. None = άγνωστο (π.χ. .com): θα ρωτηθεί ο χρήστης."""
    host = (host or "").lower()
    for suffix, code in HOST_CURRENCY.items():
        if suffix.startswith("."):
            if host.endswith(suffix):
                return code
        elif host == suffix or host.endswith("." + suffix):
            return code
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    return "EUR" if tld in EURO_TLDS else None


def detect_currency(text, default=None) -> str | None:
    """Νόμισμα από κείμενο τιμής. Οι λέξεις (eur, kr, ft…) μετράνε μόνο ως ξεχωριστές λέξεις, ώστε το «left»
    να μη διαβάζεται ως φιορίνι και το «incl. VAT» ως νόμισμα «VAT»."""
    if not text:
        return default
    t = _SPACES.sub(" ", str(text))
    low = t.lower()
    for sym, code in _SIGN_SYMBOLS.items():
        if sym in low:
            return code
    for sym, code in _ALPHA_SYMBOLS.items():
        if re.search(rf"(?<![^\W\d_]){re.escape(sym)}(?![^\W\d_])", low):
            return code
    for m in re.finditer(r"\b([A-Z]{3})\b", t):
        if m.group(1) in ISO_CODES:
            return m.group(1)
    return default


AVAIL_IN = ["instock", "in_stock", "limitedavailability", "onlineonly", "instoreonly"]
AVAIL_OUT = ["outofstock", "out_of_stock", "soldout", "discontinued", "preorder", "presale",
             "backorder"]


def availability_from_schema(value) -> bool | None:
    if not value:
        return None
    v = str(value).lower().rsplit("/", 1)[-1]
    if any(x == v for x in AVAIL_IN):
        return True
    if any(x == v for x in AVAIL_OUT):
        return False
    return None


OUT_PHRASES = [
    "μη διαθέσιμο", "εξαντλήθηκε", "εξαντλημένο", "δεν είναι διαθέσιμο", "προσωρινά μη διαθέσιμο",
    "out of stock", "currently unavailable", "sold out", "temporarily unavailable",
    "derzeit nicht verfügbar", "nicht verfügbar", "nicht auf lager", "ausverkauft",
    "non disponibile", "attualmente non disponibile", "esaurito",
    "actuellement indisponible", "rupture de stock", "épuisé",
    "no disponible", "agotado", "temporalmente sin stock",
    "dieses angebot wurde beendet", "this listing has ended", "angebot beendet",
]
IN_PHRASES = [
    "άμεσα διαθέσιμο", "διαθέσιμο", "σε απόθεμα", "παράδοση σε", "προσθήκη στο καλάθι", "στο καλάθι",
    "in stock", "add to cart", "auf lager", "in den einkaufswagen", "sofort lieferbar",
    "disponibilità immediata", "disponibile", "aggiungi al carrello", "en stock",
    "ajouter au panier", "añadir a la cesta", "sofort versandfertig",
]


def availability_from_text(text: str) -> bool | None:
    if not text:
        return None
    t = " ".join(text.lower().split())
    for p in OUT_PHRASES:
        if p in t:
            return False
    for p in IN_PHRASES:
        if p in t:
            return True
    return None


def _phrases_in(text: str):
    """(βρέθηκε «μη διαθέσιμο», βρέθηκε «διαθέσιμο») χωρίς να μετρά το «διαθέσιμο» μέσα στο «μη διαθέσιμο»."""
    t = " ".join((text or "").lower().split())
    out = any(p in t for p in OUT_PHRASES)
    for p in sorted(OUT_PHRASES, key=len, reverse=True):
        t = t.replace(p, " ")
    return out, any(p in t for p in IN_PHRASES)


CART_WORDS = ["add to cart", "add to basket", "προσθήκη στο καλάθι", "στο καλάθι", "αγορά τώρα", "buy now",
              "in den warenkorb", "aggiungi al carrello", "ajouter au panier", "añadir a la cesta",
              "añadir al carrito", "do koszyka", "do košíku", "lägg i varukorgen"]
PRODUCT_AREAS = ["[itemtype*='schema.org/Product']", "main .product", "#product", ".product-info", ".product-details",
                 ".product-main", ".product-page", "main"]


def availability_from_page(soup) -> bool | None:
    """Διαθεσιμότητα όταν δεν τη λέει η σελίδα με δομημένα δεδομένα. Ένα ενεργό κουμπί «στο καλάθι» μετράει
    περισσότερο από λέξεις κάπου στη σελίδα (π.χ. «Εξαντλήθηκε» σε σχετικά προϊόντα)."""
    disabled_cart = False
    for b in soup.select("button, input[type=submit], a[class*=cart], a[class*=basket]"):
        label = " ".join([_text(b), str(b.get("value") or ""), str(b.get("aria-label") or ""),
                          str(b.get("title") or "")]).lower()
        if any(w in label for w in CART_WORDS):
            disabled = b.has_attr("disabled") or str(b.get("aria-disabled")).lower() == "true" \
                or "disabled" in " ".join(b.get("class") or []).lower()
            if not disabled:
                return True
            disabled_cart = True
    for sel in PRODUCT_AREAS:
        el = soup.select_one(sel)
        if el is None:
            continue
        out, inn = _phrases_in(_text(el)[:8000])
        if out and disabled_cart:  # «Εξαντλήθηκε» και ανενεργό κουμπί καλαθιού
            return False
        if out != inn:
            return inn
        break
    body = soup.body.get_text(" ") if soup.body else ""
    out, inn = _phrases_in(body[:20000])
    if out != inn:  # μόνο όταν η σελίδα λέει καθαρά το ένα από τα δύο
        return inn
    return None


# ---------------------------------------------------------------- JSON-LD
def _iter_jsonld(soup, deep: bool = True):
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except ValueError:
                continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                yield d
                # Στο πρώτο πέρασμα όχι στις λίστες (σχετικά προϊόντα, breadcrumbs): μόνο το κύριο προϊόν
                for k in ("@graph", "mainEntity", "hasVariant") + (("itemListElement",) if deep else ()):
                    if k in d:
                        stack.append(d[k])


def _types(d):
    t = d.get("@type")
    if isinstance(t, list):
        return [str(x).lower() for x in t]
    return [str(t).lower()] if t else []


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def from_jsonld(soup) -> ProductInfo:
    info = _from_jsonld(soup, deep=False)
    if info.price is None:
        info.merge(_from_jsonld(soup, deep=True), "jsonld")
    return info


def _offer_price(o: dict):
    """Η τιμή μιας προσφοράς. Από τα priceSpecification αγνοεί την παλιά/διαγραμμένη τιμή (StrikethroughPrice)."""
    if "aggregateoffer" in _types(o):
        return parse_price(o.get("lowPrice") or o.get("price"))
    price = parse_price(o.get("price"))
    if price is None and isinstance(o.get("priceSpecification"), (dict, list)):
        specs = o["priceSpecification"] if isinstance(o["priceSpecification"], list) else [o["priceSpecification"]]
        for ps in specs:
            if not isinstance(ps, dict):
                continue
            kind = str(ps.get("priceType") or "").lower()
            if any(k in kind for k in ("strikethrough", "listprice", "msrp", "srp")):
                continue
            price = parse_price(ps.get("price"))
            if price is not None:
                break
    return price


def _from_jsonld(soup, deep: bool) -> ProductInfo:
    info = ProductInfo()
    for d in _iter_jsonld(soup, deep):
        if not any(t in ("product", "productgroup", "individualproduct") for t in _types(d)):
            continue
        p = ProductInfo()
        p.title = _first(d.get("name"))
        brand = _first(d.get("brand"))
        p.brand = brand.get("name") if isinstance(brand, dict) else brand
        p.ean = next((str(_first(d.get(k))) for k in ("gtin13", "gtin", "gtin14", "gtin12", "gtin8", "ean")
                      if d.get(k)), None)
        p.mpn = str(_first(d.get("mpn"))) if d.get("mpn") else None
        img = _first(d.get("image"))
        p.image = img.get("url") if isinstance(img, dict) else img
        offers = d.get("offers")
        offer_list = offers if isinstance(offers, list) else [offers] if offers else []
        cands = []
        for o in offer_list:
            if not isinstance(o, dict):
                continue
            price = _offer_price(o)
            avail = availability_from_schema(o.get("availability"))
            if "aggregateoffer" in _types(o) and avail is None and (o.get("offerCount") or 0):
                avail = True
            if price is not None:
                cands.append((price, o.get("priceCurrency"), avail))
        # Η φθηνότερη προσφορά που ΜΠΟΡΕΙΣ να αγοράσεις· μια φθηνή αλλά εξαντλημένη δεν είναι «σε απόθεμα»
        buyable = [c for c in cands if c[2] is not False]
        pool = buyable or cands
        if pool:
            best = min(pool, key=lambda c: c[0])
            p.price, p.currency = best[0], best[1]
            p.in_stock = best[2] if best[2] is not None else (True if any(c[2] for c in cands) else None)
        info.merge(p, "jsonld")
        if info.price is not None:
            break
    return info


# ---------------------------------------------------------------- meta / microdata
def from_meta(soup) -> ProductInfo:
    p = ProductInfo()

    def meta(*names):
        for n in names:
            t = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) \
                or soup.find("meta", attrs={"itemprop": n})
            if t and t.get("content"):
                return t["content"].strip()
        return None

    p.title = meta("og:title", "twitter:title")
    p.price = parse_price(meta("product:price:amount", "og:price:amount", "price"))
    p.currency = meta("product:price:currency", "og:price:currency", "priceCurrency")
    p.in_stock = availability_from_schema(meta("product:availability", "og:availability", "availability"))
    p.image = meta("og:image")
    p.ean = meta("gtin13", "gtin")
    p.mpn = meta("mpn")
    if p.price is None:
        el = soup.find(attrs={"itemprop": "price"})
        if el is not None:
            p.price = parse_price(el.get("content") or el.get_text())
    if p.in_stock is None:
        el = soup.find(attrs={"itemprop": "availability"})
        if el is not None:
            p.in_stock = availability_from_schema(el.get("href") or el.get("content"))
    return p


def _text(el) -> str:
    return " ".join(el.get_text(" ").split()) if el is not None else ""


def _sel_text(soup, *selectors) -> str:
    for s in selectors:
        el = soup.select_one(s)
        if el is not None and _text(el):
            return _text(el)
    return ""


# ---------------------------------------------------------------- site rules
def from_amazon(soup, html: str) -> ProductInfo:
    p = ProductInfo()
    if "validateCaptcha" in html or "/errors/validateCaptcha" in html or \
            "api-services-support@amazon.com" in html:
        p.blocked = "Η Amazon ζήτησε επιβεβαίωση (captcha)"
        return p
    p.title = _sel_text(soup, "#productTitle", "#title") or None
    price_txt = _sel_text(
        soup,
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#corePrice_desktop .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#priceblock_ourprice", "#priceblock_dealprice",
        "#tp_price_block_total_price_ww .a-offscreen",
        "#centerCol .a-price .a-offscreen",
    )
    if price_txt:
        p.price = parse_price(price_txt)
        p.currency = detect_currency(price_txt, None)
    avail = _sel_text(soup, "#availability", "#outOfStock", "#availability_feature_div")
    p.in_stock = availability_from_text(avail)
    if p.in_stock is None:
        if soup.select_one("#add-to-cart-button") or soup.select_one("#buy-now-button"):
            p.in_stock = True
        elif soup.select_one("#outOfStock"):
            p.in_stock = False
    if p.in_stock is None and p.price is None and p.title:
        p.in_stock = False
    for row in soup.select("#productDetails_detailBullets_sections1 tr, #detailBullets_feature_div li"):
        t = _text(row)
        m = re.search(r"(?:EAN|GTIN)\D{0,5}(\d{8,14})", t)
        if m and not p.ean:
            p.ean = m.group(1)
        m = re.search(r"(?:Modellnummer|Model number|Numero modello|Numéro du modèle|Número de modelo"
                      r"|Herstellerreferenz|Item model number)\s*[:‎‎]*\s*([\w\-./]+)", t, re.I)
        if m and not p.mpn:
            p.mpn = m.group(1)
    return p


def from_ebay(soup, html: str) -> ProductInfo:
    p = ProductInfo()
    p.title = _sel_text(soup, "h1.x-item-title__mainTitle", "h1#itemTitle", "h1") or None
    price_txt = _sel_text(soup, ".x-price-primary", "#prcIsum", ".x-bin-price__content")
    if price_txt:
        p.price = parse_price(price_txt)
        p.currency = detect_currency(price_txt, None)
    status = _sel_text(soup, ".d-statusmessage", ".x-quantity__availability", ".ux-message__content")
    p.in_stock = availability_from_text(status)
    if p.in_stock is None and (soup.select_one("#binBtn_btn") or soup.select_one("[data-testid='x-bin-action']")
                               or soup.select_one(".x-bin-action")):
        p.in_stock = True
    return p


SITE_RULES = {
    "amazon.": from_amazon,
    "ebay.": from_ebay,
}

GENERIC_PRICE_SELECTORS = [
    "[data-price]", ".product-price .price", ".price-box .price", ".product__price", ".price--current",
    ".current-price", ".price-current", ".final-price", ".product-info-price .price", ".price",
]
GENERIC_AVAIL_SELECTORS = [
    ".availability", ".stock", ".product-availability", "[class*=availability]", "[class*=stock]",
]


OLD_PRICE = re.compile(r"old|was|regular|strike|before|initial|previous|compare|list-?price|rrp|msrp|crossed", re.I)


def _is_old_price(el) -> bool:
    return el.name in ("del", "s", "strike") or bool(OLD_PRICE.search(" ".join(el.get("class") or [])))


def _without_old_prices(el):
    """Αντίγραφο του στοιχείου χωρίς παλιές/διαγραμμένες τιμές (<del>, .old-price…) και ποσοστά έκπτωσης."""
    import copy

    el = copy.copy(el)
    for x in list(el.find_all(True)):
        if x.decomposed:
            continue
        if _is_old_price(x) or re.search(r"discount|badge|percent|saving", " ".join(x.get("class") or []), re.I):
            x.decompose()
    return el


def from_generic(soup) -> ProductInfo:
    p = ProductInfo()
    h1 = soup.find("h1")
    p.title = _text(h1) or None
    for s in GENERIC_PRICE_SELECTORS:
        el = next((e for e in soup.select(s) if not _is_old_price(e)), None)
        if el is None:
            continue
        el = _without_old_prices(el)
        val = el.get("data-price") or _text(el)
        price = parse_price(val)
        if price:
            p.price = price
            p.currency = detect_currency(_text(el), None)
            break
    for s in GENERIC_AVAIL_SELECTORS:
        el = soup.select_one(s)
        if el is not None:
            v = availability_from_text(_text(el))
            if v is not None:
                p.in_stock = v
                break
    return p


CHALLENGE_MARKERS = ["cf-challenge", "challenge-platform", "cf-turnstile", "g-recaptcha", "h-captcha",
                     "px-captcha", "captcha-delivery.com", "/_incapsula_resource", "datadome"]
CHALLENGE_TITLES = ["just a moment", "attention required", "verify you are human", "are you a robot",
                    "access denied", "security check", "robot check", "bot verification"]


def _looks_like_challenge(soup, html: str) -> bool:
    low = html[:200000].lower()
    title = (soup.title.get_text() if soup.title else "").strip().lower()
    if any(t in title for t in CHALLENGE_TITLES):
        return True
    return any(m in low for m in CHALLENGE_MARKERS) and len(soup.get_text(" ").split()) < 400


def extract(html: str, url: str = "") -> ProductInfo:
    soup = BeautifulSoup(html, "html.parser")
    host = host_of(url)
    info = ProductInfo()
    for key, fn in SITE_RULES.items():
        if key in host:
            site = fn(soup, html)
            if site.blocked:
                return site
            info.merge(site, key.strip("."))
            break
    info.merge(from_jsonld(soup), "jsonld")
    info.merge(from_meta(soup), "meta")
    info.merge(from_generic(soup), "generic")
    if info.price is None and _looks_like_challenge(soup, html):
        info.blocked = "Το κατάστημα ζήτησε επιβεβαίωση ότι δεν είσαι ρομπότ (captcha)"
        return info
    if info.in_stock is None and info.price is not None:
        info.in_stock = availability_from_page(soup)
    info.currency = normalize_currency(info.currency)
    if info.currency is None and info.price is not None:
        info.currency = currency_for_host(host)
    if info.title:
        info.title = " ".join(info.title.split())[:300]
    return info
