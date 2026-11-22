"""Unit tests of `scraper/scrapers/noon_apify.py` (F6): Noon KSA through saswave/noon-product-scraper.

`FakeClient` stands in for Apify; the real `RelevanceAgent` with `FakeBedrock` decides relevance
("good" in the title). What it proves:
- C11: one search run per keyword with exactly one /saudi-en/ search URL; the output is cut to
  `APIFY_MAX_RESULTS` locally;
- product URLs from slug + sku; no sku, no price, non-SAR currency and Wayakit's own products
  (brand, store or name: C09) are dropped; SACO as a Noon store is kept; sale_price before price;
- details run only for relevant rows that have no shipping info, in one run per keyword, and give
  the `global` (imported) flag; a details failure fails the keyword loudly;
- size comes from the title only.
"""
from __future__ import annotations

import random
import sys

import pytest

from tests.conftest import load_module
from tests.unit._fake_apify import FakeClient
from tests.unit._fake_bedrock import FakeBedrock

pytestmark = pytest.mark.unit

m = load_module("scraper/main.py")
config = m.config
from scrapers import apify_common as common, noon_apify  # noqa: E402
from services import apify_client as ac  # noqa: E402

ai = sys.modules[m.RelevanceAgent.__module__]
ROW_KEYS = {"Product", "Price_SAR", "Company", "URL", "Unit of measurement", "Total quantity", "Seller", "Origin"}


def agent():
    return ai.RelevanceAgent(client=FakeBedrock(), sleep=lambda s: None, rng=random.Random(0))


def item(name="good car wax 500 ml", sku="N0001", **over):
    base = {"name": name, "sku": sku, "url": "car-wax", "price": 30.0, "sale_price": 25.0,
            "brand": "Meguiars", "store_name": "Fake Store"}
    base.update(over)
    return base


def shipped(name="good car wax 500 ml", sku="N0001", global_flag=False, **over):
    return item(name, sku, shipping_banner_details={"global": global_flag}, **over)


def url_of(sku, slug="car-wax"):
    return f"https://www.noon.com/saudi-en/{slug}/{sku}/p/"


def scrape(*results, keyword="car wax", mode="volume", relevance=None):
    client = FakeClient(*results)
    rows = noon_apify.NoonApifyScraper(None, relevance or agent(), client=client).scrape(keyword, mode)
    return rows, client


# --- 1. C11 -----------------------------------------------------------------------------------------
def test_search_input_is_exact():
    assert noon_apify.build_search_input("car wax & polish") == {
        "search_urls": ["https://www.noon.com/saudi-en/search/?q=car%20wax%20%26%20polish"],
        "max_pages": 1,
        "product_urls": [],
    }


def test_details_input_is_exact():
    urls = [url_of("A1"), url_of("B2")]
    assert noon_apify.build_details_input(urls) == {"search_urls": [], "max_pages": 1, "product_urls": urls}


def test_one_search_run_per_keyword():
    client = FakeClient([], [])
    scraper = noon_apify.NoonApifyScraper(None, agent(), client=client)
    assert scraper.scrape("car wax", "volume") == []
    assert scraper.scrape("car shampoo", "volume") == []
    assert len(client.calls) == 2
    for call, keyword in zip(client.calls, ["car%20wax", "car%20shampoo"]):
        assert call["actor"] == config.APIFY_NOON_ACTOR == "saswave~noon-product-scraper"
        assert call["input"]["search_urls"] == [f"https://www.noon.com/saudi-en/search/?q={keyword}"]
        assert call["input"]["product_urls"] == []
        assert call["max_items"] == config.APIFY_MAX_RESULTS
        assert call["max_usd"] == config.APIFY_NOON_MAX_USD
        assert call["timeout_s"] == config.APIFY_RUN_TIMEOUT_S


