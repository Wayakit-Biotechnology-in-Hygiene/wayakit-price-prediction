"""Unit tests of `scraper/scrapers/amazon_apify.py` (F6): Amazon KSA through junglee/amazon-crawler.

The adapter gets a `FakeClient` (no Apify) and the real `RelevanceAgent` with `FakeBedrock`
(a title is relevant iff it contains "good"; wipes units = the first integer). What it proves:
- C11: the exact actor input (amazon.sa search URL, never countryCode / zipCode), one run per
  `scrape()` with the configured actor, item cap and USD cap;
- each drop rule (not SA, sponsored, no title, no / zero / negative price, non-SAR currency,
  Wayakit's own products: C09) with its reason; the price is `price.value`, never `listPrice`;
- sizes: 2 of {title, Item Volume, Size, Unit Count} must agree on the pack total (C10), weights
  never count, "3X concentrate" is not a pack; no size -> dropped before the AI;
- relevance through the batch API with the post-drop titles; units mode (wipes batch, Number of
  Items); output keys are exactly the 8 contract keys; Company is the brand, never the seller;
- origin from the data only; duplicate ASINs; errors propagate; the client is resolved lazily.
"""
from __future__ import annotations

import random
import sys

import pytest

from tests.conftest import load_module
from tests.unit._fake_apify import FakeClient
from tests.unit._fake_bedrock import FakeBedrock, titles_in

pytestmark = pytest.mark.unit

m = load_module("scraper/main.py")
config = m.config
from scrapers import amazon_apify  # noqa: E402
from services import apify_client as ac  # noqa: E402

ai = sys.modules[m.RelevanceAgent.__module__]
ROW_KEYS = {"Product", "Price_SAR", "Company", "URL", "Unit of measurement", "Total quantity", "Seller", "Origin"}


def agent():
    return ai.RelevanceAgent(client=FakeBedrock(), sleep=lambda s: None, rng=random.Random(0))


def item(title="good Glass cleaner 500ml", asin="B0FAKE0001", **over):
    base = {
        "loadedCountryCode": "SA",
        "title": title,
        "asin": asin,
        "url": f"https://www.amazon.sa/dp/{asin}",
        "brand": "Dettol",
        "price": {"value": 25.5, "currency": "SAR"},
        "listPrice": {"value": 99.0, "currency": "SAR"},
        "seller": {"name": "Fake Shop"},
        "attributes": [{"key": "Item Volume", "value": "500 Milliliters"}],
    }
    base.update(over)
    return base


def scrape(items, keyword="glass cleaner", mode="volume", relevance=None):
    client = FakeClient(items)
    scraper = amazon_apify.AmazonApifyScraper(None, relevance or agent(), client=client)
    return scraper.scrape(keyword, mode), client


def walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_keys(v)


# --- 1. C11: exact actor input ----------------------------------------------------------------------
def test_build_input_is_exact():
    assert amazon_apify.build_input("pet urine & odor") == {
        "categoryOrProductUrls": [{"url": "https://www.amazon.sa/s?k=pet+urine+%26+odor"}],
        "maxItemsPerStartUrl": config.APIFY_MAX_RESULTS,
        "maxSearchPagesPerStartUrl": 2,
        "scrapeProductDetails": True,
        "language": "en",
        "maxOffers": 0,
        "scrapeSellers": False,
        "useCaptchaSolver": False,
    }


@pytest.mark.parametrize("keyword", ["glass cleaner", "pet urine & odor", "منظف زجاج", "a/b?c=d"])
def test_no_country_or_zip_code_anywhere(keyword):
    keys = {k.lower() for k in walk_keys(amazon_apify.build_input(keyword))}
    assert "countrycode" not in keys and "zipcode" not in keys
    [entry] = amazon_apify.build_input(keyword)["categoryOrProductUrls"]
    assert entry["url"].startswith("https://www.amazon.sa/s?k=")


def test_scrape_makes_one_run_with_the_configured_caps():
    rows, client = scrape([item()])
    [call] = client.calls
    assert call["actor"] == config.APIFY_AMAZON_ACTOR == "junglee~amazon-crawler"
    assert call["input"] == amazon_apify.build_input("glass cleaner")
    assert call["max_items"] == config.APIFY_MAX_RESULTS == 50
    assert call["max_usd"] == config.APIFY_AMAZON_MAX_USD
    assert call["timeout_s"] == config.APIFY_RUN_TIMEOUT_S
    assert len(rows) == 1


