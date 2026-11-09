"""Αναζήτηση ενός προϊόντος σε άλλα καταστήματα και εύρεση πιθανών αντιστοιχιών."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from .shops import domain_matches, host_of

STOPWORDS = {"the", "and", "with", "for", "με", "και", "για", "του", "της", "των", "new", "neu",
             "black", "white", "μαύρο", "λευκό", "-", "|", "&"}

# Ειδικοί selectors αποτελεσμάτων ανά κατάστημα (αν υπάρχουν). Αλλιώς γενική μέθοδος.
RESULT_SELECTORS = {
    "amazon.": ["div[data-component-type='s-search-result'] h2 a",
                "div[data-component-type='s-search-result'] a.a-link-normal.s-no-outline"],
    "ebay.": ["li.s-item a.s-item__link", "li.s-card a.su-link", "ul.srp-results li a[href*='/itm/']"],
    "bestprice.gr": ["a[href*='/item/']"],
}

# Μοτίβα URL που μοιάζουν με σελίδα προϊόντος.
PRODUCT_URL_HINTS = [r"/dp/", r"/itm/", r"/item/", r"/p/", r"/product", r"/products/", r"\.html?$",
                     r"/\d{5,}", r"-\d{5,}", r"/artikel/"]
NON_PRODUCT_HINTS = [r"/search", r"/s\?", r"/cart", r"/login", r"/account", r"/help", r"/customer",
                     r"/category", r"/cat/", r"/brand", r"/stores", r"/blog", r"javascript:", r"#$"]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


def tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9α-ω]+(?:[-.][a-z0-9α-ω]+)*", normalize(text))
    return [t for t in toks if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]


def build_query(title: str | None, ean: str | None = None, mpn: str | None = None,
                brand: str | None = None) -> str:
    """Το καλύτερο κείμενο αναζήτησης: μάρκα + κωδικός κατασκευαστή, αλλιώς οι πρώτες λέξεις του τίτλου."""
    if mpn and len(mpn) >= 4:
        return f"{brand} {mpn}".strip() if brand and brand.lower() not in mpn.lower() else mpn
    words = (title or "").replace(",", " ").split()
    return " ".join(words[:7])


def search_url(template: str, query: str) -> str:
    return template.replace("{q}", quote_plus(query))


def score(query_tokens: list[str], candidate_title: str, codes: list[str]) -> float:
    ct = set(tokens(candidate_title))
    if not ct or not query_tokens:
        return 0.0
    qt = set(query_tokens)
    s = len(qt & ct) / len(qt)
    norm_title = normalize(candidate_title).replace("-", "").replace(" ", "")
    for code in codes:
        c = normalize(code).replace("-", "").replace(" ", "")
        if len(c) >= 4 and c in norm_title:
            s += 0.6
    # Τα νούμερα (π.χ. 850W, 4070) πρέπει να ταιριάζουν
    q_nums = {t for t in qt if re.search(r"\d", t)}
    if q_nums:
        s += 0.3 * len(q_nums & ct) / len(q_nums) - 0.3 * (len(q_nums - ct) / len(q_nums))
    return round(s, 3)


def _looks_like_product(url: str) -> bool:
    path = urlparse(url).path + ("?" + urlparse(url).query if urlparse(url).query else "")
    if any(re.search(p, path) for p in NON_PRODUCT_HINTS):
        return False
    return any(re.search(p, path) for p in PRODUCT_URL_HINTS)


def _clean_url(url: str, host: str) -> str:
    # Amazon: κράτα μόνο /dp/ASIN
    if "amazon." in host:
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
        if m:
            p = urlparse(url)
            return f"{p.scheme}://{p.netloc}/dp/{m.group(1)}"
    if "ebay." in host:
        m = re.search(r"/itm/(?:[^/]+/)?(\d{9,})", url)
        if m:
            p = urlparse(url)
            return f"{p.scheme}://{p.netloc}/itm/{m.group(1)}"
    return url.split("#")[0]


def find_candidates(html: str, page_url: str, title: str, ean=None, mpn=None, brand=None,
                    limit: int = 3, min_score: float = 0.5) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    host = host_of(page_url)
    selectors = next((v for k, v in RESULT_SELECTORS.items() if k in host), None)
    anchors = []
    if selectors:
        for s in selectors:
            anchors += soup.select(s)
    if not anchors:
        anchors = soup.find_all("a", href=True)

    q_tokens = tokens(" ".join(filter(None, [brand, title])))
    codes = [c for c in (ean, mpn) if c]
    seen = {}
    for a in anchors:
        href = a.get("href")
        if not href:
            continue
        url = urljoin(page_url, href)
        uhost = host_of(url)
        if not (domain_matches(uhost, host) or domain_matches(host, uhost)):
            continue
        if not selectors and not _looks_like_product(url):
            continue
        text = " ".join((a.get("title") or a.get("aria-label") or a.get_text(" ") or "").split())
        if not text:
            img = a.find("img")
            text = (img.get("alt") or "") if img else ""
        if len(text) < 6:
            continue
        sc = score(q_tokens, text, codes)
        if sc < min_score:
            continue
        url = _clean_url(url, uhost)
        if url not in seen or seen[url]["score"] < sc:
            seen[url] = {"url": url, "title": text[:250], "score": sc}
    return sorted(seen.values(), key=lambda d: -d["score"])[:limit]
