"""GoGreen (gogreen.com.sa, a Zid store): the whole catalog, read in Arabic.

Since November 2026 the store is Arabic-only and its search ignores the query (any search,
even one that matches nothing, lists the same products; checked 2026-11-25). The catalog is
small (about 170 products in 7 pages), so each search reads the catalog listing once, keeps the
titles with a size, and lets the relevance AI pick the ones that match the (English) keyword:
the models read Arabic titles against an English query. Only the selected products' pages are
opened, for the price.

Sizes come from the Arabic title (``arabic_size``), conservatively: one liquid size (لتر = L,
مل / ملي / ملل = ml) and an optional count of the same product ("12 حبة (1لتر)" = 12 L); a title
with two sizes ("3 لتر و مزيل بقع 1 لتر"), a set (مجموعة), a free gift or a "+" bundle, or a
weight (جرام, كجم) gives no size and is dropped.
"""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from utils import parse_volume_with_multiplier, parse_count_string
from services.browser import (fetch_html, collect_products, HttpStatusError, SiteLayoutChanged)
from log_config import get_logger

logger = get_logger()

BASE_URL = "https://gogreen.com.sa/"
RESULT_SELECTOR = "div.product-box"
LINK_SELECTOR = "a.tp-image-product"
NAME_SELECTOR = "h1.title-product"
PRICE_SELECTOR = "p.product-formatted-price"
MAX_CATALOG_PAGES = 20

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫", "01234567890123456789.")
_ARABIC = re.compile(r"[؀-ۿ]")
# Liquid sizes: لتر / ليتر / لترات = L; مل / ملي / ملل / مللي = ml. Not followed by another letter.
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(لترات|لتر|ليتر|مللي|ملل|ملي|مل)(?![\w])")
# A count of the same product: "12 حبة", "3 حبات", "4 قطع".
_COUNT = re.compile(r"(\d+)\s*(حبات|حبة|قطعة|قطع|عبوات|عبوة)(?![\w])")
_UNITS = re.compile(r"(\d+)\s*(مناديل|منديل|حبات|حبة|قطعة|قطع)(?![\w])")
# A set, a package of different products, a free gift, a special offer or a "+" bundle.
_KIT = re.compile(r"مجموعة|بكج|باقة|مجان|هدية|عرض|\+")
_WEIGHT = re.compile(r"\d\s*(جرام|غرام|جم|كجم|كغ|كيلو)(?![\w])")


def normalize_digits(text):
    """Arabic-Indic and Persian digits (and the Arabic decimal separator) as ASCII."""
    return (text or "").translate(_DIGITS)


def arabic_size(title, search_mode):
    """``{'quantity', 'unit', 'normalized'}`` from an Arabic title, or ``None`` when in doubt.

    Volume mode: exactly one liquid size, times an optional count of the same product. Units mode:
    a count ("100 منديل"). A kit, a gift, a weight or two sizes give ``None``.
    """
    text = normalize_digits(title)
    if _KIT.search(text) or _WEIGHT.search(text):
        return None
    if search_mode == "units":
        counts = _UNITS.findall(text)
        if len(counts) != 1:
            return None
        quantity = int(counts[0][0])
        return {"quantity": quantity, "unit": "units", "normalized": quantity} if quantity > 0 else None
    sizes = _SIZE.findall(text)
    if len(sizes) != 1:
        return None
    counts = _COUNT.findall(text)
    if len(counts) > 1:
        return None
    multiplier = int(counts[0][0]) if counts else 1
    size, word = float(sizes[0][0]), sizes[0][1]
    if size <= 0 or multiplier <= 0:
        return None
    liters = word in ("لترات", "لتر", "ليتر")
    quantity = size * multiplier
    return {"quantity": quantity, "unit": "L" if liters else "ml",
            "normalized": quantity * 1000 if liters else quantity}


