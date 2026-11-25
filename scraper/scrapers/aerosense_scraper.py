import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from utils import extract_aerosense_units
from services.browser import fetch_html, SiteLayoutChanged
from log_config import get_logger

logger = get_logger()

SITE_URL = "https://www.aero-sense.com"
SHOP_URL = f"{SITE_URL}/en/online-shop"
EUR_TO_SAR = 4.39
TITLE_SELECTOR = 'h1 div.field--name-title'
VARIATION_SELECTOR = '#edit-purchased-entity-0-attributes-attribute-volume .js-form-item'


def product_slug(search_term):
    return search_term.replace(' ', '-').lower()


def find_product_url(shop_html, slug):
    """URL of the product whose last path segment is ``slug``, in any shop category.

    The shop regroups its products from time to time (in 2026 'cabin-and-exterior-cleaning' was
    split into 'interior-cleaning', 'exterior-cleaning' and 'inflight-hygiene'), so the URL is
    looked up on the shop index instead of being built from a fixed category. ``None`` if absent.
    """
    soup = BeautifulSoup(shop_html, 'html.parser')
    for link in soup.select("a[href*='/online-shop/']"):
        path = link['href'].split('#', 1)[0].split('?', 1)[0].rstrip('/')
        if path.rsplit('/', 1)[-1] == slug and path.count('/') >= 4:
            return urljoin(SITE_URL, path)
    return None


def parse_package_info(package_text):
    """Volume in ml of a package text such as '5 litre can x 4' (0 when there is none)."""
    volume_ml = 0
    base_match = re.search(r'(\d+[\.,]?\d*)\s*(ml|l)', package_text, re.IGNORECASE)
    if base_match:
        base_volume = float(base_match.group(1).replace(',', '.'))
        unit = base_match.group(2).lower()
        if unit == 'l':
            base_volume *= 1000
        volume_ml = base_volume
        multiplier_match = re.search(r'x\s*(\d+)', package_text, re.IGNORECASE)
        if multiplier_match:
            volume_ml *= int(multiplier_match.group(1))
    return volume_ml


def parse_product_page(html, product_url, mode='volume'):
    """One product dict per priced package of an AeroSense product page.

    Packages without a price (quotes on request, e.g. '200 litre barrel') are skipped. A page
    without the title or the package list raises ``SiteLayoutChanged``.
    """
    soup = BeautifulSoup(html, 'html.parser')
    title_tag = soup.select_one(TITLE_SELECTOR)
    variation_tags = soup.select(VARIATION_SELECTOR)
    if title_tag is None or not variation_tags:
        raise SiteLayoutChanged(f"no product title or package list at {product_url}")
    product_name = title_tag.text.strip()

    products = []
    for variation in variation_tags:
        package_tag = variation.select_one('.package')
        price_tag = variation.select_one('.variationprice')
        if not package_tag or not price_tag:
            continue
        package_info = package_tag.text.strip()
        price_text = price_tag.text.strip().replace('€', '').replace(',', '').strip()
        price_sar = round(float(price_text) * EUR_TO_SAR, 2)
        if mode == 'units':
            total_quantity, unit = extract_aerosense_units(package_info), 'units'
        else:
            total_quantity, unit = parse_package_info(package_info), 'ml'
        logger.info(f"  - Found variation: {product_name} | {package_info} | Price: {price_sar:.2f} SAR | "
                    f"Quantity: {total_quantity} {unit}")
        products.append({
            'Company': 'AeroSense',
            'Product': f"{product_name} - {package_info}",
            'Price_SAR': price_sar,
            'Total quantity': total_quantity,
            'Unit of measurement': unit,
            'URL': product_url,
        })
    return products


class AeroSenseScraper:
    """AeroSense (EUR prices converted to SAR). The keyword is the product slug; no relevance AI."""

    def __init__(self, browser):
        self.browser = browser
        self._shop_html = None  # the shop index, read once per scraper instance

    def scrape(self, search_term, mode='volume'):
        slug = product_slug(search_term)
        logger.info(f"  [AeroSense Scraper] Looking up product '{slug}' (Mode: {mode})")
        with self.browser.page() as page:
            if self._shop_html is None:
                self._shop_html = fetch_html(page, SHOP_URL, wait_for="a[href*='/online-shop/']")
            product_url = find_product_url(self._shop_html, slug)
            if product_url is None:
                raise SiteLayoutChanged(f"product '{slug}' is not in the AeroSense shop index")
            html = fetch_html(page, product_url, wait_for=TITLE_SELECTOR, timeout_ms=20_000)
        return parse_product_page(html, product_url, mode)
