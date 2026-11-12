from pricetracker.extract import extract

URL = "https://www.e-kiriazis.gr/ell/product/Ekkinitis-Booster-Mpatarias-TOPDON-V3000"


def page(avail="Άμεσα διαθέσιμο", slider_first=False):
    slider = """<div class="ltn__product-slider-area"><div class="product-item">
        <h3>TOPDON 2824</h3><div class="product-price"><span>500.00&euro;</span></div></div></div>"""
    main = f"""<div class="ltn__shop-details-inner">
      <h1>Εκκινητής - Booster Μπαταρίας TOPDON V3000</h1>
      <div class="modal-product-meta ltn__product-details-menu-1"><ul>
        <li class="overlay-meta"><strong>Διαθεσιμότητα:</strong> <span style="color: #277a33">{avail}</span></li>
        <li class="overlay-meta"><strong>Κωδικός:</strong> <span>V3000</span></li></ul>
        <div class="product-price mt-20"><span>179.97&euro;</span></div>
        <a href="#" class="theme-btn-1 btn" title="Προσθήκη">Προσθήκη</a></div></div>"""
    og = ('<meta property="og:image" content="https://www.e-kiriazis.gr/https://www.e-kiriazis.gr/'
          'thumbnails/a.jpg"/>')
    body = (slider + main) if slider_first else (main + slider)
    return f"<html><head>{og}</head><body>{body}</body></html>"


def test_price_and_stock():
    i = extract(page(), URL)
    assert i.price == 179.97 and i.currency == "EUR" and i.in_stock is True


def test_related_slider_price_is_not_taken():
    assert extract(page(slider_first=True), URL).price == 179.97


def test_out_of_stock():
    assert extract(page("Εξαντλημένο"), URL).in_stock is False


def test_doubled_og_image():
    assert extract(page(), URL).image == "https://www.e-kiriazis.gr/thumbnails/a.jpg"


# Ευρήματα ελέγχου 12/11
from pricetracker.extract import _undouble_url  # noqa: E402


def test_related_price_does_not_block_product_price():
    h = """<html><body><div class="related-products"><span class="price">500,00 €</span></div>
      <div class="product-price">179,97 €</div></body></html>"""
    assert extract(h, URL).price == 179.97


def test_stock_ignores_related_slider_on_ekiriazis():
    h = page().replace('<div class="product-item">',
                       '<div class="product-item"><span>Εξαντλημένο</span>')
    assert extract(h, URL).in_stock is True


def test_empty_first_candidate_tries_next():
    h = """<html><body><div class="product-details"><span class="price"></span>
      <span class="price">12,00 €</span></div></body></html>"""
    assert extract(h, URL).price == 12.0


def test_wrapper_with_unmarked_old_price():
    h = """<html><body><div class="product-price"><span>200.00€</span> <span>179.97€</span>
      <span>3 δόσεις 60,00€</span></div></body></html>"""
    assert extract(h, URL).price == 179.97


def test_undouble_url_only_same_host():
    assert _undouble_url("https://s.gr/https://s.gr/a.jpg") == "https://s.gr/a.jpg"
    assert _undouble_url("https://www.s.gr/https://s.gr/a.jpg") == "https://s.gr/a.jpg"
    assert _undouble_url("https://shop.gr/a.jpg?ref=http://partner.gr") == "https://shop.gr/a.jpg?ref=http://partner.gr"
    assert _undouble_url("https://cdn.x/resize?src=https://shop.gr/a.jpg") == "https://cdn.x/resize?src=https://shop.gr/a.jpg"
    assert _undouble_url(None) is None


def test_doubled_jsonld_image():
    h = """<html><head><script type="application/ld+json">{"@type":"Product","name":"X",
      "image":"https://s.gr/https://s.gr/a.jpg","offers":{"price":"10","priceCurrency":"EUR"}}</script>
      </head><body></body></html>"""
    assert extract(h, "https://s.gr/p").image == "https://s.gr/a.jpg"
