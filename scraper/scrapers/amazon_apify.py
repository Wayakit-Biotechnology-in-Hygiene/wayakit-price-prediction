"""Amazon KSA through the Apify actor `junglee/amazon-crawler` (F6; replaces the Selenium scraper).

One actor run per `scrape()` call, with the search URL `https://www.amazon.sa/s?k=<keyword>`.
`countryCode` and `zipCode` are never passed: they cost USD 0.05 more per result.

Conversion (`convert_item`) drops, in order: results not loaded from Saudi Arabia, sponsored
results, items without a title, without a current price (`price.value`; `listPrice` is never
read) or not in SAR, and Wayakit's own products. The size follows the 2-of-N rule: at least two
of {title, Item Volume, Size, Unit Count} must agree on the pack total, and per-unit attributes
never outvote a title that states a pack (`amazon_size`). Then the shared step in
`apify_common.select_relevant` drops rows without a size and asks the relevance agent.

Contract: `__init__(driver_path, relevance_agent, client=None)`; `scrape(keyword, search_mode)`
returns the scraper-contract dicts plus `Seller` and `Origin`. `__init__` reads no secret: the
shared Apify client is resolved on the first `scrape()`. Errors are not caught here; they reach
`scraper/main.py` (fatal vs per-keyword).
"""
import collections
import re
import urllib.parse

import config
from log_config import get_logger
from scrapers.apify_common import (agreed_source, clean, flatten_text, is_sar, is_wayakit,
                                   pack_count, parse_count, parse_price, parse_volume,
                                   select_relevant, sizes_agree, to_row)
from services import apify_client

logger = get_logger()

SEARCH_URL = "https://www.amazon.sa/s?"
COMPANY_NOT_FOUND = "Company not found"  # same default as the old Selenium scraper

_INVISIBLE = str.maketrans("", "", "‎‏﻿")
_VISIT_STORE_RE = re.compile(r"^visit\s+the\s+(.+?)\s+store$", re.I)
_BRAND_PREFIX_RE = re.compile(r"^brand\s*:\s*", re.I)


def build_input(keyword: str) -> dict:
    """The exact actor input. Never add countryCode or zipCode."""
    return {
        "categoryOrProductUrls": [{"url": SEARCH_URL + urllib.parse.urlencode({"k": keyword})}],
        "maxItemsPerStartUrl": config.APIFY_MAX_RESULTS,
        "maxSearchPagesPerStartUrl": 2,
        "scrapeProductDetails": True,
        "language": "en",
        "maxOffers": 0,
        "scrapeSellers": False,
        "useCaptchaSolver": False,
    }


def _attr_key(key):
    return " ".join(str(key).translate(_INVISIBLE).strip().rstrip(":").split()).lower()


def _attr_value(value):
    if isinstance(value, (dict, list, tuple)):
        return clean(flatten_text(value))
    return clean(value).translate(_INVISIBLE).strip()


def amazon_attributes(item: dict) -> dict:
    """`attributes`, `attributesMapped` and `productOverview` merged into one dict with
    normalized lowercase keys; the first value seen wins. Each source may be a dict or a list of
    {"key"|"name": ..., "value": ...}: the exact nesting is not confirmed yet."""
    merged = {}
    for source in (item.get("attributes"), item.get("attributesMapped"), item.get("productOverview")):
        if isinstance(source, dict):
            pairs = source.items()
        elif isinstance(source, list):
            pairs = []
            for entry in source:
                if isinstance(entry, dict):
                    key = entry.get("key", entry.get("name"))
                    if key is not None:
                        pairs.append((key, entry.get("value")))
        else:
            continue
        for key, value in pairs:
            key = _attr_key(key)
            if key and key not in merged:
                merged[key] = _attr_value(value)
    return merged


def _company(brand):
    company = _BRAND_PREFIX_RE.sub("", clean(brand))
    store = _VISIT_STORE_RE.match(company)
    if store:
        company = store.group(1).strip()
    return company or COMPANY_NOT_FOUND


def _seller(item):
    seller = item.get("seller")
    if isinstance(seller, dict):
        return clean(seller.get("name"))
    if isinstance(seller, str):
        return clean(seller)
    return ""


def _number_of_items(value):
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n >= 1 else None