# --- 2. drop rules ----------------------------------------------------------------------------------
def _without(key):
    it = item()
    it.pop(key)
    return it


@pytest.mark.parametrize("it, reason", [
    (_without("loadedCountryCode"), "not_sa"),
    (item(loadedCountryCode="AE"), "not_sa"),
    (item(isSponsored=True), "sponsored"),
    (item(categoryPageData={"isSponsored": True}), "sponsored"),
    (item(title="   "), "no_title"),
    (_without("title"), "no_title"),
    (_without("price"), "no_price"),
    (item(price={"value": 0, "currency": "SAR"}), "no_price"),
    (item(price={"value": -4.5, "currency": "SAR"}), "no_price"),
    (item(price={"value": None, "currency": "SAR"}), "no_price"),
    (item(price={"value": 25.0, "currency": "USD"}), "currency"),
    (item(price={"value": 25.0}), "currency"),
    (item(brand="WAYAKIT"), "wayakit"),
    (item(seller={"name": "Waya Kit Official"}), "wayakit"),
    (item(title="good Wayakit pet odor eliminator 500ml"), "wayakit"),
    (item(attributes=[{"key": "Item Volume", "value": "500 ml"}, {"key": "Manufacturer", "value": "waya-kit"}]),
     "wayakit"),
])
def test_drop_rules(it, reason):
    candidate, why = amazon_apify.convert_item(it, "volume")
    assert candidate is None and why == reason


def test_kept_item_has_no_reason():
    candidate, why = amazon_apify.convert_item(item(), "volume")
    assert candidate is not None and why == ""


def test_non_sa_and_sponsored_never_reach_the_ai():
    relevance = agent()
    rows, _ = scrape([item(loadedCountryCode="AE", asin="A1"), item(isSponsored=True, asin="A2")],
                     relevance=relevance)
    assert rows == [] and relevance.stats()["evaluated"] == 0


# --- 3. price ---------------------------------------------------------------------------------------
def test_price_is_the_current_value_not_list_price():
    candidate, _ = amazon_apify.convert_item(item(price={"value": 25.5, "currency": "SAR"},
                                                  listPrice={"value": 99.0, "currency": "SAR"}), "volume")
    assert candidate["Price_SAR"] == pytest.approx(25.5)


def test_list_price_alone_is_not_a_price():
    it = _without("price")
    candidate, why = amazon_apify.convert_item(it, "volume")
    assert candidate is None and why == "no_price"


@pytest.mark.parametrize("price, currency", [({"value": "1,234.50", "currency": "ر.س"}, None),
                                             (1234.5, "SAR"), ("1,234.50", "SAR")])
def test_price_shapes_and_arabic_currency(price, currency):
    over = {"price": price}
    if currency is not None:
        over["currency"] = currency
    candidate, why = amazon_apify.convert_item(item(**over), "volume")
    assert why == "" and candidate["Price_SAR"] == pytest.approx(1234.5)


# --- 4. sizes ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("title, attributes, expected", [
    ("good Cleaner 500ml", [{"key": "Item Volume", "value": "500 Milliliters"}], ("ml", 500.0)),
    ("good Cleaner", [{"key": "Item Volume", "value": "5 Liters"}, {"key": "Size", "value": "907 Grams"},
                      {"key": "Unit Count", "value": "169.0 Fluid Ounces"}], ("ml", 5000.0)),
    ("good Cleaner 500ml Pack of 3", [{"key": "Size", "value": "500 ml (Pack of 3)"}], ("ml", 1500.0)),
    ("good Detergent 3X concentrate 500 ml", [{"key": "Item Volume", "value": "500 ml"}], ("ml", 500.0)),
    ("good Cleaner 16.9 fl oz", [{"key": "Item Volume", "value": "16.9 Fluid Ounces"}], ("fl oz", 16.9)),
    ("good Cleaner 1 Litre", {"Item Volume": "1000 Milliliters"}, ("ml", 1000.0)),
])
def test_size_needs_two_agreeing_sources(title, attributes, expected):
    candidate, why = amazon_apify.convert_item(item(title=title, attributes=attributes), "volume")
    assert why == ""
    assert candidate["Unit of measurement"] == expected[0]
    assert candidate["Total quantity"] == pytest.approx(expected[1])


