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


# ---------------------------------------------------------------- αυτόματη εύρεση URL αναζήτησης
SEARCH_PARAM_NAMES = ("q", "query", "search", "s", "text", "k", "keyword", "keywords", "search_query",
                      "searchterm", "term", "w", "kw", "sSearch", "search_text", "searchQuery", "_nkw", "qs")
# Συνηθισμένα μοτίβα (WooCommerce, Magento, Shopify, PrestaShop, OpenCart κ.ά.), δοκιμάζονται με τη σειρά
COMMON_SEARCH_PATHS = ("/search?q={q}", "/search?query={q}", "/search?text={q}", "/?s={q}&post_type=product",
                       "/catalogsearch/result/?q={q}", "/search?search={q}", "/index.php?route=product/search&search={q}",
                       "/search?controller=search&s={q}", "/?s={q}", "/el/search?q={q}", "/search/?q={q}")


def _template_from_target(target: str, page_url: str) -> str | None:
    if not target:
        return None
    for ph in ("{search_term_string}", "{search_term}", "{query}", "{searchTerms}", "{q}"):
        if ph in target:
            t = urljoin(page_url, target.replace(ph, "{q}"))
            return t if t.startswith("http") else None
    return None


def search_url_candidates(html: str, page_url: str) -> list[str]:
    """Πιθανά URL αναζήτησης ({q} = το κείμενο) από την αρχική σελίδα ενός καταστήματος, από το πιο αξιόπιστο."""
    import json

    soup = BeautifulSoup(html or "", "html.parser")
    out: list[str] = []

    def add(t):
        if t and t not in out and domain_matches(host_of(t), host_of(page_url)):
            out.append(t)

    # 1. JSON-LD WebSite → SearchAction (το ίδιο που διαβάζει η Google)
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                pa = d.get("potentialAction")
                for a in (pa if isinstance(pa, list) else [pa]):
                    if isinstance(a, dict) and "Search" in str(a.get("@type", "")):
                        tgt = a.get("target")
                        if isinstance(tgt, dict):
                            tgt = tgt.get("urlTemplate")
                        elif isinstance(tgt, list):
                            tgt = next((x if isinstance(x, str) else (x or {}).get("urlTemplate")
                                        for x in tgt), None)
                        add(_template_from_target(tgt if isinstance(tgt, str) else "", page_url))
                stack.extend(v for k, v in d.items() if k in ("@graph", "mainEntity"))
    # 2. Φόρμα αναζήτησης (GET) με πεδίο κειμένου
    forms = []
    for form in soup.find_all("form"):
        if (form.get("method") or "get").lower() != "get":
            continue
        inputs = form.find_all("input")
        field = None
        for inp in inputs:
            typ = (inp.get("type") or "text").lower()
            name = inp.get("name") or ""
            if typ in ("search", "text") and name and (typ == "search" or name in SEARCH_PARAM_NAMES
                                                        or "search" in name.lower() or "query" in name.lower()):
                field = name
                break
        if not field:
            continue
        action = urljoin(page_url, form.get("action") or page_url)
        extra = [(i.get("name"), i.get("value") or "") for i in inputs
                 if (i.get("type") or "").lower() == "hidden" and i.get("name")]
        from urllib.parse import urlencode

        qs = urlencode(extra)
        sep = "&" if "?" in action else "?"
        tmpl = action.split("#")[0] + sep + (qs + "&" if qs else "") + f"{field}={{q}}"
        role = " ".join([str(form.get("role") or ""), " ".join(form.get("class") or []), form.get("id") or "",
                         str(form.get("action") or "")]).lower()
        forms.append((0 if "search" in role else 1, tmpl))
    for _, t in sorted(forms, key=lambda x: x[0]):
        add(t)
    # 3. Συνηθισμένα μοτίβα
    parts = urlparse(page_url)
    base = f"{parts.scheme}://{parts.netloc}"
    for p in COMMON_SEARCH_PATHS:
        add(base + p)
    return out


def looks_like_search_results(html: str, final_url: str, word: str, home_url: str) -> bool:
    """Ελέγχει ότι η σελίδα είναι πράγματι αποτελέσματα αναζήτησης για τη λέξη."""
    if not html or len(html) < 500:
        return False
    if final_url.rstrip("/") == home_url.rstrip("/"):
        return False
    low = html.lower()
    if low.count(word.lower()) < 2:
        return False
    nf = ("404", "page not found", "δεν βρέθηκε η σελίδα", "seite nicht gefunden")
    title = (BeautifulSoup(html, "html.parser").title or "")
    title = title.get_text().lower() if title else ""
    return not any(x in title for x in nf)