def test_output_is_cut_to_the_cap_locally():
    items = [shipped(f"good car wax {i} 500 ml", f"S{i:03d}") for i in range(60)]
    rows, client = scrape(items)
    assert len(client.calls) == 1, "every row had shipping info: no details run"
    assert len(rows) == config.APIFY_MAX_RESULTS == 50
    assert {r["URL"] for r in rows} == {url_of(f"S{i:03d}") for i in range(50)}


# --- 2. product URL ---------------------------------------------------------------------------------
def test_product_url():
    assert noon_apify.product_url({"sku": "ZX", "url": "dry-shampoo"}) == \
        "https://www.noon.com/saudi-en/dry-shampoo/ZX/p/"
    full = "https://www.noon.com/saudi-en/dry-shampoo/ZX/p/?o=1"
    assert noon_apify.product_url({"sku": "ZX", "url": full}) == full
    assert noon_apify.product_url({"url": "dry-shampoo"}) is None


def test_missing_sku_is_dropped():
    it = item()
    it.pop("sku")
    assert noon_apify.convert_item(it, "volume") == (None, "no_sku")


# --- 3. price ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("over, expected", [
    ({"sale_price": 25.0, "price": 30.0}, 25.0),
    ({"sale_price": 0, "price": 30.0}, 30.0),
    ({"sale_price": None, "price": 30.0}, 30.0),
    ({"sale_price": "1,234.50", "price": 2000}, 1234.5),
])
def test_sale_price_is_preferred(over, expected):
    candidate, why = noon_apify.convert_item(item(**over), "volume")
    assert why == "" and candidate["Price_SAR"] == pytest.approx(expected)


@pytest.mark.parametrize("over", [{"sale_price": None, "price": None}, {"sale_price": 0, "price": 0},
                                  {"sale_price": -1, "price": "abc"}])
def test_no_price_is_dropped(over):
    assert noon_apify.convert_item(item(**over), "volume") == (None, "no_price")


def test_both_prices_missing_is_dropped():
    it = item()
    it.pop("sale_price")
    it.pop("price")
    assert noon_apify.convert_item(it, "volume") == (None, "no_price")


def test_no_name_is_dropped():
    assert noon_apify.convert_item(item(name="  "), "volume") == (None, "no_title")


# --- 4. currency ------------------------------------------------------------------------------------
def test_currency_absent_means_sar_and_aed_is_dropped():
    assert noon_apify.convert_item(item(), "volume")[1] == ""
    assert noon_apify.convert_item(item(currency="SAR"), "volume")[1] == ""
    assert noon_apify.convert_item(item(currency="AED"), "volume") == (None, "currency")


# --- 5. Wayakit and SACO ----------------------------------------------------------------------------
@pytest.mark.parametrize("over", [{"brand": "Wayakit"}, {"store_name": "WAYA KIT store"},
                                  {"name": "good waya-kit car wax 500 ml"}])
def test_wayakit_is_dropped(over):
    assert noon_apify.convert_item(item(**over), "volume") == (None, "wayakit")


def test_saco_as_a_noon_store_is_kept():
    candidate, why = noon_apify.convert_item(item(store_name="SACO"), "volume")
    assert why == "" and candidate["Seller"] == "SACO"


def test_company_and_seller():
    candidate, _ = noon_apify.convert_item(item(brand=" Meguiars ", store_name=" Fake Store "), "volume")
    assert (candidate["Company"], candidate["Seller"]) == ("Meguiars", "Fake Store")
    candidate, _ = noon_apify.convert_item(item(brand=None, store_name=None), "volume")
    assert (candidate["Company"], candidate["Seller"]) == ("Company not found", "")


# --- 6. details -------------------------------------------------------------------------------------
def test_details_run_only_for_relevant_rows():
    search = [item("good car wax 500 ml", "A1"), item("bad car wax 500 ml", "B2"),
              item("good car wax", "C3")]  # no size: dropped before the AI
    details = [{"sku": "A1", "shipping_banner_details": {"global": True}, "specifications": []}]
    rows, client = scrape(search, details)
    assert len(client.calls) == 2
    det = client.calls[1]
    assert det["actor"] == config.APIFY_NOON_ACTOR
    assert det["input"] == {"search_urls": [], "max_pages": 1, "product_urls": [url_of("A1")]}
    assert det["max_items"] == 1 and det["max_usd"] == config.APIFY_NOON_MAX_USD
    assert [(r["URL"], r["Origin"]) for r in rows] == [(url_of("A1"), "imported")]
    assert set(rows[0]) == ROW_KEYS