@pytest.mark.parametrize("title, attributes", [
    ("good Cleaner 500ml", [{"key": "Item Volume", "value": "750 Milliliters"}]),   # disagree
    ("good Cleaner 500ml", []),                                                     # one source only
    ("good Cleaner 907 g", [{"key": "Size", "value": "907 Grams"}]),                # weights never count
    ("good Cleaner 12 oz", [{"key": "Size", "value": "12 Ounces"}]),                # plain oz is a weight
    ("good Cleaner 500ml Pack of 3", [{"key": "Item Volume", "value": "500 Milliliters"}]),  # per-item vs pack
])
def test_size_without_agreement_is_dropped_before_the_ai(title, attributes):
    relevance = agent()
    rows, _ = scrape([item(title=title, attributes=attributes)], relevance=relevance)
    assert rows == []
    assert relevance.stats()["evaluated"] == 0, "no_size is dropped before calling the AI"


# Review round 1 (C10): two per-unit attributes never outvote a title that states the pack.
_PER_UNIT = [{"key": "Item Volume", "value": "500 Milliliters"}, {"key": "Size", "value": "500 ml"},
             {"key": "Unit Count", "value": "3.0 Count"}]


def test_per_unit_attributes_take_the_title_pack_total():
    candidate, why = amazon_apify.convert_item(
        item(title="good Dettol Surface Cleaner 500ml, Pack of 3", attributes=_PER_UNIT, price={"value": 45, "currency": "SAR"}),
        "volume")
    assert why == ""
    assert candidate["Total quantity"] == pytest.approx(1500.0), "never the 500 ml of one unit"


@pytest.mark.parametrize("title", [
    "good Dettol Surface Cleaner, Pack of 3",           # pack stated, no size: ambiguous
    "good Dettol Surface Cleaner 750ml, Pack of 3",     # attributes x pack != title total
])
def test_per_unit_attributes_with_an_ambiguous_pack_title_are_dropped(title):
    relevance = agent()
    rows, _ = scrape([item(title=title, attributes=_PER_UNIT)], relevance=relevance)
    assert rows == []
    assert relevance.stats()["evaluated"] == 0


def test_attributes_agreeing_without_a_pack_in_the_title_are_used():
    assert amazon_apify.amazon_size("good Cleaner", {"item volume": "500 ml", "size": "500 Milliliters"}) == ("ml", 500.0)


def test_title_breakdown_and_per_unit_attributes():
    assert amazon_apify.amazon_size("good Dish Liquid 1.5 L (3 x 500 ml)",
                                    {"item volume": "500 ml", "size": "500 ml"}) == ("ml", 1500.0)


@pytest.mark.parametrize("attrs_field, shape", [
    ("attributes", "list-key"), ("attributes", "list-name"), ("attributes", "dict"),
    ("attributesMapped", "dict"), ("productOverview", "list-key"),
])
def test_attribute_shapes_are_tolerated(attrs_field, shape):
    key = "‎Item Volume :"  # invisible mark and a trailing colon, as seen on Amazon
    if shape == "list-key":
        value = [{"key": key, "value": "500 Milliliters"}]
    elif shape == "list-name":
        value = [{"name": key, "value": "500 Milliliters"}]
    else:
        value = {key: "500 Milliliters"}
    it = item(title="good Cleaner 500ml")
    it.pop("attributes")
    it[attrs_field] = value
    assert amazon_apify.amazon_attributes(it).get("item volume") == "500 Milliliters"
    candidate, why = amazon_apify.convert_item(it, "volume")
    assert why == "" and candidate["Total quantity"] == pytest.approx(500.0)


def test_attributes_first_value_wins_and_missing_is_empty():
    it = item(attributes=[{"key": "Size", "value": "1 L"}], productOverview={"Size": "2 L"})
    assert amazon_apify.amazon_attributes(it)["size"] == "1 L"
    bare = item()
    bare.pop("attributes")
    assert amazon_apify.amazon_attributes(bare) == {}
    assert amazon_apify.amazon_attributes(item(attributes=None, attributesMapped="junk")) == {}


