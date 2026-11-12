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
    "skroutz.gr": ["a.js-sku-link[href^='/s/']", "a[href^='/s/'][href$='.html']"],
    "idealo.": ["a[href*='OffersOfProduct']", "a[href*='/preisvergleich/Typ']"],
    "geizhals.": ["a.listview__name-link", "a[href*='-a'][href$='.html']"],
    "pricerunner.com": ["a[href*='/pl/']"],
    "pricespy.co.uk": ["a[href*='/product.php']", "a[href*='/product/']"],
    "ledenicheur.fr": ["a[href*='/product.php']", "a[href*='/product/']"],
    "tweakers.net": ["a[href*='/pricewatch/'][href$='.html']"],
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


# Τεχνικά χαρακτηριστικά (24000mAh, 12V, 850W, 512GB…): δεν είναι κωδικός μοντέλου και συχνά λείπουν από τίτλους
_UNITS = {"mah", "ah", "wh", "kwh", "va", "v", "w", "kw", "a", "gb", "tb", "mb", "mm", "cm", "m", "kg", "g", "hz", "khz",
          "mhz", "ghz", "l", "ml", "inch", "in", "mp", "k", "p", "fps", "db", "bar", "nm", "rpm", "x", "cc", "t", "lm",
          "pcs", "τεμ", "lt", "lb", "oz", "dpi", "mbps", "gbps", "mt", "cl"}
# Μονογράμματες μονάδες μόνο με μικρό αριθμό: «4K», «2x», «5A» είναι χαρακτηριστικά, «13600K» και «7950X» μοντέλα
_SHORT_UNITS = {"k", "x", "t", "a"}


def _unit(t: str) -> tuple[str, str] | None:
    """(μονάδα, τιμή) αν η λέξη είναι τεχνικό χαρακτηριστικό (24000mAh, 12V, 850W, 2x8GB), αλλιώς None."""
    t = t.lower().strip("()[]{},;:")
    m = re.fullmatch(r"(\d+)x(\d+(?:[.,]\d+)?)([a-zα-ω]*)", t)  # «2x8GB»: συσκευασία, όχι μοντέλο
    if m:
        return "x" + m.group(3), f"{m.group(1)}x{m.group(2)}"
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)([a-zα-ω]+)", t)
    if not m or m.group(2) not in _UNITS:
        return None
    if m.group(2) in _SHORT_UNITS and float(m.group(1).replace(",", ".")) >= 1000:
        return None
    return m.group(2), m.group(1).replace(",", ".")


def _units(toks) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for t in toks:
        u = _unit(t)
        if u:
            out.setdefault(u[0], set()).add(u[1])
    return out


def _compact(text: str) -> str:
    return re.sub(r"[\s\-./_]+", "", normalize(text))


def model_codes(text: str) -> list[str]:
    """Κωδικοί μοντέλου στον τίτλο: λέξεις με γράμματα ΚΑΙ αριθμούς (V3000, RM850x, SM-S921B), όχι μονάδες
    μέτρησης (24000mAh, 12V) ή σκέτοι αριθμοί (χρονιές κ.λπ.)."""
    out = []
    for t in tokens(text):
        c = t.replace("-", "").replace(".", "")
        if len(c) >= 3 and re.search(r"\d", c) and re.search(r"[a-zα-ω]", c) and not _unit(t) and c not in out:
            out.append(c)
    return out


