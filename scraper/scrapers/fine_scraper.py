import re
from urllib.parse import urldefrag, urljoin, quote

from bs4 import BeautifulSoup

from utils import parse_volume_string, parse_count_string
from services.browser import fetch_html, collect_products, keep_if_relevant, PageLoadError, SiteLayoutChanged
from log_config import get_logger

logger = get_logger()

BASE_URL = "https://ksa.finestore.com/en"
LINK_SELECTOR = "div.listing-page a.display-flex"
NO_RESULTS_TEXT = "No results found"
NO_RESULTS_SELECTOR = f"div.listing-page-item-container :text('{NO_RESULTS_TEXT}')"
NAME_SELECTOR = "div.ecomz-product-name-style"
# The product page is rendered by Angular after load: wait until the price shows its currency.
PRICE_READY_SELECTOR = "div.ecomz-product-price-style:has-text('SAR')"
MAX_SEARCH_PAGES = 5


def _safe_get_text(element):
    return element.get_text(strip=True) if element else None


def search_url(keyword):
    return f"{BASE_URL}/products?keyword={quote(keyword)}"


def parse_search_results(html, page_url):
    """``(product URLs, next page URL or None)`` of a search page; ([], None) for 'No results'.

    A page with neither product links nor the no-results notice raises ``SiteLayoutChanged``.
    """
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for link in soup.select(LINK_SELECTOR):
        href = link.get('href')
        if href:
            url = urljoin(page_url, href)
            if url not in urls:
                urls.append(url)
    if not urls:
        if NO_RESULTS_TEXT in soup.get_text(' ', strip=True):
            return [], None
        raise SiteLayoutChanged("Fine search page without product links or no-results notice")
    return urls, next_page_url(soup, page_url)


def next_page_url(soup, page_url):
    """URL of the "Next" pagination link, or None.

    Only an <a> whose own text is "Next" counts (a product titled "NextGen ..." does not), with a
    real href that leads to another page (not '#', 'javascript:' or the current page). The live
    site showed no such link on 2026-11-25 (one results page), so this usually returns None, like
    the Selenium version when it found no "Next" element.
    """
    current = urldefrag(page_url)[0]
    for link in soup.find_all('a', href=True):
        own_text = ''.join(link.find_all(string=True, recursive=False)).strip()
        href = link['href'].strip()
        if own_text != 'Next' or not href or href.startswith(('#', 'javascript')):
            continue
        target = urldefrag(urljoin(page_url, href))[0]
        if target != current:
            return target
    return None


def extract_price(soup):
    price_element = soup.select_one("div.ecomz-product-price-style")
    if price_element:
        text = price_element.get_text(separator=' ', strip=True)
        sar_match = re.search(r'SAR\s*([\d.,]+)', text, re.I)
        if sar_match:
            return sar_match.group(1).replace(',', '')
    return '0.00'


def extract_product_specs(soup):
    """``(base volume parsed from the spec table or None, pack multiplier)``."""
    base_volume_data = None
    multiplier = 1
    for row in soup.select("div.product-tr"):
        cells = row.select("div.product-cell")
        if len(cells) != 2:
            continue
        label = (_safe_get_text(cells[0]) or '').lower()
        value = (_safe_get_text(cells[1]) or '').strip()
        if not label or not value:
            continue
        if ("ml" in label or "ml of" in label) and not base_volume_data:
            base_volume_data = parse_volume_string(value)
        elif ("l" in label or "liters" in label or "ltr" in label) and not base_volume_data:
            base_volume_data = parse_volume_string(value)
        if not any(term in label for term in ['number of sheets', 'number of wipes', 'number of pieces',
                                              'volume', 'capacity', 'content', 'size']):
            if not re.search(r'(\d+)\s*(ml|l|ltr|liter|liters|gallons?|fl\s?oz)\b', value, re.I):
                m_pack = re.search(r'\b(\d+)\b', value)
                if m_pack and 2 <= int(m_pack.group(1)) <= 50:
                    multiplier = int(m_pack.group(1))
    return base_volume_data, multiplier


