import json
from bs4 import BeautifulSoup
from urllib.parse import quote, urljoin
from utils import parse_volume_string, parse_count_string, parse_saco_count_string
from services.browser import fetch_html, read_html, collect_products, keep_if_relevant
from log_config import get_logger

logger = get_logger()

# Ticket A: SACO rows must not change. Only the browser layer moved from Selenium to Playwright
# (Mafer, 2026-11-25, option A); the parsing below is the Selenium version's, statement for
# statement, and tests/differential/test_scraper_main_differential.py proves that the same saved
# HTML gives the same rows.
BASE_URL = "https://www.saco.sa/en/"
LINK_SELECTOR = "a[href*='/en/product/']"
NO_RESULTS_SELECTOR = "h2:has-text('No results found')"
PRODUCTS_TO_FIND = 6
SECONDS_FOR_MORE_RESULTS = 3  # the Selenium version also waited 3 s for the rest of the cards


def parse_search_links(html, page_url):
    """Unique product URLs of a results page, in page order. The new site (Next.js) repeats the
    same href in several <a> per card (image and title); like Selenium's ``get_attribute('href')``
    each one is resolved against the page URL."""
    soup = BeautifulSoup(html, 'html.parser')
    product_hrefs = []
    for link in soup.select(LINK_SELECTOR):
        if not link.get('href'):
            continue
        href = urljoin(page_url, link['href'])
        if href not in product_hrefs:
            product_hrefs.append(href)
    return product_hrefs


def parse_product_page(html, product_url, search_mode):
    soup = BeautifulSoup(html, 'html.parser')

    details = {
        'Product': 'Not found', 'Price_SAR': '0.00', 'Company': 'Brand not found',
        'URL': product_url, 'Unit of measurement': 'units', 'Total quantity': 0
    }

    # The new SACO site (Next.js/Tailwind) has no stable CSS classes;
    # the reliable data is the JSON-LD (schema.org/Product) with name and price.
    product_data = None
    for tag in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(tag.string or '')
        except (TypeError, ValueError):
            continue  # another JSON-LD block that is not valid JSON; the Product one is searched for
        if isinstance(data, dict) and data.get('@type') == 'Product':
            product_data = data
            break

    if product_data:
        details['Product'] = product_data.get('name') or 'Not found'
        offers = product_data.get('offers') or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if offers.get('price'):
            details['Price_SAR'] = str(offers['price'])
        brand = product_data.get('brand') or {}
        if isinstance(brand, dict) and brand.get('name'):
            details['Company'] = brand['name']
    else:
        title_tag = soup.find('h1')
        if title_tag:
            details['Product'] = title_tag.get_text(strip=True)

    product_name = details['Product']
    if product_name != "Not found":
        if search_mode == 'units':
            parsed_data = parse_saco_count_string(product_name)
            if not parsed_data:
                parsed_data = parse_count_string(product_name)
        else:
            parsed_data = parse_volume_string(product_name)

        if parsed_data:
            details['Total quantity'] = parsed_data['quantity']
            details['Unit of measurement'] = parsed_data['unit']
            logger.debug(f"        -> Extracted amount: {details['Total quantity']} {details['Unit of measurement']}")

    return details


class SacoScraper:
    def __init__(self, browser, relevance_agent):
        self.browser = browser
        self.relevance_agent = relevance_agent
        self.base_url = BASE_URL

    def scrape(self, keyword, search_mode):
        logger.info(f"  [Saco Scraper] Searching: '{keyword}'")
        search_url = f"{self.base_url}products-search?q={quote(keyword)}"
        with self.browser.page() as page:
            # The cookie banner is not clicked any more: nothing is clicked, the pages are read.
            html = fetch_html(page, search_url, wait_for=f"{LINK_SELECTOR}, {NO_RESULTS_SELECTOR}",
                              timeout_ms=25_000)
            product_hrefs = parse_search_links(html, page.url)
            if product_hrefs:
                page.wait_for_timeout(SECONDS_FOR_MORE_RESULTS * 1000)
                product_hrefs = parse_search_links(read_html(page, search_url), page.url)
            if not product_hrefs:
                logger.info(f"    > No results for '{keyword}'.")
                return []
            logger.info(f"    > {len(product_hrefs)} unique products on the results page.")

            def details(url):
                return parse_product_page(fetch_html(page, url, wait_for="h1", timeout_ms=30_000), url, search_mode)

            found = collect_products(product_hrefs, details, keep_if_relevant(self.relevance_agent, keyword),
                                     PRODUCTS_TO_FIND, 'SACO')
        logger.info(f"  [Saco Scraper] Finished scraping. Found data for {len(found)} products.")
        return found