def build_queries(title: str | None, ean: str | None = None, mpn: str | None = None,
                  brand: str | None = None) -> list[str]:
    """Αναζητήσεις με τη σειρά που δοκιμάζονται: κωδικός κατασκευαστή, «μάρκα + μοντέλο» (οι μηχανές αναζήτησης
    των καταστημάτων συχνά δεν βγάζουν τίποτα με μεγάλο τίτλο) και μετά οι πρώτες λέξεις του τίτλου."""
    out = []
    if mpn and len(mpn) >= 4:
        out.append(build_query(title, ean, mpn, brand))
    words = (title or "").replace(",", " ").split()
    models = [w for w in words if model_codes(w)][:2]
    if models:
        b = (brand or "").strip()
        if not b:  # μάρκα = η πρώτη λατινική λέξη πριν από τον κωδικό («Τροφοδοτικό Corsair RM850x» → Corsair)
            b = next((w for w in words[:words.index(models[0])]
                      if not re.search(r"[α-ωά-ώ]", w, re.I) and re.search(r"[a-z]", w, re.I)), "")
        # «μάρκα + μοντέλο + έκδοση»: οι λατινικές λέξεις γύρω από τον κωδικό (Galaxy S24 Ultra, O11 Dynamic EVO XL,
        # RM850x 2021), ώστε το κατάστημα να μην επιστρέψει μόνο άλλες εκδόσεις του ίδιου μοντέλου
        brand_words = ({x.casefold() for x in b.split()} | {_compact(b)}) if b else set()

        def plain(w):
            ts = tokens(w)
            return (bool(ts) and not re.search(r"[α-ωά-ώ]", w, re.I) and not any(_unit(t) for t in ts)
                    and w.casefold() not in brand_words and _compact(w) not in brand_words)

        def with_brand(ws):
            return " ".join(([b] if b and _compact(b) not in _compact(" ".join(ws)) else []) + ws)

        i = words.index(models[0])
        span = [models[0]]
        for w in reversed(words[max(0, i - 2):i]):  # «Galaxy» στο «Samsung Galaxy S24»
            if not plain(w):
                break
            span.insert(0, w)
        for w in words[i + 1:i + 4]:
            if not plain(w):
                break
            span.append(w)
        if len(span) > 1:
            out.append(with_brand(span))
        out.append(with_brand(models))
    out.append(build_query(title))
    return [q for i, q in enumerate(out) if q and q not in out[:i]]


def search_url(template: str, query: str) -> str:
    return template.replace("{q}", quote_plus(query))


def _has_code(code: str, text: str) -> bool:
    """Ο κωδικός υπάρχει στο κείμενο ως κωδικός, όχι ως κομμάτι μεγαλύτερου αριθμού (V300 ≠ V3000, O11 = O11D).
    Ελέγχεται σε όλο το κείμενο χωρίς κενά και σε κάθε κομμάτι του («i5-13600KF» → «13600kf»)."""
    pat = re.compile(r"(?<!\d)" * code[0].isdigit() + re.escape(code) + r"(?!\d)")
    return any(pat.search(x) for x in [_compact(text)] + re.split(r"[\s\-./_()]+", normalize(text)) if x)


def _model_positions(seq: list[str], models) -> list[int]:
    return [i for i, t in enumerate(seq) if any(_has_code(m, t) for m in models)]


def _span_variants(seq: list[str], models, keep) -> set[str]:
    """Οι εκδόσεις (Mini, EVO, XL, Pro…) στις λέξεις γύρω από τον κωδικό μοντέλου: «O11 Dynamic EVO XL»,
    «MacBook Air M3». Όχι πιο μακριά («RM850x 850W 80 Plus Gold», «NH-D15 CPU Air Cooler»). `keep(t)`: ποιες
    άλλες λέξεις επιτρέπεται να υπάρχουν ανάμεσα (π.χ. το «Dynamic»)."""
    out: set[str] = set()
    for i in _model_positions(seq, models):
        out |= _variants([seq[i]])
        for rng in (range(i + 1, min(len(seq), i + 5)), range(i - 1, max(-1, i - 3), -1)):
            for j in rng:
                t = seq[j]
                v = _variants([t])
                if v:
                    out |= v
                elif t.isdigit() or _unit(t) or not keep(t):
                    break
    return out


