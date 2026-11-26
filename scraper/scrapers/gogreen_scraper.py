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

In B2C (``set_channel('B2C')``, called by ``run_scraping`` for Home) only sizes up to
``config.GOGREEN_B2C_MAX_LITERS`` (4 L) are kept, before the relevance AI: Wayakit's B2C sizes stop
at 4 L and GoGreen's 5, 20 and 30 L jerrycans would pull the B2C price per liter down. B2B keeps
every size.

The cap of ``products_to_find_limit`` products per search counts **distinct** products (Mafer,
2026-11-25): identical variants of one product count once and only the first, in catalog order, is
kept. A variant is a product with the same title once a trailing scent or colour ("... 3 لتر -
ليمون") or a "بعطر ..." / "برائحة ..." scent phrase is ignored, and the same size (checked before
its page is opened), or a product with the same size and the same price (checked on its page). The
search goes on through the relevant list until the cap is reached or the list ends (on 2026-11-25
five scents of one 3 L floor cleaner at 45 SAR filled the whole cap). A product page whose own name
states a different size, or a carton the catalog title does not state, is dropped: the catalog size
is in doubt.

Three of Mafer's competitor decisions (2026-11-25) are applied to the catalog titles before the
relevance AI, in this scraper only (``query_rule_rejects``; the shared relevance prompt, which SACO
also uses, is unchanged): for a disinfectant or sanitizer query, chlorine bleach (bleach, chlorine,
hypochlorite, كلور, مبيض; not "bleach-free" / "بدون كلور") is not a competitor unless the query
itself asks for bleach, and neither is a carpet or upholstery product (a carpet shampoo "تنظيف
وتعقيم" is not a disinfectant); for a carpet or upholstery stain or spot remover query, a laundry
stain remover (clothes, ملابس, غسيل) that does not name carpets or upholstery is not a competitor.
"""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import config

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
_LITERS = ("لترات", "لتر", "ليتر")
# A scent or colour variant: a trailing "- ليمون" without digits or brackets, or a scent phrase
# ("بعطر الليمون", "برائحة العود") up to the size or the end of the title.
_TRAILING_VARIANT = re.compile(r"\s*-\s*[^\d()\-]+$")
_SCENT_PHRASE = re.compile(r"(?:^|\s)(?:بعطر|برائحة|بريحة)\s[^\d()]*?(?=\s*\d|\s*\(|$)")
_CARTON = re.compile(r"كرتون|كراتين")  # also كرتونة, كرتونات

# Mafer's decisions of 2026-11-25, as title rules (``query_rule_rejects``). An Arabic word may carry
# the prefixes و / ب / ل / ف and the article ال (or لل, "للكلور"); it is not part of a longer word
# ("كلوريد", chloride, is not "كلور"). Diacritics and tatweel are removed first ("مبيّض").
_AR = "؀-ۿ"
_AR_MARKS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_DISINFECT_QUERY = re.compile(r"disinfect|saniti[sz]|مطهر|معقم|تعقيم", re.I)
_BLEACH_QUERY = re.compile(r"bleach|chlorine|hypochlorite|كلور|مبيض", re.I)
_BLEACH_FREE = re.compile(r"(?:bleach|chlorine)[\s-]*free|(?:no|without|non)[\s-]+(?:bleach|chlorine)"
                          r"|(?:بدون|خالي من|خال من|خالية من)\s*(?:ال)?(?:كلور|مبيض)", re.I)
_BLEACH = re.compile(r"bleach|\bchlorine\b|hypochlorite"
                     rf"|(?<![{_AR}])(?:[وف]?لل|[وبلف]?(?:ال)?)"
                     r"(?:كلور|كلورين|كلوركس|مبيض|مبيضة|هيبوكلوريت|هيبوكلورايت|هايبوكلوريت|هايبوكلورايت)"
                     rf"(?![{_AR}])", re.I)
# Furniture (أثاث) alone is not upholstery ("مطهر ومعقم للأسطح والأثاث" is a surface disinfectant);
# furniture fabric (قماش / أقمشة الأثاث) and upholstering (تنجيد) are.
_CARPET = re.compile(r"carpet|\brugs?\b|upholster|sofa|couch|سجاد|موكيت|مفروشات|كنب|تنجيد"
                     r"|(?:قماش|أقمشة|اقمشة)\s+(?:ال)?(?:أثاث|اثاث)", re.I)
_STAIN_QUERY = re.compile(r"stain|spot|بقع", re.I)
_LAUNDRY = re.compile(r"\bclothes\b|\bclothing\b|laundry|garment|ملابس|غسيل|غسالة", re.I)


def normalize_digits(text):
    """Arabic-Indic and Persian digits (and the Arabic decimal separator) as ASCII."""
    return (text or "").translate(_DIGITS)


def _plain(text):
    """Digits as ASCII, Arabic diacritics and tatweel removed, spaces collapsed (``query_rule_rejects``)."""
    return " ".join(_AR_MARKS.sub("", normalize_digits(text)).split())


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
    # A carton with no count of bottles that can be read ("4 كراتين 5 لتر", "كرتون منظف 5 لتر"):
    # the price is for more than the size in the title, so there is no size (drop when in doubt).
    if _CARTON.search(text) and not counts:
        return None
    multiplier = int(counts[0][0]) if counts else 1
    size, word = float(sizes[0][0]), sizes[0][1]
    if size <= 0 or multiplier <= 0:
        return None
    liters = word in _LITERS
    quantity = size * multiplier
    return {"quantity": quantity, "unit": "L" if liters else "ml",
            "normalized": quantity * 1000 if liters else quantity}


def title_size(title, search_mode):
    """The Arabic rules for an Arabic title; the usual English parsers otherwise."""
    if _ARABIC.search(title or ""):
        return arabic_size(title, search_mode)
    parse = parse_count_string if search_mode == "units" else parse_volume_with_multiplier
    return parse(title)


def variant_key(title):
    """The title without a trailing scent or colour ("منظف الارضيات 3 لتر - ليمون") and without a
    scent phrase ("بعطر الليمون", "برائحة العود"), digits normalized, spaces collapsed, lower case.
    Two scents of one product give the same key; a trailing part with a digit or a bracket
    ("- سعة 1 لتر", "- تنظيف وتعقيم (2 لتر)") is part of the product name and is kept."""
    text = " ".join(normalize_digits(title).split())
    text = _TRAILING_VARIANT.sub("", text)
    text = _SCENT_PHRASE.sub(" ", text)
    return " ".join(text.split()).lower()


def query_rule_rejects(title, keyword):
    """Why ``title`` is not a competitor for the search ``keyword`` under Mafer's decisions of
    2026-11-25, or ``None``. Applied before the relevance AI, in GoGreen only:

    - a disinfectant or sanitizer query: chlorine bleach is rejected unless the query asks for
      bleach or chlorine ("bleach-free", "بدون كلور" is not bleach), and so is a carpet or
      upholstery product (a carpet shampoo that says "تعقيم") unless the query names carpets;
    - a stain or spot remover query that names carpets or upholstery: a laundry stain remover
      (clothes, ملابس, غسيل) is rejected unless its title also names carpets or upholstery.
    """
    title, keyword = _plain(title), _plain(keyword)
    if _DISINFECT_QUERY.search(keyword):
        if not _BLEACH_QUERY.search(keyword) and _BLEACH.search(_BLEACH_FREE.sub(" ", title)):
            return "chlorine bleach is not a disinfectant competitor"
        if not _CARPET.search(keyword) and _CARPET.search(title):
            return "a carpet or upholstery product is not a disinfectant competitor"
    if _STAIN_QUERY.search(keyword) and _CARPET.search(keyword):
        if _LAUNDRY.search(title) and not _CARPET.search(title):
            return "a laundry stain remover is not a carpet or upholstery stain remover"
    return None


def _normalized(size):
    return size.get("normalized", size.get("quantity")) if size else None


def _price(text):
    """The product dict's ``Price_SAR`` as a number; 0.0 when it is not one."""
    try:
        return float(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def page_contradicts_catalog(page_name, title, size):
    """True when the product page's own name clearly disagrees with the catalog title: it states
    a carton (كرتون) the catalog title does not, or it has one size of its own and that size is not
    the catalog size. A page name without a size, or with two, says nothing and is not a
    contradiction."""
    page_name = " ".join((page_name or "").split())
    if not page_name:
        return False
    if _CARTON.search(page_name) and not _CARTON.search(title or ""):
        return True
    page_size = title_size(page_name, "units" if size.get("unit") == "units" else "volume")
    if page_size is None:
        return False
    return abs(float(_normalized(page_size)) - float(_normalized(size))) > 1e-6


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
    """The scraper-contract dict: the catalog title and size, the price from the product page.

    ``None`` when the page's own name states another size or a carton than the catalog title
    (``page_contradicts_catalog``): a dropped row is better than a wrong volume."""
    soup = BeautifulSoup(html, "html.parser")
    name = soup.select_one(NAME_SELECTOR)
    if not name:
        raise SiteLayoutChanged(f"no product name at {product_url}")
    if page_contradicts_catalog(name.get_text(" "), title, size):
        logger.info(f"      -> DISCARDED (product page states another size or a carton): {title[:60]}")
        return None
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
        self.max_liters = None  # set_channel('B2C') sets config.GOGREEN_B2C_MAX_LITERS
        self._catalog = None

    def set_channel(self, channel):
        """B2C keeps sizes up to ``config.GOGREEN_B2C_MAX_LITERS``; B2B keeps every size."""
        self.max_liters = config.GOGREEN_B2C_MAX_LITERS if channel == "B2C" else None

    def _fits_channel(self, title, size):
        """Volume sizes (``normalized`` in ml) up to ``max_liters``. A units-mode count has no
        liter limit, but a pack of liquid bottles or jerrycans sized in liters ("12 حبة (1لتر)",
        "4 حبات 20 لتر") is still dropped in B2C when its liquid volume is above the limit."""
        if self.max_liters is None:
            return True
        limit = self.max_liters * 1000
        if size["unit"] != "units":
            return size["normalized"] <= limit
        volume = title_size(title, "volume")
        singles = [float(quantity) * (1000 if word in _LITERS else 1)
                   for quantity, word in _SIZE.findall(normalize_digits(title))]
        return (volume is None or volume["normalized"] <= limit) and all(v <= limit for v in singles)

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
                     for size in [title_size(title, search_mode)] if size and self._fits_channel(title, size)]
            limit = f" up to {self.max_liters} L" if self.max_liters else ""
            logger.info(f"    > {len(sized)} of {len(catalog)} catalog products have a size{limit}.")
            allowed = []
            for item in sized:
                reason = query_rule_rejects(item[1], keyword)
                if reason:
                    logger.info(f"      -> DISCARDED ({reason}): {item[1][:60]}")
                else:
                    allowed.append(item)
            sized = allowed
            if not sized:
                return []
            answers = self.relevance_agent.filter_relevant([title for _, title, _ in sized], keyword)
            relevant = [item for item, ok in zip(sized, answers) if ok]
            logger.info(f"    > {len(relevant)} relevant to '{keyword}'.")
            by_url = {url: (title, size) for url, title, size in relevant}
            kept_variants, kept_size_prices = set(), set()

            def details(url):
                title, size = by_url[url]
                if (variant_key(title), _normalized(size)) in kept_variants:
                    logger.info(f"      -> SKIPPED (scent or colour variant of a kept product): {title[:60]}")
                    return None  # its page is not opened
                return parse_product_page(fetch_html(page, url, wait_for=NAME_SELECTOR), url, title, size)

            def keep(product):
                """Distinct products only: same size and same price as a kept product is a variant."""
                if product is None:
                    return False
                title, size = by_url[product["URL"]]
                price = _price(product.get("Price_SAR"))
                size_price = (_normalized(size), price)
                if price > 0 and size_price in kept_size_prices:
                    logger.info(f"      -> DISCARDED (same size and price as a kept product): {title[:60]}")
                    return False
                kept_variants.add((variant_key(title), _normalized(size)))
                if price > 0:
                    kept_size_prices.add(size_price)
                return True

            return collect_products([url for url, _, _ in relevant], details, keep,
                                    self.products_to_find_limit, "GoGreen")
