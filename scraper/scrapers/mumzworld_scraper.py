import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote
from utils import parse_volume_string
from services.browser import fetch_html, collect_products, keep_if_relevant
from log_config import get_logger

logger = get_logger()

# Ticket A: Mumzworld rows must not change. Only the browser layer moved from Selenium to
# Playwright (Mafer, 2026-11-25, option A); the parsing below is the Selenium version's, statement
# for statement, and tests/differential/test_scraper_main_differential.py proves that the same
# saved HTML gives the same rows. The pack multiplier bug (it takes a volume as the multiplier)
# is known and deliberately not fixed here.
BASE_URL = "https://www.mumzworld.com/sa-en/"
# Prefix selectors: the CSS-modules hash (e.g. __lcVK_) changes with every frontend redeploy;
# the class prefix does not.
CARD_SELECTOR = "div[class*='ProductCard_productCard__']"
NAME_SELECTOR = "h1[class*='ProductDetails_productName__']"
PRODUCTS_TO_FIND = 10


def _safe_get_text(element):
    return element.get_text(strip=True) if element else None


def parse_mumzworld_count_string(text_string):
    if not text_string:
        return None
    match = re.search(r'(\d+)\s*(wipes|count|sheets|sachets|pack|pcs|pieces|pc|s)\b', text_string, re.I)
    if not match:
        return None
    quantity = int(match.group(1))
    return {'quantity': quantity, 'unit': 'units', 'normalized': quantity}


def parse_search_results(html, base_url=BASE_URL):
    """Product URLs of the result cards, in page order (repeats kept, as before)."""
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for container in soup.select(CARD_SELECTOR):
        link_tag = container.select_one("a[class*='ProductCard_productName__']")
        if link_tag and link_tag.has_attr('href'):
            urls.append(urljoin(base_url, link_tag['href']))
    return urls


def parse_product_page(html, product_url, search_mode):
    details = {
        'Product': 'Not found', 'Price_SAR': '0.00', 'Company': 'Not found',
        'URL': product_url, 'Unit of measurement': 'units', 'Total quantity': 0
    }
    soup = BeautifulSoup(html, 'html.parser')

    product_name_tag = soup.select_one(NAME_SELECTOR)
    product_name = _safe_get_text(product_name_tag)
    if product_name:
        details['Product'] = product_name
        details['Company'] = product_name.split(' - ')[0].strip() if ' - ' in product_name else product_name.split(' ')[0].strip()

        parsed_data = parse_mumzworld_count_string(product_name) if search_mode == 'units' else parse_volume_string(product_name)
        if parsed_data:
            base_quantity = parsed_data['quantity']
            multiplier_match = re.search(r'(?:pack of|x|of)\s*(\d+)', product_name, re.IGNORECASE)
            if multiplier_match:
                multiplier = int(multiplier_match.group(1))
                details['Total quantity'] = base_quantity * multiplier
                logger.debug(f"      -> Multiplier found: {base_quantity} * {multiplier} = {details['Total quantity']}")
            else:
                details['Total quantity'] = base_quantity
            details['Unit of measurement'] = parsed_data['unit']
            logger.debug(f"      -> Extracted amount: {details['Total quantity']} {parsed_data['unit']}")

    price_tag = soup.select_one("span[class*='Price_integer__']")
    if price_tag:
        details['Price_SAR'] = _safe_get_text(price_tag).replace(',', '')
    return details


class MumzworldScraper:
    def __init__(self, browser, relevance_agent):
        self.browser = browser
        self.relevance_agent = relevance_agent
        self.base_url = BASE_URL

    def scrape(self, keyword, search_mode):
        logger.info(f"  [Mumzworld Scraper] Searching: '{keyword}' (Mode: {search_mode})")
        search_url = f"{self.base_url}search?q={quote(keyword)}"
        with self.browser.page() as page:
            logger.info(f"    > Navigating to: {search_url}")
            # No result card (a search without results, or a block page) raises ElementNotFound.
            html = fetch_html(page, search_url, wait_for=CARD_SELECTOR)
            product_urls = parse_search_results(html, self.base_url)
            logger.info(f"    > {len(product_urls)} products on the results page.")

            def details(url):
                return parse_product_page(fetch_html(page, url, wait_for=NAME_SELECTOR), url, search_mode)

            return collect_products(product_urls, details, keep_if_relevant(self.relevance_agent, keyword),
                                    PRODUCTS_TO_FIND, 'Mumzworld')
