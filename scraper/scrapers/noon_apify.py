"""Noon KSA through the Apify actor `saswave/noon-product-scraper` (F6).

One search run per keyword (the actor does not return the keyword, so keywords are never
mixed in one run) on `https://www.noon.com/saudi-en/search/?q=<keyword>`. The actor does not
return a currency either: SAR is assumed from `/saudi-en/`, and an explicit non-SAR currency is
dropped. Its output is always cut to `config.APIFY_MAX_RESULTS` locally because it returns the
whole page whatever the cap.

Conversion (`convert_item`) drops items without a name, without a SKU, without a price
(`sale_price` if present, else `price`), not in SAR, and Wayakit's own products. The size comes
from the title only. After `apify_common.select_relevant`, one details run per keyword fetches
the relevant products whose search item had no `shipping_banner_details` object: its `global`
flag is the only Noon origin signal (imported). The Wayakit exclusion runs again on the details
(brand and seller), because a seller can appear only there. A failing details run fails the
keyword; rows are never returned without the details that were asked for.

Contract: `__init__(driver_path, relevance_agent, client=None)`; `scrape(keyword, search_mode)`
returns the scraper-contract dicts plus `Seller` and `Origin`. `__init__` reads no secret.
"""
import collections
import re
import urllib.parse

import config
from log_config import get_logger
from scrapers.apify_common import (clean, flatten_text, is_sar, is_wayakit, parse_count,
                                   parse_price, parse_volume, select_relevant, to_row)
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


def build_details_input(urls: list) -> dict:
    return {"search_urls": [], "max_pages": 1, "product_urls": list(urls)}


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


def _global_flag(item):
    details = item.get("shipping_banner_details")
    return details.get("global") if isinstance(details, dict) else None


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
        size = parse_volume(product)
        unit, quantity = size if size else ("ml", None)

    # A null `shipping_banner_details` carries no flag: only a dict skips the details run.
    has_shipping = isinstance(item.get("shipping_banner_details"), dict)
    candidate = {
        "Product": product,
        "Price_SAR": price_sar,
        "Company": clean(item.get("brand")) or COMPANY_NOT_FOUND,
        "URL": url,
        "Unit of measurement": unit,
        "Total quantity": quantity,
        "Seller": clean(item.get("store_name")),
        "_sku": _sku_of(item) or _sku_of({"url": url}) or url,  # dedupe/join key
        "_has_shipping": has_shipping,
        "_origin": {
            "country_of_origin": None,
            "texts": (product,),
            "noon_global": _global_flag(item) if has_shipping else None,
        },
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

        need = [c for c in rows if not c["_has_shipping"]]
        if need:
            # Only for relevant rows: the global-shipping flag (origin) is only in details.
            urls = [c["URL"] for c in need]
            logger.info(f"   [noon] details run for {len(urls)} of {len(rows)} relevant rows")
            details = client.run_actor(config.APIFY_NOON_ACTOR, build_details_input(urls),
                                       max_items=len(urls), max_usd=config.APIFY_NOON_MAX_USD,
                                       timeout_s=config.APIFY_RUN_TIMEOUT_S)[:config.APIFY_MAX_RESULTS]
            by_sku = {c["_sku"]: c for c in need}
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                candidate = by_sku.get(_sku_of(detail))
                if candidate is None:
                    continue
                if is_wayakit(detail.get("brand"), detail.get("store_name"), candidate["Seller"]):
                    # Our own product, only visible in details: never a competitor row (C09).
                    candidate["_wayakit"] = True
                    continue
                candidate["_origin"]["noon_global"] = _global_flag(detail)
                candidate["_origin"]["texts"] += (flatten_text(detail.get("specifications")),)
                if not candidate["Seller"] and clean(detail.get("store_name")):
                    candidate["Seller"] = clean(detail.get("store_name"))
            kept = [c for c in rows if not c.get("_wayakit")]
            more["wayakit"] += len(rows) - len(kept)
            rows = kept

        logger.info(f"   [noon] {len(items)} items, drops {dict(reasons + more)}, {len(rows)} rows")
        return [to_row(c) for c in rows]