def title_size(title, search_mode):
    """The Arabic rules for an Arabic title; the usual English parsers otherwise."""
    if _ARABIC.search(title or ""):
        return arabic_size(title, search_mode)
    parse = parse_count_string if search_mode == "units" else parse_volume_with_multiplier
    return parse(title)


def parse_catalog_page(html):
    """``[(url, title)]`` of a catalog listing page, in page order (the title is the image's alt).

    Boxes without the product link, or without a title, raise ``SiteLayoutChanged``.
    """
    soup = BeautifulSoup(html, "html.parser")
    products = []
    boxes = soup.select(RESULT_SELECTOR)
    for box in boxes:
        link = box.select_one(LINK_SELECTOR)
        if not (link and link.has_attr("href")):
            continue
        image = link.select_one("img")
        title = " ".join((image.get("alt") or image.get("title") or "").split()) if image else ""
        if not title:
            continue
        url = urljoin(BASE_URL, link["href"])
        if url not in [u for u, _ in products]:
            products.append((url, title))
    if boxes and not products:
        raise SiteLayoutChanged("GoGreen product boxes without a product link and title")
    return products


def parse_product_page(html, product_url, title, size):
    """The scraper-contract dict: the catalog title and size, the price from the product page."""
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select_one(NAME_SELECTOR):
        raise SiteLayoutChanged(f"no product name at {product_url}")
    details = {
        "Product": title, "Price_SAR": "0.00", "Company": "GoGreen", "URL": product_url,
        "Unit of measurement": size["unit"], "Total quantity": size["quantity"],
    }
    price_tag = soup.select_one(PRICE_SELECTOR)
    if price_tag:
        # the theme shows the price without decimals (e.g. '182'), in Western digits
        price_text = re.search(r"([\d,]+(?:\.\d{1,2})?)", normalize_digits(price_tag.get_text()))
        if price_text:
            details["Price_SAR"] = price_text.group(1).replace(",", "")
    return details


class GoGreenScraper:
    def __init__(self, browser, relevance_agent):
        self.browser = browser
        self.relevance_agent = relevance_agent
        self.products_to_find_limit = 5
        self._catalog = None

    def _load_catalog(self, page):
        """Every ``(url, title)`` of the store, read once per scraper. The page after the last one
        answers 404; a first page without products raises ``SiteLayoutChanged``."""
        if self._catalog is not None:
            return self._catalog
        catalog = []
        for number in range(1, MAX_CATALOG_PAGES + 1):
            url = urljoin(BASE_URL, f"products?page={number}")
            try:
                html = fetch_html(page, url, wait_for=RESULT_SELECTOR)
            except HttpStatusError as error:
                if error.status == 404 and number > 1:
                    break
                raise
            new = [p for p in parse_catalog_page(html) if p[0] not in {u for u, _ in catalog}]
            if not new:
                break
            catalog += new
        if not catalog:
            raise SiteLayoutChanged("GoGreen catalog without products")
        logger.info(f"    > GoGreen catalog: {len(catalog)} products.")
        self._catalog = catalog
        return catalog

    def scrape(self, keyword, search_mode):
        logger.info(f"  [GoGreen Scraper] Searching: '{keyword}' (Mode: {search_mode})")
        with self.browser.page() as page:
            catalog = self._load_catalog(page)
            sized = [(url, title, size) for url, title in catalog
                     for size in [title_size(title, search_mode)] if size]
            logger.info(f"    > {len(sized)} of {len(catalog)} catalog products have a size.")
            if not sized:
                return []
            answers = self.relevance_agent.filter_relevant([title for _, title, _ in sized], keyword)
            relevant = [item for item, ok in zip(sized, answers) if ok]
            logger.info(f"    > {len(relevant)} relevant to '{keyword}'.")
            by_url = {url: (title, size) for url, title, size in relevant}

            def details(url):
                title, size = by_url[url]
                return parse_product_page(fetch_html(page, url, wait_for=NAME_SELECTOR), url, title, size)

            return collect_products([url for url, _, _ in relevant], details, lambda _: True,
                                    self.products_to_find_limit, "GoGreen")