def score(query_tokens: list[str], candidate_title: str, codes: list[str],
          models: list[str] | None = None, brand: str | None = None) -> float:
    cseq = tokens(candidate_title)
    ct = set(cseq)
    if not ct or not query_tokens:
        return 0.0
    qseq = list(query_tokens)
    qt = set(qseq)
    compact = _compact(candidate_title)
    # Μια λέξη μετράει ως κοινή και όταν είναι γραμμένη αλλιώς («LIAN-LI» = «Lian Li»)
    common = {t for t in qt if t in ct or (len(t) >= 4 and _compact(t) in compact)}
    s = len(common) / len(qt)
    norm_title = normalize(candidate_title).replace("-", "").replace(" ", "")
    for code in codes:
        c = normalize(code).replace("-", "").replace(" ", "")
        if len(c) >= 4 and c in norm_title:
            s += 0.6
    # Τα νούμερα (π.χ. 850W, 4070) πρέπει να ταιριάζουν· τα τεχνικά χαρακτηριστικά (24000mAh) που λείπουν από
    # τον τίτλο του καταστήματος δεν μετράνε αρνητικά
    q_nums = {t for t in qt if re.search(r"\d", t)}
    if q_nums:
        missing = {t for t in q_nums - ct if not _unit(t)}
        s += 0.3 * len(q_nums & ct) / len(q_nums) - 0.3 * (len(missing) / len(q_nums))
    if models:
        cand_models = model_codes(candidate_title)
        if all(_has_code(m, candidate_title) for m in models):
            # Ίδιος κωδικός μοντέλου: σχεδόν σίγουρα το ίδιο προϊόν (λιγότερο σίγουρα αν λείπει η μάρκα)
            strong = 0.8 if not brand or _compact(brand) in compact else 0.6
            s = max(s, strong + 0.2 * len(common) / len(qt))
        elif any(_same_family(m, x) for m in models for x in cand_models):
            return 0.0  # άλλο μοντέλο της ίδιας σειράς (V2000 αντί για V3000, RM850e αντί για RM850x)
        # Εκδόσεις του ίδιου μοντέλου (O11 Dynamic EVO XL ≠ O11 Mini / Air / EVO): μια έκδοση που δεν ζητήθηκε
        # σημαίνει άλλο προϊόν, και μια έκδοση που ζητήθηκε αλλά λείπει σημαίνει μάλλον το βασικό μοντέλο
        qv = _span_variants(qseq, models, lambda t: not re.search(r"[α-ω]", t))
        cv = _span_variants(cseq, models + cand_models, lambda t: t in qt or any(t in q for q in qt))
        if cv - qv:
            return 0.0
        if qv - cv:
            s = min(s, 0.45)
    # Ίδιο χαρακτηριστικό με άλλη τιμή (256GB ≠ 512GB, 20000mAh ≠ 10000mAh): άλλο προϊόν
    qu, cu = _units(qt), _units(ct)
    if any(qu[u] - cu[u] and cu[u] - qu[u] for u in qu.keys() & cu.keys()):
        s = min(s, 0.3)
    # Αξεσουάρ για το προϊόν («Θήκη για …», «Καλώδιο για …»): όχι το ίδιο το προϊόν. Μετά τον κωδικό και χωρίς
    # «για/for» μάλλον περιγράφει το ίδιο το προϊόν («Lian Li O11 … ATX Case»), οπότε μετράει λιγότερο
    acc = [i for i, w in enumerate(cseq) if w in ACCESSORY_WORDS and w not in qt]
    if acc:
        pos = _model_positions(cseq, models) if models else []
        after = bool(pos) and min(acc) > pos[0] and not (ct & FOR_WORDS)
        s -= 0.2 if after else 0.5
    return round(s, 3)


FOR_WORDS = {"για", "for", "fur", "pour", "per", "para", "compatible", "συμβατο", "συμβατη", "συμβατα", "συμβατος"}
ACCESSORY_WORDS = {"θηκη", "θηκες", "καλυμμα", "καλωδιο", "ανταπτορας", "προστατευτικο", "τζαμακι",
                   "βαση", "ανταλλακτικο", "ανταλλακτικα", "case", "cover", "sleeve", "cable", "adapter", "mount",
                   "protector", "replacement", "spare", "hulle", "kabel", "halterung", "etui", "custodia", "funda"}


