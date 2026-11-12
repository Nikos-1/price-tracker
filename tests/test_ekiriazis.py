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