def test_no_details_run_when_nothing_is_relevant():
    rows, client = scrape([item("bad car wax 500 ml", "B2")])
    assert rows == [] and len(client.calls) == 1


def test_no_details_run_when_every_relevant_row_has_shipping_info():
    rows, client = scrape([shipped("good car wax 500 ml", "A1", global_flag=True),
                           shipped("good car polish 250 ml", "A2", global_flag=False),
                           item("bad car wax 500 ml", "B2")])
    assert len(client.calls) == 1
    assert [r["Origin"] for r in rows] == ["imported", "unknown"]


def test_details_only_for_the_rows_without_shipping_info():
    search = [shipped("good car wax 500 ml", "A1", global_flag=True), item("good car polish 250 ml", "A2")]
    rows, client = scrape(search, [{"sku": "A2", "shipping_banner_details": {"global": False}}])
    assert client.calls[1]["input"]["product_urls"] == [url_of("A2")]
    assert client.calls[1]["max_items"] == 1
    assert [r["Origin"] for r in rows] == ["imported", "unknown"]


def test_made_in_ksa_in_the_name_beats_global():
    rows, _ = scrape([item("good car wax 500 ml Made in KSA", "A1")],
                     [{"sku": "A1", "shipping_banner_details": {"global": True}}])
    assert [r["Origin"] for r in rows] == ["local"]


def test_made_in_ksa_in_the_specifications_is_local():
    rows, _ = scrape([item("good car wax 500 ml", "A1")],
                     [{"sku": "A1", "shipping_banner_details": {"global": False},
                       "specifications": [{"name": "Origin", "value": "Made in Saudi Arabia"}]}])
    assert [r["Origin"] for r in rows] == ["local"]


def test_details_are_joined_by_the_sku_in_the_url():
    rows, _ = scrape([item("good car wax 500 ml", "A1")],
                     [{"url": url_of("A1"), "shipping_banner_details": {"global": True}}])
    assert [r["Origin"] for r in rows] == ["imported"]


def test_details_fill_an_empty_seller_and_unmatched_rows_keep_search_values():
    search = [item("good car wax 500 ml", "A1", store_name=""), item("good car polish 250 ml", "A2")]
    details = [{"sku": "A1", "store_name": "Details Store", "shipping_banner_details": {"global": False}}]
    rows, _ = scrape(search, details)
    by_url = {r["URL"]: r for r in rows}
    assert by_url[url_of("A1")]["Seller"] == "Details Store"
    assert by_url[url_of("A2")]["Seller"] == "Fake Store" and by_url[url_of("A2")]["Origin"] == "unknown"


def test_details_never_overwrite_a_search_seller():
    rows, _ = scrape([item("good car wax 500 ml", "A1", store_name="Search Store")],
                     [{"sku": "A1", "store_name": "Details Store"}])
    assert [r["Seller"] for r in rows] == ["Search Store"]


@pytest.mark.parametrize("error", [ac.ApifyRunFailed("fake details FAILED"), ac.ApifyBudgetExceeded("fake")])
def test_details_failure_propagates(error):
    with pytest.raises(type(error)):
        scrape([item("good car wax 500 ml", "A1")], error)


@pytest.mark.parametrize("error", [ac.ApifyFatalError("fake HTTP 403"), ac.ApifyRunFailed("fake")])
def test_search_failure_propagates(error):
    with pytest.raises(type(error)):
        scrape(error)


def test_duplicate_skus_are_dropped():
    rows, _ = scrape([shipped("good car wax 500 ml", "A1"), shipped("good car wax 500 ml v2", "A1"),
                      shipped("good car polish 250 ml", "A2")])
    assert len(rows) == 2