def amazon_size(title, attrs: dict):
    """Volume-mode size of an Amazon item: `("ml" | "fl oz", pack total)` or None (dropped as
    'no_size').

    2-of-N rule: at least two of {title, Item Volume, Size, Unit Count} agree on the pack total.
    Amazon often fills Item Volume and Size per unit for a multipack, so two attributes can agree
    on the size of one unit while the title states the pack ("500ml, Pack of 3"). When the agreed
    value comes from the attributes and the title states a pack (> 1), it is only used if the
    attributes times the title's pack equal the title's own total (then that total is returned);
    otherwise the item is ambiguous and dropped. A per-unit size is never returned for a pack.
    """
    found = agreed_source([title, attrs.get("item volume") or attrs.get("volume"),
                           attrs.get("size"), attrs.get("unit count")])
    if found is None:
        return None
    index, size = found
    if index == 0:
        return size  # the title agrees with an attribute
    pack = pack_count(title)
    if pack <= 1:
        return size
    title_size = parse_volume(title)
    if title_size is not None and sizes_agree((size[0], size[1] * pack), title_size):
        return title_size
    return None


def convert_item(item: dict, search_mode: str):
    """`(candidate, "")` for a kept item, `(None, reason)` for a dropped one."""
    if item.get("loadedCountryCode") != "SA":
        return None, "not_sa"
    page_data = item.get("categoryPageData")
    if item.get("isSponsored") or (isinstance(page_data, dict) and page_data.get("isSponsored")):
        return None, "sponsored"
    product = clean(item.get("title"))
    if not product:
        return None, "no_title"

    price = item.get("price")
    if isinstance(price, dict):
        value, currency = price.get("value"), price.get("currency")
    else:
        value, currency = price, item.get("currency")
    price_sar = parse_price(value)
    if price_sar is None:
        return None, "no_price"
    if not is_sar(currency):
        return None, "currency"

    attrs = amazon_attributes(item)
    seller = _seller(item)
    if is_wayakit(item.get("brand"), seller, product, attrs.get("manufacturer")):
        return None, "wayakit"

    asin = clean(item.get("asin"))
    url = clean(item.get("url")) or (f"https://www.amazon.sa/dp/{asin}" if asin else "")

    if search_mode == "units":
        unit = "units"
        quantity = parse_count(product)
        unit_count = attrs.get("unit count")
        if quantity is None and unit_count and "count" in unit_count.lower():
            quantity = parse_count(unit_count)
        if quantity is None and attrs.get("number of items"):
            quantity = _number_of_items(attrs["number of items"])
    else:
        size = amazon_size(product, attrs)
        unit, quantity = size if size else ("ml", None)

    country = next((v for k, v in attrs.items() if "country" in k and "origin" in k and v), None)
    candidate = {
        "Product": product,
        "Price_SAR": price_sar,
        "Company": _company(item.get("brand")),
        "URL": url,
        "Unit of measurement": unit,
        "Total quantity": quantity,
        "Seller": seller,
        "_key": asin or url or None,
        "_origin": {
            "country_of_origin": country,
            "texts": (product, flatten_text(attrs), flatten_text(item.get("description")),
                      flatten_text(item.get("features"))),
            "noon_global": None,
        },
    }
    return candidate, ""


class AmazonApifyScraper:
    def __init__(self, driver_path, relevance_agent, client=None):
        self.driver_path = driver_path  # unused; kept for the scraper contract
        self.relevance_agent = relevance_agent
        self._client = client

    def scrape(self, keyword: str, search_mode: str) -> list:
        client = self._client or apify_client.shared_client()
        items = client.run_actor(config.APIFY_AMAZON_ACTOR, build_input(keyword),
                                 max_items=config.APIFY_MAX_RESULTS,
                                 max_usd=config.APIFY_AMAZON_MAX_USD,
                                 timeout_s=config.APIFY_RUN_TIMEOUT_S)[:config.APIFY_MAX_RESULTS]
        reasons = collections.Counter()
        candidates = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                reasons["not_a_dict"] += 1
                continue
            candidate, reason = convert_item(item, search_mode)
            if candidate is None:
                reasons[reason] += 1
                continue
            key = candidate["_key"]
            if key is not None and key in seen:
                reasons["duplicate"] += 1
                continue
            if key is not None:
                seen.add(key)
            candidates.append(candidate)
        rows, more = select_relevant(candidates, keyword, search_mode, self.relevance_agent)
        logger.info(f"   [amazon] {len(items)} items, drops {dict(reasons + more)}, {len(rows)} rows")
        return [to_row(c) for c in rows]
