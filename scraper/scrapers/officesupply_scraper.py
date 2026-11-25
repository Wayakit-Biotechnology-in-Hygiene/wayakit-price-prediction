import re
from urllib.parse import urljoin, quote

from bs4 import BeautifulSoup

from utils import parse_volume_with_multiplier, parse_count_string
from services.browser import fetch_html, collect_products, keep_if_relevant, SiteLayoutChanged
from log_config import get_logger

logger = get_logger()

BASE_URL = "https://officesupply.sa/en/"
RESULT_SELECTOR = "div.ut2-gl__body"
NO_RESULTS_SELECTOR = ".ty-no-items"
PRICE_SELECTOR = "span.ty-price-num"


def search_url(keyword):
    return (
        f"{BASE_URL}?match=all&subcats=Y&pcode_from_q=Y&pshort=Y&pfull=Y"
        f"&pname=Y&pkeywords=Y&search_performed=Y&dispatch=products.search&q={quote(keyword)}"
    )


def parse_search_results(html):
    """Product URLs of a search results page, in page order ([] for 'No products found').

    A page with neither results nor the no-results notice raises ``SiteLayoutChanged``.
    """
    soup = BeautifulSoup(html, 'html.parser')
    containers = soup.select(RESULT_SELECTOR)
    if not containers:
        if soup.select_one(NO_RESULTS_SELECTOR):
            return []
        raise SiteLayoutChanged("OfficeSupply search page without results or no-results notice")
    urls = []
    for container in containers:
        link_tag = container.select_one("a.product_icon_lnk")
        if link_tag and link_tag.has_attr('href'):
            url = urljoin(BASE_URL, link_tag['href'])
            if url not in urls:
                urls.append(url)
    if not urls:
        raise SiteLayoutChanged("OfficeSupply result cards without 'a.product_icon_lnk' links")
    return urls


def extract_price(soup):
    """Price text with 2 decimals from the CS-Cart price block (the decimals are in a <sup>)."""
    container = soup.select_one('span.ty-price bdi, .ty-price bdi') or soup.select_one('h1 bdi')
    if not container:
        return None
    spans = container.select('span.ty-price-num')
    digit_spans = [s for s in spans if re.search(r'\d', s.get_text())]
    target = None
    if len(digit_spans) >= 1:
        target = digit_spans[0]
    elif spans:
        target = spans[-1]
    if not target:
        return None
    sup = target.find('sup')
    full_digits = ''.join(re.findall(r'\d+', target.get_text()))
    dec = ''.join(re.findall(r'\d+', sup.get_text())) if sup else ''
    if sup and dec and full_digits.endswith(dec):
        integer_part = full_digits[:-len(dec)] or full_digits
    else:
        integer_part = full_digits
    if not integer_part:
        return None
    price = f"{integer_part}.{dec}" if dec else integer_part
    if re.match(r'^\d+$', price):
        price = price + '.00'
    return price


def parse_product_page(html, product_url, search_mode):
    details = {
        'Product': 'Not found', 'Price_SAR': '0.00', 'Company': 'Brand not found',
        'URL': product_url, 'Unit of measurement': 'units', 'Total quantity': 0
    }
    soup = BeautifulSoup(html, 'html.parser')
    name_tag = soup.select_one("div.ut2-pb__title h1")
    if name_tag:
        details['Product'] = name_tag.get_text(strip=True)

    extracted_price = extract_price(soup)
    if extracted_price:
        details['Price_SAR'] = extracted_price
    else:
        logger.warning(f"        -> Could not extract the price of {product_url}.")

    if details['Product'] != 'Not found':
        parse = parse_count_string if search_mode == 'units' else parse_volume_with_multiplier
        parsed_data = parse(details['Product'])
        if parsed_data:
            details['Total quantity'] = parsed_data['quantity']
            details['Unit of measurement'] = parsed_data['unit']
    return details


class OfficeSupplyScraper:
    def __init__(self, browser, relevance_agent):
        self.browser = browser
        self.relevance_agent = relevance_agent
        self.products_to_find_limit = 10

    def scrape(self, keyword, search_mode):
        logger.info(f"  [OfficeSupply Scraper] Searching: '{keyword}' (Mode: {search_mode})")
        with self.browser.page() as page:
            html = fetch_html(page, search_url(keyword), wait_for=f"{RESULT_SELECTOR}, {NO_RESULTS_SELECTOR}")
            product_urls = parse_search_results(html)
            logger.info(f"    > {len(product_urls)} products on the results page.")

            def details(url):
                return parse_product_page(fetch_html(page, url, wait_for=PRICE_SELECTOR), url, search_mode)

            return collect_products(product_urls, details, keep_if_relevant(self.relevance_agent, keyword),
                                    self.products_to_find_limit, 'OfficeSupply')
