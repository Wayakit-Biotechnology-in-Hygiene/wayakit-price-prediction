"""Noon KSA through the Apify actor `saswave/noon-product-scraper` (F6).

One search run per keyword (the actor does not return the keyword, so keywords are never
mixed in one run) on `https://www.noon.com/saudi-en/search/?q=<keyword>`. The actor does not
return a currency either: SAR is assumed from `/saudi-en/`, and an explicit non-SAR currency is
dropped. Its output is always cut to `config.APIFY_MAX_RESULTS` locally because it returns the
whole page whatever the cap.

Conversion (`convert_item`) drops items without a name, without a SKU, without a price
(`sale_price` if present, else `price`), not in SAR, and Wayakit's own products. The size comes
from the title only (in volume mode an ambiguous title has no size: a count of 2 or more such as
"28 oz, 4 Count", a pack notation the parser does not read, a kit of different products, a dose
or a capacity; and only a size in ml, liters or fl oz counts, never a plain oz or a gallon alone;
`apify_common.title_volume`), and so does `origin` (a "made in KSA" title is local, anything
else unknown). There is no details run: `shipping_banner_details.global` is the
same generic fee message on every product, local ones included (2026-11-23 smoke run), so it is
not an origin signal, and the search items already carry `store_name` for the Wayakit exclusion
(Mafer, 2026-11-23).

Contract: `__init__(driver_path, relevance_agent, client=None)`; `scrape(keyword, search_mode)`
returns the scraper-contract dicts plus `Seller` and `Origin`. `__init__` reads no secret.
"""
import collections
import re
import urllib.parse

import config
from log_config import get_logger
from scrapers.apify_common import (clean, is_sar, is_wayakit, parse_count, parse_price,
                                   select_relevant, title_volume, to_row)
from services import apify_client

logger = get_logger()

NOON_BASE = "https://www.noon.com"
SEARCH_URL = "https://www.noon.com/saudi-en/search/?q="
PRODUCT_URL = "https://www.noon.com/saudi-en/{slug}/{sku}/p/"
COMPANY_NOT_FOUND = "Company not found"
_SKU_IN_URL_RE = re.compile(r"/([A-Za-z0-9]+)/p/?")


def build_search_input(keyword: str) -> dict:
    return {"search_urls": [SEARCH_URL + urllib.parse.quote(keyword)], "max_pages": 1,
            "product_urls": []}


def product_url(item: dict):
    """The product URL: `url` if it is absolute, joined to noon.com if it is a path
    ("/saudi-en/.../p/"), else built from the bare slug (`url`) and `sku`; None without a SKU."""
    url = clean(item.get("url"))
    if url.lower().startswith("http"):
        return url
    sku = clean(item.get("sku"))
    if not sku:
        return None
    if "/" in url:
        # A path ("/saudi-en/dry-shampoo/ZX/p/"), not a bare slug: never wrap it in PRODUCT_URL.
        return NOON_BASE + "/" + url.lstrip("/")
    slug = url
    if not slug:
        return f"https://www.noon.com/saudi-en/{sku}/p/"
    return PRODUCT_URL.format(slug=slug, sku=sku)


def _sku_of(item):
    sku = clean(item.get("sku"))
    if sku:
        return sku
    match = _SKU_IN_URL_RE.search(clean(item.get("url")))
    return match.group(1) if match else ""


def convert_item(item: dict, search_mode: str):
    """`(candidate, "")` for a kept item, `(None, reason)` for a dropped one."""
    product = clean(item.get("name"))
    if not product:
        return None, "no_title"
    url = product_url(item)
    if url is None:
        return None, "no_sku"
    price_sar = parse_price(item.get("sale_price"))
    if price_sar is None:
        price_sar = parse_price(item.get("price"))
    if price_sar is None:
        return None, "no_price"
    currency = item.get("currency")
    if currency not in (None, "") and not is_sar(currency):
        return None, "currency"
    if is_wayakit(item.get("brand"), item.get("store_name"), product):
        return None, "wayakit"

    if search_mode == "units":
        unit, quantity = "units", parse_count(product)
    else:
        # The title is the only source: an ambiguous title (a count, a kit of different products,
        # a dose...) has no size, and a plain oz or a gallon alone is not enough.
        size = title_volume(product, explicit_only=True)
        unit, quantity = size if size else ("ml", None)

    candidate = {
        "Product": product,
        "Price_SAR": price_sar,
        "Company": clean(item.get("brand")) or COMPANY_NOT_FOUND,
        "URL": url,
        "Unit of measurement": unit,
        "Total quantity": quantity,
        "Seller": clean(item.get("store_name")),
        "_sku": _sku_of(item) or _sku_of({"url": url}) or url,  # dedupe/join key
        "_origin": {"country_of_origin": None, "texts": (product,)},
    }
    return candidate, ""


class NoonApifyScraper:
    def __init__(self, driver_path, relevance_agent, client=None):
        self.driver_path = driver_path  # unused; kept for the scraper contract
        self.relevance_agent = relevance_agent
        self._client = client

    def scrape(self, keyword: str, search_mode: str) -> list:
        client = self._client or apify_client.shared_client()
        items = client.run_actor(config.APIFY_NOON_ACTOR, build_search_input(keyword),
                                 max_items=config.APIFY_MAX_RESULTS,
                                 max_usd=config.APIFY_NOON_MAX_USD,
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
            if candidate["_sku"] in seen:
                reasons["duplicate"] += 1
                continue
            seen.add(candidate["_sku"])
            candidates.append(candidate)

        rows, more = select_relevant(candidates, keyword, search_mode, self.relevance_agent)

        logger.info(f"   [noon] {len(items)} items, drops {dict(reasons + more)}, {len(rows)} rows")
        return [to_row(c) for c in rows]