def extract_units_data(soup, product_title):
    for row in soup.select("div.product-tr"):
        cells = row.select("div.product-cell")
        if len(cells) != 2:
            continue
        label = (_safe_get_text(cells[0]) or '').lower()
        value = (_safe_get_text(cells[1]) or '').strip()
        if any(term in label for term in ['number of sheets', 'number of wipes', 'number of pieces', 'count', 'pcs']):
            match = re.search(r'(\d+)', value)
            if match:
                sheets_count = int(match.group(1))
                return {'quantity': sheets_count, 'unit': 'units', 'normalized': sheets_count}
    return parse_count_string(product_title)


def extract_title_multiplier(title):
    match = re.search(r'[x×]\s*(\d+)\b', title, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r'\(carton of \d+x(\d+)\)', title, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r'(\d+)\s+(bottles?|pieces?|units?|pcs?|pack)', title, re.I)
    if match:
        return int(match.group(1))
    return 1


def parse_product_page(html, product_url, search_mode):
    details = {
        'Product': 'Not found', 'Price_SAR': '0.00', 'Company': 'Fine',
        'URL': product_url, 'Unit of measurement': 'units', 'Total quantity': 0
    }
    soup = BeautifulSoup(html, 'html.parser')
    details['Product'] = (
        _safe_get_text(soup.select_one("span.mg-l-0.f-xs-18")) or
        _safe_get_text(soup.select_one(NAME_SELECTOR)) or
        _safe_get_text(soup.select_one("h1")) or
        details['Product']
    )
    details['Price_SAR'] = extract_price(soup)

    if search_mode == 'units':
        final_data = extract_units_data(soup, details['Product'])
        _, spec_multiplier = extract_product_specs(soup)
        multiplier = max(spec_multiplier, extract_title_multiplier(details['Product']))
    else:
        base_volume_data, multiplier = extract_product_specs(soup)
        multiplier = max(multiplier, extract_title_multiplier(details['Product']))
        final_data = (base_volume_data or
                      parse_volume_string(details['Product']) or
                      parse_count_string(details['Product']))

    if final_data:
        details['Unit of measurement'] = final_data['unit']
        details['Total quantity'] = final_data['quantity'] * multiplier
    return details


def is_valid_product(details):
    """``(valid, reason)``: a positive price and quantity and a product name."""
    try:
        if float(str(details.get('Price_SAR', '0')).replace(',', '')) <= 0:
            return False, "Invalid price"
    except ValueError:
        return False, "Invalid price format"
    if details.get('Total quantity', 0) <= 0:
        return False, "Invalid quantity"
    if details.get('Product', '') == 'Not found':
        return False, "Product name not found"
    return True, "Valid"


class FineScraper:
    def __init__(self, browser, relevance_agent):
        self.browser = browser
        self.relevance_agent = relevance_agent
        self.products_to_find_limit = 6

    def scrape(self, keyword, search_mode):
        logger.info(f"  [Fine Scraper] Searching for: '{keyword}' (Mode: {search_mode})")
        relevant = keep_if_relevant(self.relevance_agent, keyword)

        def keep(details):
            valid, reason = is_valid_product(details)
            if not valid:
                logger.info(f"      -> Validation failed: {reason}")
                return False
            return relevant(details)

        found, visited_pages, seen_products = [], set(), set()
        with self.browser.page() as page:

            def details(url):
                return parse_product_page(fetch_html(page, url, wait_for=PRICE_READY_SELECTOR, timeout_ms=12_000),
                                          url, search_mode)

            next_url = search_url(keyword)
            for page_number in range(1, MAX_SEARCH_PAGES + 1):
                if next_url is None or next_url in visited_pages or len(found) >= self.products_to_find_limit:
                    break
                visited_pages.add(next_url)
                try:
                    html = fetch_html(page, next_url, wait_for=f"{LINK_SELECTOR}, {NO_RESULTS_SELECTOR}")
                    product_urls, next_url = parse_search_results(html, next_url)
                    product_urls = [url for url in product_urls if url not in seen_products]
                    seen_products.update(product_urls)
                    logger.info(f"    > {len(product_urls)} new products on results page {page_number}.")
                    found += collect_products(product_urls, details, keep,
                                              self.products_to_find_limit - len(found), 'Fine')
                except PageLoadError as error:
                    # The first page, or a later one with nothing found yet, fails the search; a
                    # later page keeps what the earlier ones gave (the Selenium version did too).
                    if page_number == 1 or not found:
                        raise
                    logger.warning(f"    ! Fine: results page {page_number} failed ({type(error).__name__}): "
                                   f"{error}. Keeping the {len(found)} products found before it.")
                    break
        return found