# --- 5. relevance -----------------------------------------------------------------------------------
def test_only_relevant_titles_survive_and_the_ai_sees_the_post_drop_titles():
    bedrock = FakeBedrock()
    relevance = ai.RelevanceAgent(client=bedrock, sleep=lambda s: None, rng=random.Random(0))
    items = [item(title="good Cleaner A 500ml", asin="A1"),
             item(title="bad Cleaner B 500ml", asin="A2"),
             item(title="good Cleaner C", asin="A3", attributes=[]),       # no size
             item(title="good Cleaner D 500ml", asin="A4", loadedCountryCode="AE")]
    rows, _ = scrape(items, relevance=relevance)
    assert [r["Product"] for r in rows] == ["good Cleaner A 500ml"]
    sent = [t for call in bedrock.calls for t in titles_in(call)]
    assert sent == ["good Cleaner A 500ml", "bad Cleaner B 500ml"]
    assert relevance.stats()["evaluated"] == 2 and relevance.stats()["relevant"] == 1


# --- 6. units mode ----------------------------------------------------------------------------------
def test_wipes_units_come_from_the_wipes_batch():
    relevance = agent()
    rows, _ = scrape([item(title="good wipes 80 count", attributes=[])], keyword="wet wipes", mode="units",
                     relevance=relevance)
    assert [(r["Unit of measurement"], r["Total quantity"]) for r in rows] == [("units", 80)]
    assert relevance.stats()["wipes_evaluated"] == 1


def test_units_from_number_of_items():
    rows, _ = scrape([item(title="good toilet brush", attributes=[{"key": "Number of Items", "value": "2"}])],
                     keyword="toilet brush", mode="units")
    assert [(r["Unit of measurement"], r["Total quantity"]) for r in rows] == [("units", 2)]


def test_units_from_the_title_first():
    candidate, why = amazon_apify.convert_item(
        item(title="good microfiber cloths 12 pcs", attributes=[{"key": "Number of Items", "value": "3"}]), "units")
    assert why == "" and (candidate["Unit of measurement"], candidate["Total quantity"]) == ("units", 12)


def test_units_without_any_count_are_dropped_before_the_ai():
    relevance = agent()
    rows, _ = scrape([item(title="good toilet brush", attributes=[])], keyword="toilet brush", mode="units",
                     relevance=relevance)
    assert rows == [] and relevance.stats()["evaluated"] == 0


# --- 7. output keys, Company, Seller, URL ------------------------------------------------------------
def test_output_has_exactly_the_contract_keys():
    rows, _ = scrape([item()])
    [row] = rows
    assert set(row) == ROW_KEYS
    assert row["Product"] == "good Glass cleaner 500ml"
    assert row["Price_SAR"] == pytest.approx(25.5)
    assert row["URL"] == "https://www.amazon.sa/dp/B0FAKE0001"
    assert row["Seller"] == "Fake Shop"


@pytest.mark.parametrize("brand, expected", [
    ("Dettol", "Dettol"), ("Visit the Dettol Store", "Dettol"), ("Brand: Dettol", "Dettol"),
    ("  Dettol  ", "Dettol"), (None, "Company not found"), ("", "Company not found"),
])
def test_company_is_the_brand_never_the_seller(brand, expected):
    candidate, _ = amazon_apify.convert_item(item(brand=brand, seller={"name": "Fake Shop"}), "volume")
    assert candidate["Company"] == expected
    assert candidate["Company"] != "Fake Shop"


@pytest.mark.parametrize("seller, expected", [({"name": " Shop A "}, "Shop A"), ("Shop B", "Shop B"),
                                              (None, ""), (123, "")])
def test_seller_shapes(seller, expected):
    candidate, why = amazon_apify.convert_item(item(seller=seller), "volume")
    assert why == "" and candidate["Seller"] == expected


def test_url_falls_back_to_the_asin():
    it = item(asin="B0FAKE0009")
    it.pop("url")
    candidate, _ = amazon_apify.convert_item(it, "volume")
    assert candidate["URL"] == "https://www.amazon.sa/dp/B0FAKE0009"


