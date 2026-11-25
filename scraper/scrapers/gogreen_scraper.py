import re
from urllib.parse import urljoin, quote

from bs4 import BeautifulSoup

from utils import parse_volume_with_multiplier, parse_count_string
from services.browser import fetch_html, collect_products, keep_if_relevant, SiteLayoutChanged
from log_config import get_logger

logger = get_logger()

BASE_URL = "https://gogreen.com.sa/"
RESULT_SELECTOR = "div.product-box"
NAME_SELECTOR = "h1.title-product"
# The site runs on the Zid platform: its language dialog only sets this cookie.
LOCALE_COOKIE = {'name': 'locale', 'value': 'en-sa', 'url': BASE_URL}


class LanguageUnavailable(SiteLayoutChanged):
    """The store does not serve English (since November 2026 it is Arabic-only)."""


def page_language(html):
    tag = BeautifulSoup(html, 'html.parser').find('html')
    return (tag.get('lang') or '').lower() if tag else ''


def parse_search_results(html):
    """Product URLs of a search results page.

    The store has no "no results" page: a search that matches nothing lists 24 other products
    (checked 2026-11-25), which the relevance AI then discards. So a page without any product box
    is not an empty search but a changed or blocked page (``scrape()`` waits for the boxes and
    raises ``ElementNotFound``), and boxes without the product link raise ``SiteLayoutChanged``.
    """
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    containers = soup.select(RESULT_SELECTOR)
    for container in containers:
        link_tag = container.select_one("a.tp-image-product")
        if link_tag and link_tag.has_attr('href'):
            url = urljoin(BASE_URL, link_tag['href'])
            if url not in urls:
                urls.append(url)
    if containers and not urls:
        raise SiteLayoutChanged("GoGreen product boxes without 'a.tp-image-product' links")
    return urls


def parse_product_page(html, product_url, search_mode):
    details = {
        'Product': 'Not found', 'Price_SAR': '0.00', 'Company': 'GoGreen',
        'URL': product_url, 'Unit of measurement': 'units', 'Total quantity': 0
    }
    soup = BeautifulSoup(html, 'html.parser')
    name_tag = soup.select_one(NAME_SELECTOR)
    product_name = name_tag.get_text(strip=True) if name_tag else None
    if not product_name:
        raise SiteLayoutChanged(f"no product name at {product_url}")
    details['Product'] = product_name

    price_tag = soup.select_one("p.product-formatted-price")
    if price_tag:
        # the theme shows the price without decimals (e.g. '182')
        price_text = re.search(r'([\d,]+(?:\.\d{1,2})?)', price_tag.get_text())
        if price_text:
            details['Price_SAR'] = price_text.group(1).replace(',', '')

    parse = parse_count_string if search_mode == 'units' else parse_volume_with_multiplier
    parsed_data = parse(product_name)
    if parsed_data:
        details['Total quantity'] = parsed_data['quantity']
        details['Unit of measurement'] = parsed_data['unit']
    return details


class GoGreenScraper:
    def __init__(self, browser, relevance_agent):
        self.browser = browser
        self.relevance_agent = relevance_agent
        self.products_to_find_limit = 5

    def _ensure_english(self, page):
        """Sets the English locale cookie and checks that the store answers in English.

        Titles are parsed for English units and the keywords are English, so an Arabic page
        raises ``LanguageUnavailable`` instead of returning no rows.
        """
        page.context.add_cookies([LOCALE_COOKIE])
        html = fetch_html(page, BASE_URL, wait_for='html')
        language = page_language(html)
        if not language.startswith('en'):
            raise LanguageUnavailable(f"GoGreen serves lang='{language}' with the English locale cookie")

    def scrape(self, keyword, search_mode):
        logger.info(f"  [GoGreen Scraper] Searching: '{keyword}' (Mode: {search_mode})")
        with self.browser.page() as page:
            self._ensure_english(page)
            search_url = urljoin(BASE_URL, f"products?search={quote(keyword)}")
            html = fetch_html(page, search_url, wait_for=RESULT_SELECTOR)
            product_urls = parse_search_results(html)
            logger.info(f"    > {len(product_urls)} products on the results page.")

            def details(url):
                return parse_product_page(fetch_html(page, url, wait_for=NAME_SELECTOR), url, search_mode)

            return collect_products(product_urls, details, keep_if_relevant(self.relevance_agent, keyword),
                                    self.products_to_find_limit, 'GoGreen')