VARIANT_WORDS = {"mini", "micro", "xl", "xxl", "max", "plus", "pro", "ultra", "lite", "air", "evo", "vision",
                 "flow", "slim", "compact", "se", "fe", "neo", "nano", "elite", "turbo", "duo", "jr", "junior"}


def _variants(toks) -> set[str]:
    out = set()
    for t in toks:
        out |= {p for p in t.split("-") if p in VARIANT_WORDS}
    return out


def _same_family(a: str, b: str) -> bool:
    """«v3000» και «v2000», «rm850x» και «rm850e», «v300» και «v3000»: ίδια γράμματα μπροστά, άλλος κωδικός
    (όχι όμως «o11» και «o11d», που είναι ο ίδιος κωδικός γραμμένος συντομευμένα)."""
    pa, pb = re.match(r"[a-zα-ω]*", a).group(0), re.match(r"[a-zα-ω]*", b).group(0)
    short, long_ = sorted((a, b), key=len)
    return a != b and bool(pa) and pa == pb and not _has_code(short, long_)


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
    models = model_codes(title or "")
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
        sc = score(q_tokens, text, codes, models, brand)
        if sc < min_score:
            continue
        url = _clean_url(url, uhost)
        if url not in seen or seen[url]["score"] < sc:
            seen[url] = {"url": url, "title": text[:250], "score": sc}
    return sorted(seen.values(), key=lambda d: -d["score"])[:limit]


# ---------------------------------------------------------------- αυτόματη εύρεση URL αναζήτησης
SEARCH_PARAM_NAMES = ("q", "query", "search", "s", "text", "k", "keyword", "keywords", "search_query",
                      "searchterm", "term", "w", "kw", "sSearch", "search_text", "searchQuery", "_nkw", "qs",
                      "searchfor")
# Συνηθισμένα μοτίβα (WooCommerce, Magento, Shopify, PrestaShop, OpenCart κ.ά.), δοκιμάζονται με τη σειρά
COMMON_SEARCH_PATHS = ("/search?q={q}", "/search?query={q}", "/search?text={q}", "/?s={q}&post_type=product",
                       "/catalogsearch/result/?q={q}", "/search?search={q}", "/index.php?route=product/search&search={q}",
                       "/search?controller=search&s={q}", "/?s={q}", "/el/search?q={q}", "/search/?q={q}",
                       "/search.php?searchfor={q}", "/search.php?q={q}")  # search.php: καταστήματα της Komvos.gr


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
    parts = urlparse(page_url)
    base = f"{parts.scheme}://{parts.netloc}"
    # 3. Αναζήτηση που γίνεται με JavaScript (κουτί χωρίς <form>): π.χ. location = "/search.php?searchfor=" + q
    for m in re.finditer(r"""["'](/?[\w/.\-]*\?(?:[\w\-]+=[^"'&]*&)*(\w+)=)["']\s*\+""", html or ""):
        path, param = m.group(1), m.group(2)
        if param in SEARCH_PARAM_NAMES or "search" in param.lower() or "search" in path.lower():
            add(urljoin(base + "/", path) + "{q}")
    # 4. Συνηθισμένα μοτίβα, πρώτα αυτά της πλατφόρμας που «φαίνεται» στην αρχική σελίδα
    for p in sorted(COMMON_SEARCH_PATHS, key=lambda p: _platform_rank(p, html or "")):
        add(base + p)
    return out


# Αποτυπώματα πλατφόρμας στην αρχική σελίδα → ποια μοτίβα αναζήτησης να δοκιμαστούν πρώτα
PLATFORM_HINTS = [
    (("search.php", "thpicvx", "komvos"), ("/search.php",)),
    (("woocommerce", "wp-content"), ("/?s=",)),
    (("mage/", "magento", "catalogsearch"), ("/catalogsearch/",)),
    (("route=product", "catalog/view/theme", "opencart"), ("route=product/search",)),
    (("prestashop",), ("controller=search",)),
    (("cdn.shopify.com", "shopify"), ("/search?q=",)),
]


def _platform_rank(path: str, html: str) -> int:
    low = html[:400000].lower()
    for marks, paths in PLATFORM_HINTS:
        if any(m in low for m in marks) and any(x in path for x in paths):
            return 0
    return 1