# --- 8. origin --------------------------------------------------------------------------------------
def attr(name, value):
    """One junglee attribute. Built by a helper because writing the country-of-origin attribute
    as a dict literal trips gitleaks' generic-api-key rule (false positive in CI, 2026-11-23)."""
    return {"key": name, "value": value}


@pytest.mark.parametrize("extra, title, size_attr, expected", [
    (attr("Country of Origin", "Saudi Arabia"), "good Cleaner 500ml", "500 Milliliters", "local"),
    (attr("Country of Origin", "China"), "good Cleaner 500ml", "500 Milliliters", "imported"),
    (None, "good Cleaner 16.9 fl oz", "16.9 Fluid Ounces", "imported"),
    (None, "good Cleaner 500ml", "500 Milliliters", "unknown"),
    (attr("Country/Region of origin", "KSA"), "good Cleaner 16.9 fl oz", "16.9 Fluid Ounces", "local"),
])
def test_origin_from_the_data(extra, title, size_attr, expected):
    attributes = [{"key": "Item Volume", "value": size_attr}] + ([extra] if extra else [])
    rows, _ = scrape([item(title=title, attributes=attributes)])
    assert [r["Origin"] for r in rows] == [expected]


def test_made_in_ksa_in_the_description_is_local():
    rows, _ = scrape([item(description="Proudly made in Saudi Arabia.")])
    assert [r["Origin"] for r in rows] == ["local"]
    rows, _ = scrape([item(features=["Fresh scent", "Made in KSA"])])
    assert [r["Origin"] for r in rows] == ["local"]


# --- 9. duplicates ----------------------------------------------------------------------------------
def test_duplicate_asins_are_dropped():
    rows, _ = scrape([item(asin="A1"), item(asin="A1", title="good Glass cleaner 500ml v2"), item(asin="A2")])
    assert len(rows) == 2


def test_items_beyond_the_cap_are_ignored():
    items = [item(asin=f"A{i:03d}", title=f"good Glass cleaner {i} 500ml") for i in range(60)]
    rows, _ = scrape(items)
    assert len(rows) == config.APIFY_MAX_RESULTS


# --- 10. errors propagate ---------------------------------------------------------------------------
@pytest.mark.parametrize("error", [ac.ApifyFatalError("fake HTTP 401"), ac.ApifyRunFailed("fake FAILED"),
                                   ac.ApifyBudgetExceeded("fake budget")])
def test_apify_errors_propagate(error):
    scraper = amazon_apify.AmazonApifyScraper(None, agent(), client=FakeClient(error))
    with pytest.raises(type(error)):
        scraper.scrape("glass cleaner", "volume")


def test_relevance_fatal_error_propagates():
    class FatalAgent:
        fatal_error = None

        def filter_relevant(self, titles, query):
            self.fatal_error = m.RelevanceAgentFatalError("fake AccessDeniedException")
            raise self.fatal_error

        def extract_wipes_units_batch(self, titles):
            raise AssertionError("not reached")

    relevance = FatalAgent()
    with pytest.raises(m.RelevanceAgentFatalError):
        amazon_apify.AmazonApifyScraper(None, relevance, client=FakeClient([item()])).scrape("x", "volume")
    assert relevance.fatal_error is not None


# --- 11. lazy client --------------------------------------------------------------------------------
def test_client_is_resolved_lazily(monkeypatch):
    def no_client():
        raise ac.ApifyFatalError("Apify token not available (fake)")

    monkeypatch.setattr(ac, "shared_client", no_client)
    scraper = amazon_apify.AmazonApifyScraper(None, agent())  # construction reads no secret
    with pytest.raises(ac.ApifyFatalError):
        scraper.scrape("glass cleaner", "volume")


def test_scrape_uses_the_shared_client_when_none_is_given(monkeypatch):
    client = FakeClient([item()])
    monkeypatch.setattr(ac, "shared_client", lambda: client)
    rows = amazon_apify.AmazonApifyScraper(None, agent()).scrape("glass cleaner", "volume")
    assert len(rows) == 1 and len(client.calls) == 1
