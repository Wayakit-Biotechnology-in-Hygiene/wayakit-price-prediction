"""Unit tests of `scraper/scrapers/noon_apify.py` (F6): Noon KSA through saswave/noon-product-scraper.

`FakeClient` stands in for Apify; the real `RelevanceAgent` with `FakeBedrock` decides relevance
("good" in the title). What it proves:
- C11: one search run per keyword with exactly one /saudi-en/ search URL; the output is cut to
  `APIFY_MAX_RESULTS` locally;
- product URLs from slug + sku; no sku, no price, non-SAR currency and Wayakit's own products
  (brand, store or name: C09) are dropped; SACO as a Noon store is kept; sale_price before price;
- exactly one actor run per keyword (no details run, 2026-11-23); `shipping_banner_details` never
  decides origin and neither does a fl oz size: a "made in KSA" title is local, anything else
  unknown;
- size comes from the title only: ml, liters or fl oz (a plain "oz" or a gallon alone is not
  enough); an ambiguous title (a kit of different products, a count, a dose...) has none; a
  multipack of the same product is kept with its total.
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
    assert len(client.calls) == 1, "one search run, never a details run"
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


# --- 6. one run, origin from the title ---------------------------------------------------------------
def test_relevant_rows_need_no_details_run():
    search = [item("good car wax 500 ml", "A1"), item("bad car wax 500 ml", "B2"),
              item("good car wax", "C3")]  # no size: dropped before the AI
    rows, client = scrape(search)
    assert len(client.calls) == 1
    assert [(r["URL"], r["Origin"]) for r in rows] == [(url_of("A1"), "unknown")]
    assert set(rows[0]) == ROW_KEYS


@pytest.mark.parametrize("banner", [{"global": True}, {"global": False}, None, "", [], "yes"])
def test_the_shipping_banner_never_decides_origin(banner):
    """2026-11-23 smoke run: `global` was the same generic fee message on every product, local
    ones (sold by SACO) included, so it is not an origin signal (Mafer, 2026-11-23)."""
    rows, client = scrape([item("good car wax 500 ml", "A1", shipping_banner_details=banner),
                           item("good car polish 16 fl oz", "A2", shipping_banner_details=banner),
                           item("good car shampoo 1 L Made in KSA", "A3", shipping_banner_details=banner)])
    assert len(client.calls) == 1
    assert [r["Origin"] for r in rows] == ["unknown", "unknown", "local"]


def test_made_in_ksa_in_the_name_is_local():
    rows, _ = scrape([item("good car wax 500 ml Made in KSA", "A1")])
    assert [r["Origin"] for r in rows] == ["local"]


def test_seller_comes_from_the_search_item():
    rows, _ = scrape([item("good car wax 500 ml", "A1", store_name=" Search Store "),
                      item("good car polish 250 ml", "A2", store_name=None)])
    assert [r["Seller"] for r in rows] == ["Search Store", ""]


@pytest.mark.parametrize("error", [ac.ApifyFatalError("fake HTTP 403"), ac.ApifyRunFailed("fake"),
                                   ac.ApifyBudgetExceeded("fake")])
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


def test_units_mode_counts_from_the_title():
    rows, _ = scrape([shipped("good microfiber cloths 12 pcs", "A1")], keyword="microfiber", mode="units")
    assert [(r["Unit of measurement"], r["Total quantity"]) for r in rows] == [("units", 12)]


def test_a_fl_oz_title_is_not_imported():
    """A size in fl oz does not prove a product is imported (Mafer, 2026-11-23)."""
    rows, _ = scrape([shipped("good car wax 16 fl oz", "A1")])
    assert [(r["Unit of measurement"], r["Origin"]) for r in rows] == [("fl oz", "unknown")]


@pytest.mark.parametrize("name", ["good car wax 32oz", "good car wax 32 Ounces", "good car wash soap 1 Gallon",
                                  "good disinfectant spray 19 oz"])
def test_plain_oz_and_gallon_titles_have_no_size(name):
    """Noon has only the title: nothing confirms a plain oz (it may be a net weight) or a gallon,
    so the row is dropped (Mafer, 2026-11-23)."""
    relevance = agent()
    rows, _ = scrape([item(name, "A1")], relevance=relevance)
    assert rows == [] and relevance.stats()["evaluated"] == 0


@pytest.mark.parametrize("name, expected", [
    ("good car wax 24oz - 709ml", ("ml", 709.0)),              # the ml in the same title says it
    ("good car wax 16.9 fl oz", ("fl oz", 16.9)),
    ("good car shampoo 400ml Twinpack", ("ml", 800.0)),        # a multipack of the same product is kept
    ("good car shampoo 1 Liter Pack of Six", ("ml", 6000.0)),
    ("good car shampoo 500ML*2", ("ml", 1000.0)),
])
def test_explicit_sizes_and_same_product_multipacks(name, expected):
    rows, _ = scrape([item(name, "A1")])
    assert [(r["Unit of measurement"], r["Total quantity"], r["Origin"]) for r in rows] == \
        [(expected[0], pytest.approx(expected[1]), "unknown")]


@pytest.mark.parametrize("name", ["good car wax 28 oz, 4 Count", "good car wash pods 16 ct 15.2 oz",
                                  "good car wax 1/2 Gallon", "good car wax 12/19 oz",
                                  # conservative mode (Mafer, 2026-11-23)
                                  "good car shampoo 1 L makes 100 L", "good snow foam 1 oz per gallon, 32 oz",
                                  "good car wash 1 gal Bottle, 6/Carton", "good glass cleaner 32 oz Twin Pack",
                                  "good car wash soap 64 oz with 2 Pack Wash Mitts", "good car wash pods 16 oz",
                                  # a kit of different products is not stored (Mafer, 2026-11-23)
                                  "good car wash kit 500 ml shampoo, wax and microfiber",
                                  "good car shampoo 1L with Pack of 3 Microfiber Towels",
                                  "good leather cleaner 250 ml and conditioner 250 ml",
                                  "good car shampoo 500ml and 1L Refill", "good car wax paste 11 oz"])
def test_ambiguous_count_and_fraction_titles_have_no_size(name):
    relevance = agent()
    rows, _ = scrape([item(name, "A1")], relevance=relevance)
    assert rows == [] and relevance.stats()["evaluated"] == 0


def test_client_is_resolved_lazily(monkeypatch):
    def no_client():
        raise ac.ApifyFatalError("Apify token not available (fake)")

    monkeypatch.setattr(ac, "shared_client", no_client)
    scraper = noon_apify.NoonApifyScraper(None, agent())
    with pytest.raises(ac.ApifyFatalError):
        scraper.scrape("car wax", "volume")


# --- review round 1 ---------------------------------------------------------------------------------
@pytest.mark.parametrize("url, expected", [
    ("/saudi-en/dry-shampoo/ZX/p/", "https://www.noon.com/saudi-en/dry-shampoo/ZX/p/"),
    ("saudi-en/dry-shampoo/ZX/p/", "https://www.noon.com/saudi-en/dry-shampoo/ZX/p/"),
    ("dry-shampoo", "https://www.noon.com/saudi-en/dry-shampoo/ZX/p/"),
    ("", "https://www.noon.com/saudi-en/ZX/p/"),
])
def test_product_url_shapes(url, expected):
    assert noon_apify.product_url({"sku": "ZX", "url": url}) == expected