_STOP = {"https", "http", "www", "home", "shop", "menu", "cart", "login", "contact", "about", "search", "more",
         "products", "product", "προϊόντα", "καλάθι", "αναζήτηση", "επικοινωνία", "σύνδεση", "προσφορές", "όλα",
         "νέα", "περισσότερα", "κατηγορίες", "λογαριασμός", "facebook", "instagram", "youtube", "twitter"}


def probe_words(html: str, n: int = 2) -> list[str]:
    """Λέξεις δοκιμής για την αναζήτηση, από την ίδια την αρχική σελίδα (π.χ. μάρκες που πουλά το
    κατάστημα): ένα κατάστημα εργαλείων κήπου δεν έχει «samsung» στα αποτελέσματα."""
    from collections import Counter

    soup = BeautifulSoup(html or "", "html.parser")
    cnt: Counter = Counter()
    for a in soup.find_all("a"):
        for w in re.findall(r"[^\W\d_]{4,}", a.get_text(" ")):
            lw = w.lower()
            if lw not in _STOP:
                cnt[lw] += 1
    return [w for w, c in cnt.most_common(n) if c >= 2]


def _same_page(a: str | None, b: str | None) -> bool:
    """Ίδια διεύθυνση, αγνοώντας http/https, www, την τελική κάθετο και το ?query/#."""
    def norm(u):
        u = (u or "").strip().lower().split("#")[0].split("?")[0]
        u = re.sub(r"^https?://(www\.)?", "", u)
        return u.rstrip("/")
    return bool(a and b) and norm(a) == norm(b)


def returns_home(html: str, final_url: str, home_url: str) -> bool:
    """Η «αναζήτηση» άνοιξε στην ουσία την αρχική σελίδα: είτε έγινε ανακατεύθυνση εκεί, είτε η σελίδα δηλώνει
    ως κανονική της διεύθυνση (canonical) την αρχική. Έτσι φαίνεται ένα λάθος URL αναζήτησης (π.χ. το «?s=…» του
    WordPress σε κατάστημα που δεν το υποστηρίζει). Το og:url δεν μετράει: πολλά καταστήματα το έχουν ίδιο παντού."""
    if _same_page(final_url, home_url):
        return True
    soup = BeautifulSoup(html or "", "html.parser")
    link = soup.find("link", rel=lambda r: r and "canonical" in (r if isinstance(r, list) else [r]))
    u = (link or {}).get("href")
    return bool(u) and _same_page(urljoin(final_url or home_url, u), home_url)


def looks_like_search_results(html: str, final_url: str, word: str, home_url: str,
                              home_html: str | None = None) -> bool:
    """Ελέγχει ότι η σελίδα είναι πράγματι αποτελέσματα αναζήτησης για τη λέξη."""
    if not html or len(html) < 500:
        return False
    if returns_home(html, final_url, home_url):
        return False
    low = html.lower()
    w = word.lower()
    # Η λέξη να υπάρχει στα αποτελέσματα, όχι μόνο στο «Αναζήτηση για …» (ή να φαίνεται και στη διεύθυνση)
    if low.count(w) < 2 and not (low.count(w) >= 1 and w in final_url.lower() and low.count("<a ") >= 20):
        return False
    nf = ("404", "page not found", "δεν βρέθηκε η σελίδα", "seite nicht gefunden")
    title = (BeautifulSoup(html, "html.parser").title or "")
    title = title.get_text().lower() if title else ""
    # Οι λέξεις δοκιμής βγαίνουν από την αρχική σελίδα, άρα υπάρχουν και εκεί: μια σελίδα με τον ίδιο τίτλο και
    # όχι περισσότερες εμφανίσεις της λέξης είναι απλώς η αρχική ξανά
    if home_html:
        ht = BeautifulSoup(home_html, "html.parser").title
        if ht and ht.get_text().strip().lower() == title.strip() and low.count(w) <= home_html.lower().count(w):
            return False
    return not any(x in title for x in nf)
