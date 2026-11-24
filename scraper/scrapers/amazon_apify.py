"""Amazon KSA through the Apify actor `junglee/amazon-crawler` (F6; replaces the Selenium scraper).

One actor run per `scrape()` call, with the search URL `https://www.amazon.sa/s?k=<keyword>`.
`countryCode` and `zipCode` are never passed: they cost USD 0.05 more per result.

Conversion (`convert_item`) drops, in order: results not loaded from Saudi Arabia, sponsored
results, items without a title, without a current price (`price.value`; `listPrice` is never
read) or not in SAR, and Wayakit's own products. The size (`amazon_size`, conservative): two of
{title, Item Volume, Size, Unit Count} agreeing on the pack total (a plain "oz" needs a
confirmation that is not another plain "oz"), else the title's size when it is in ml, liters or
fl oz and no attribute contradicts it; an ambiguous title (a kit of different products, a count,
a dose...), a solid Item Form or an attribute multipack the title does not state gives no size,
and per-unit attributes never outvote a title that states a pack. Then the shared step in
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
from scrapers.apify_common import (PLAIN_OZ, clean, first_agreeing, flatten_text, is_sar, is_wayakit,
                                   parse_count, parse_price, parse_volume, select_relevant, sizes_agree,
                                   title_is_ambiguous, title_volume, to_row, volume_kind, volume_pack)
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


# An Item Form that is only a solid: its oz are weights, never a volume, and Wayakit sells liquids
# only. Amazon's Item Form can list several forms ("Bar, Liquid"): any liquid form wins.
_SOLID_FORM_RE = re.compile(
    r"\b(?:powders?|tablets?|pods?|pacs?|granules?|granular|crystals?|capsules?|flakes?|cubes?|solid|bars?"
    r"|sticks?|paste|wax|beads?)\b", re.I)
_LIQUID_FORM_RE = re.compile(r"\b(?:liquids?|spray|gel|foam|aerosol|cream|lotion|concentrate)\b", re.I)
_AEROSOL_RE = re.compile(r"\baerosols?\b", re.I)
# "1 17 ounce can" (Included Components): a can sized in oz is a net weight, like an aerosol
# (2026-11-23 smoke runs: a 17 oz can stored as 17 fl oz at a case price).
_OZ_CAN_RE = re.compile(r"\d\s*(?:-\s*)?(?:oz|ounces?)\.?\s+(?:aerosol\s+)?cans?\b", re.I)
_UNIT_COUNT_RE = re.compile(r"^\s*(\d{1,4})(?:\.0+)?\s*count\b", re.I)


def _is_solid_form(item_form):
    item_form = str(item_form or "")
    return bool(_SOLID_FORM_RE.search(item_form)) and not _LIQUID_FORM_RE.search(item_form)


def _attribute_pack(attrs):
    """The largest multipack an attribute states: Size "(Pack of N)" or Unit Count "N Count"; 1 if
    none. Number of Items is not here: it is often wrong (`amazon_size`)."""
    packs = [1]
    if attrs.get("size"):
        packs.append(volume_pack(attrs["size"]))
    match = _UNIT_COUNT_RE.match(str(attrs.get("unit count") or ""))
    if match:
        packs.append(int(match.group(1)))
    return max(packs)


def _ml_label_fix(size, title_size):
    """Amazon often writes the fl oz number with a ml label ("16 Milliliters" in Unit Count for a
    title that says 16oz). An attribute in ml is read as fl oz only when the title states that
    same number (within 1 %) in fl oz; the title decides, never another attribute."""
    unit, amount = size
    if (unit == "ml" and title_size is not None and title_size[0] == "fl oz"
            and abs(title_size[1] - amount) <= 0.01 * amount):
        return "fl oz", amount
    return size


def _label_conflict(size, sources):
    """True if `size` is in ml and another source states the same number in fl oz: one of the
    two labels is wrong and nothing says which."""
    unit, amount = size
    return unit == "ml" and any(s is not None and s[0] == "fl oz" and abs(s[1] - amount) <= 0.01 * amount
                                for s in sources)


def amazon_size(title, attrs: dict):
    """Volume-mode size of an Amazon item: `("ml" | "fl oz", pack total)` or None (dropped as
    'no_size'). Conservative (Mafer, 2026-11-23): when the sources leave any doubt, None.

    No size at all when the title is ambiguous (`apify_common.title_is_ambiguous`: a count, an
    unread pack notation, a kit of different products, a dose or capacity, two different sizes),
    when the Item Form is a solid (powder, tablets, paste, bar...; Wayakit sells liquids only), or
    when an attribute states a multipack (Size "(Pack of N)", Unit Count "N Count") that the
    title does not state (or states with another number). Number of Items is weaker (the
    2026-11-23 smoke run had a single 4 L bottle with Number of Items 2, checked by Mafer on the
    page): it only blocks when the title's own size does not agree with an attribute.

    Sources: title, Item Volume, Size, Unit Count. An attribute in ml is read as fl oz when the
    title states that number in fl oz (`_ml_label_fix`).

    1. 2-of-N rule (the old Selenium rule, with Size and Unit Count added): two sources agreeing
       on the pack total give the size. A plain "oz" needs a confirmation that is not another
       plain "oz" (or a relabeled ml): two plain-oz sources never make a pair, because the oz may
       be a net weight. When the agreed value comes from the attributes and the title states a
       pack (> 1), it is only used if the attributes times the title's pack equal the title's own
       total (then that total is returned). Two attributes that agree with each other but not
       with a size the title states give no size. A ml result is dropped when another source
       states the same number in fl oz and the title does not confirm the ml value.
    2. Otherwise the title's size, only when it is in ml, liters or fl oz (never a plain oz or a
       gallon alone) and no attribute volume contradicts it; when the title states a pack, an
       attribute must confirm it (the total, or one unit of the pack).
    An aerosol (Item Form or title), or a can sized in oz (title or Included Components,
    "1 17 ounce can"), never gets a size in fl oz: its oz are a net weight.
    """
    title = str(title or "")
    item_form = str(attrs.get("item form") or "")
    if _is_solid_form(item_form) or title_is_ambiguous(title):
        return None
    sources = [title] + [None if value is None else str(value)
                         for value in (attrs.get("item volume") or attrs.get("volume"),
                                       attrs.get("size"), attrs.get("unit count"))]
    title_size = title_volume(title)
    parsed = [parse_volume(source) for source in sources[1:]]
    sizes = [title_size] + [_ml_label_fix(p, title_size) if p is not None else None for p in parsed]
    # A source is "plain" when its fl oz comes from a plain oz or from a relabeled ml.
    plain = [volume_kind(sources[0]) == PLAIN_OZ] + [
        volume_kind(source) == PLAIN_OZ or (p is not None and fixed != p)
        for source, p, fixed in zip(sources[1:], parsed, sizes[1:])]
    pack = volume_pack(title)
    attr_pack = _attribute_pack(attrs)
    if attr_pack > 1 and attr_pack != pack:
        return None  # an attribute states a multipack the title does not (or a different one)
    items = _number_of_items(attrs.get("number of items")) or 1
    unconfirmed_items = items > 1 and items != pack  # only the title agreeing with an attribute overrides it
    result = None
    found = first_agreeing(sizes, allowed=lambda i, j: not (plain[i] and plain[j]))
    if found is not None:
        index, size = found
        if index != 0 and unconfirmed_items:
            return None
        if index != 0 and pack > 1:
            if title_size is None or not sizes_agree((size[0], size[1] * pack), title_size):
                return None  # per-unit attributes of a pack the title does not total
            result = title_size
        elif index != 0 and title_size is not None and not sizes_agree(size, title_size):
            return None  # two attributes agree with each other but not with the title's own size
        elif _label_conflict(size, sizes) and not (title_size is not None and sizes_agree(size, title_size)):
            return None
        else:
            result = size
    else:
        explicit = title_volume(title, explicit_only=True)  # "24oz - 709ml" -> 709 ml
        if explicit is None or unconfirmed_items:
            return None
        confirmed = False
        for size in sizes[1:]:
            if size is None:
                continue
            if sizes_agree(size, explicit) or (pack > 1 and sizes_agree((size[0], size[1] * pack), explicit)):
                confirmed = True  # the total, or one unit of the title's pack
                continue
            return None  # an attribute contradicts the title
        if pack > 1 and not confirmed:
            return None  # a pack needs an attribute to confirm it
        result = explicit
    if result is not None and result[0] == "fl oz":
        if _AEROSOL_RE.search(item_form + " " + title):
            return None
        if _OZ_CAN_RE.search(title + " " + str(attrs.get("included components") or "")):
            return None
    return result


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