# --- 7. size from the title only --------------------------------------------------------------------
def test_size_comes_from_the_title_only():
    rows, _ = scrape([shipped("good car wax 3 x 500 ml", "A1")])
    assert [(r["Unit of measurement"], r["Total quantity"]) for r in rows] == [("ml", pytest.approx(1500.0))]


def test_title_without_a_size_is_dropped_in_volume_mode():
    relevance = agent()
    rows, client = scrape([item("good car wax", "A1", specifications=[{"name": "Size", "value": "500 ml"}])],
                          relevance=relevance)
    assert rows == [] and len(client.calls) == 1 and relevance.stats()["evaluated"] == 0


def test_details_do_not_change_the_size():
    rows, _ = scrape([item("good car wax 500 ml", "A1")],
                     [{"sku": "A1", "name": "car wax 1 L", "specifications": [{"name": "Volume", "value": "1 L"}]}])
    assert [r["Total quantity"] for r in rows] == [pytest.approx(500.0)]


def test_units_mode_counts_from_the_title():
    rows, _ = scrape([shipped("good microfiber cloths 12 pcs", "A1")], keyword="microfiber", mode="units")
    assert [(r["Unit of measurement"], r["Total quantity"]) for r in rows] == [("units", 12)]


def test_fl_oz_title_is_imported():
    rows, _ = scrape([shipped("good car wax 16 fl oz", "A1")])
    assert [(r["Unit of measurement"], r["Origin"]) for r in rows] == [("fl oz", "imported")]


def test_client_is_resolved_lazily(monkeypatch):
    def no_client():
        raise ac.ApifyFatalError("Apify token not available (fake)")

    monkeypatch.setattr(ac, "shared_client", no_client)
    scraper = noon_apify.NoonApifyScraper(None, agent())
    with pytest.raises(ac.ApifyFatalError):
        scraper.scrape("car wax", "volume")


# --- review round 1 ---------------------------------------------------------------------------------
@pytest.mark.parametrize("detail", [
    {"sku": "Z1", "store_name": "WAYAKIT", "shipping_banner_details": {"global": False}},
    {"sku": "Z1", "store_name": "Some Store", "brand": "Waya Kit"},
    {"url": url_of("Z1"), "store_name": "wayakit official"},
])
def test_a_wayakit_seller_or_brand_seen_only_in_details_is_dropped(detail):
    """C09: the search item had no store_name, so the exclusion must run again on the details."""
    search = [item("good floor cleaner 1 L", "Z1", store_name=None, brand=None),
              item("good car polish 250 ml", "A2")]
    rows, client = scrape(search, [detail])
    assert len(client.calls) == 2
    assert [r["URL"] for r in rows] == [url_of("A2")]
    assert not any(common.WAYAKIT_RE.search(r["Seller"] + " " + r["Company"]) for r in rows)


@pytest.mark.parametrize("value", [None, "", [], "yes"])
def test_a_non_object_shipping_banner_still_asks_for_details(value):
    """A uniform schema with `shipping_banner_details: null` carries no flag."""
    rows, client = scrape([item("good car wax 500 ml", "A1", shipping_banner_details=value)],
                          [{"sku": "A1", "shipping_banner_details": {"global": True}}])
    assert len(client.calls) == 2
    assert [r["Origin"] for r in rows] == ["imported"]


@pytest.mark.parametrize("url, expected", [
    ("/saudi-en/dry-shampoo/ZX/p/", "https://www.noon.com/saudi-en/dry-shampoo/ZX/p/"),
    ("saudi-en/dry-shampoo/ZX/p/", "https://www.noon.com/saudi-en/dry-shampoo/ZX/p/"),
    ("dry-shampoo", "https://www.noon.com/saudi-en/dry-shampoo/ZX/p/"),
    ("", "https://www.noon.com/saudi-en/ZX/p/"),
])
def test_product_url_shapes(url, expected):
    assert noon_apify.product_url({"sku": "ZX", "url": url}) == expected
