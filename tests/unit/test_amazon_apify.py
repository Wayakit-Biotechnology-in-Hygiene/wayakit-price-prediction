"""Unit tests of `scraper/scrapers/amazon_apify.py` (F6): Amazon KSA through junglee/amazon-crawler.

The adapter gets a `FakeClient` (no Apify) and the real `RelevanceAgent` with `FakeBedrock`
(a title is relevant iff it contains "good"; wipes units = the first integer). What it proves:
- C11: the exact actor input (amazon.sa search URL, never countryCode / zipCode), one run per
  `scrape()` with the configured actor, item cap and USD cap;
- each drop rule (not SA, sponsored, no title, no / zero / negative price, non-SAR currency,
  Wayakit's own products: C09) with its reason; the price is `price.value`, never `listPrice`;
- sizes: 2 of {title, Item Volume, Size, Unit Count} agreeing on the pack total (C10), else the
  title's size when no attribute contradicts it; a ml attribute repeating another source's fl oz
  number is fl oz; plain oz and gallons are fl oz; weights never count, "3X concentrate" is not a
  pack; no size -> dropped before the AI (cases from the 2026-11-23 smoke run);
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
    ("good Cleaner", [{"key": "Item Volume", "value": "500 Milliliters"}]),         # one attribute only
    ("good Cleaner 907 g", [{"key": "Size", "value": "907 Grams"}]),                # weights never count
    ("good Cleaner Net Wt 12 oz", [{"key": "Size", "value": "12 Ounces"}]),         # net weight in the title
    ("good Cleaner 750ml, Pack of 3", [{"key": "Item Volume", "value": "500 Milliliters"}]),  # neither total nor unit
])
def test_size_without_agreement_is_dropped_before_the_ai(title, attributes):
    relevance = agent()
    rows, _ = scrape([item(title=title, attributes=attributes)], relevance=relevance)
    assert rows == []
    assert relevance.stats()["evaluated"] == 0, "no_size is dropped before calling the AI"


# --- title-only sizes and the fl-oz-as-ml label (Mafer, 2026-11-23; real smoke-run items) ----------
@pytest.mark.parametrize("title, attrs, expected", [
    # No attribute is a volume: the title alone is enough.
    ("Clorox Pet Urine Remover for Stains and Odors, Spray Bottle, 24oz -709ml",
     {"size": "1 count (Pack of 1)", "unit count": "1.0 Count"}, ("ml", 709.0)),    # the title's own ml
    ("Pet Stain Remover and Odor Eliminator Spray, 100ml Plant-Based Surfactant Cleaner", {}, ("ml", 100.0)),
    ("good Cleaner 500ml", {}, ("ml", 500.0)),
    ("good Cleaner 500ml", {"unit count": "907.18 Grams"}, ("ml", 500.0)),     # a weight never contradicts
    # A per-unit attribute of the title's pack is consistent with it.
    ("good Cleaner 500ml Pack of 3", {"item volume": "500 Milliliters"}, ("ml", 1500.0)),
    # Plain oz in the title agrees with an attribute.
    ("Simple Solution Extreme Stain & Odor Remover, Stain Cleaner, 32oz (2 Pack)",
     {"item volume": "64 Fluid Ounces", "size": "32 Fl Oz (Pack of 2)", "unit count": "64 Milliliters"},
     ("fl oz", 64.0)),
    ("good Cleaner 12 oz", {"item volume": "12 Fluid Ounces"}, ("fl oz", 12.0)),
    ("good Cleaner 12 oz", {"size": "355 ml"}, ("fl oz", 12.0)),
    # A gallon is 128 fl oz and settles 3840 / 3750 ml.
    ("Simple Solution Pet Stain and Odor Remover | Enzymatic Cleaner | Floral Fresh, 1 Gallon",
     {"item volume": "1 Gallons", "size": "3840 ml (Pack of 1)", "unit count": "3750 Milliliters"}, ("fl oz", 128.0)),
    # Unit Count holds the fl oz number with a ml label; the explicit Item Volume confirms the title.
    ("Rejuvenate Carpet Spot Remover 32oz", {"item volume": "32 Fluid Ounces", "unit count": "32 Milliliters"},
     ("fl oz", 32.0)),
])
def test_smoke_run_sizes(title, attrs, expected):
    unit, amount = amazon_apify.amazon_size(title, attrs)
    assert unit == expected[0]
    assert amount == pytest.approx(expected[1])


@pytest.mark.parametrize("title, attrs", [
    # An attribute volume contradicts the title.
    ("Simple Solution Urine Destroyer Enzymatic Cleaner | 32 Ounces",
     {"item volume": "5 Liters", "size": "890 ml (Pack of 1)", "unit count": "907.18 Grams"}),
    ("good Cleaner 500ml", {"item volume": "750 Milliliters"}),
    # No size in the title and no two attributes agree: one attribute alone is never enough.
    ("Extreme Stain & Odor Eliminator Professional Strength Deodorizing Spray",
     {"item volume": "499.73 Milliliters", "unit count": "1 Count"}),
    ("Paw Inspired Pet Enzyme Cleaner Spray", {"unit count": "946.35 Milliliters"}),
    ("Simple Solution Cat Litter Freshner Granules, 600 gm", {"unit count": "1 Count"}),
    # Conservative mode (Mafer, 2026-11-23). 128 ml twice against 1 gallon (128 fl oz): one label is
    # wrong and no title says which.
    ("Stuart Pet Supply Co. Professional Strength Pet Odor Eliminator",
     {"item volume": "1 Gallons", "size": "128 ml (Pack of 1)", "unit count": "128 Milliliters"}),
    # A plain oz needs a confirmation that is not another plain oz or a relabeled ml: the oz may
    # be a net weight (aerosols, pastes). Title-only needs ml, liters or fl oz.
    ("good Cleaner 12 oz", {"size": "12 Ounces"}),
    ("Nature’s Miracle Urine Destroyer Plus Cat, 32 Ounces, Enzymatic Formula", {}),
    ("THORNELL Cat Odor-Off Concentrate Pet Smell Odor Eliminator – 16oz", {"unit count": "16 Milliliters"}),
    ("Floral Fresh, 1 Gallon", {}),
    # "32Oz, Blue, 32 Oz Spray" has the shape of a two-scent pack ("1L Blue, 1L Pink"): no title size,
    # and the two attributes (890 ml, 945 ml) do not agree.
    ("Simple Solution Stain And Odor Remover 2X Pro Bacteria And Enzyme Formula, Floral Fresh 32Oz, Blue, 32 Oz Spray",
     {"size": "890 ml (Pack of 1)", "unit count": "945 Milliliters"}),
    # Number of Items 2 with nothing in the title to agree with: one attribute pair or the title
    # alone is not enough.
    ("Goreem Powerful Pet Odor Eliminator Spray", {"item volume": "120 Milliliters", "unit count": "120 Milliliters",
                                                   "number of items": "2"}),
    ("good Cleaner 500ml", {"number of items": "2"}),
])
def test_smoke_run_items_without_a_usable_size(title, attrs):
    assert amazon_apify.amazon_size(title, attrs) is None


# Review of the 2026-11-23 fixes: a per-unit oz title with "N Count", a capacity, a fl oz pack.
@pytest.mark.parametrize("title, attrs, expected", [
    ("Lysol Spray 19 fl oz x 3", {"item volume": "19 Fluid Ounces", "size": "19 Fl Oz"}, ("fl oz", 57.0)),
    ("Dettol Antiseptic Liquid/500ml", {"item volume": "500 Milliliters"}, ("ml", 500.0)),
])
def test_review_sizes(title, attrs, expected):
    unit, amount = amazon_apify.amazon_size(title, attrs)
    assert unit == expected[0]
    assert amount == pytest.approx(expected[1])


@pytest.mark.parametrize("title, attrs", [
    # A title with a count of 2 or more is ambiguous: only the attributes decide, never the title alone.
    ("Method All-Purpose Cleaner Spray, French Lavender, 28 oz, 4 Count", {"unit count": "4.0 Count"}),
    ("Dish Soap 500ml, 24 Count Sponges", {"item volume": "500 Milliliters"}),
    ("Finish 24 Count 500 ml rinse aid", {"size": "500 ml"}),
    ("Tide PODS Laundry Detergent, 16 Count, 15.2 oz", {}),
    # Fractions and case notation are not read.
    ("Clorox Splash-Less Bleach, 1/2 Gallon", {"unit count": "1.0 Count"}),
    ("Lysol Disinfectant Spray, 12/19 oz", {}),
    # Conservative mode (Mafer, 2026-11-23): an ambiguous title leaves a single attribute, and an
    # attribute multipack the title does not state is ambiguous.
    ("Method All-Purpose Cleaner Spray, French Lavender, 28 oz, 4 Count",
     {"item volume": "28 Fluid Ounces", "size": "28 Fl Oz (Pack of 4)", "unit count": "112 Fl Oz"}),
    ("Tetra AquaSafe Water Conditioner, Treats up to 50 Gallons, 250 ml", {"item volume": "250 Milliliters"}),
])
def test_review_items_without_a_usable_size(title, attrs):
    assert amazon_apify.amazon_size(title, attrs) is None


def test_a_title_with_a_tank_capacity_is_not_used():
    assert amazon_apify.amazon_size("Aquarium Water Conditioner for 55 Gallon Tanks, 8 oz",
                                    {"item volume": "8 Fluid Ounces"}) is None
    # Not even when two attributes agree: they may be per-unit sizes of a pack or a kit.
    assert amazon_apify.amazon_size("Aquarium Water Conditioner for 55 Gallon Tanks, 8 oz",
                                    {"item volume": "8 Fluid Ounces", "size": "8 Fl Oz"}) is None


# Review of the conservative rules (2026-11-23) and Mafer's answers: solids and kits of different
# products are not wanted; a multipack of the same product is kept with its total.
@pytest.mark.parametrize("title, attrs", [
    # an ambiguous title drops the row even when two per-unit attributes agree
    ("Clorox Bleach, 121 oz, 3 Count", {"item volume": "121 Fluid Ounces", "size": "121 Fl Oz"}),
    ("Simple Green All-Purpose Cleaner 1 Gallon, Case of 6", {"item volume": "1 Gallons", "size": "1 Gallon"}),
    ("Leather Cleaner 16 oz and Leather Conditioner 16 oz Kit", {"item volume": "16 Fluid Ounces", "size": "16 Fl Oz"}),
    ("Car Wash Shampoo 1L with Pack of 3 Microfiber Towels", {"size": "1 Liter"}),
    # a title that states a pack but no size: the attributes are per unit
    ("Lysol Disinfectant Spray, Crisp Linen, Pack of 2", {"item volume": "19 Fluid Ounces", "size": "19 Ounce"}),
    ("Dettol Antiseptic Liquid, Pack of 3", {"item volume": "500 Milliliters", "size": "500 ml"}),
    # the title's pack and an attribute's pack disagree
    ("Hand Soap, Pack of 3", {"item volume": "36 Fluid Ounces", "size": "36 Fl Oz", "number of items": "6"}),
    ("Clorox Cleaner 32 oz", {"item volume": "32 Fluid Ounces", "size": "32 oz (Pack of 2)", "unit count": "64 Fl Oz"}),
    # solids and aerosols: their oz are a net weight
    ("Meguiar's Clay Bar 3.5 oz", {"size": "3.5 Ounce"}),
    ("Turtle Wax Paste Wax 14 oz", {"item volume": "14 Fluid Ounces"}),
    ("Meguiar's Gold Class Wax 11 oz", {"item form": "Wax", "size": "11 Ounce", "item volume": "11 Fluid Ounces"}),
    ("Lysol Disinfectant Spray, Crisp Linen, 19 oz", {"item form": "Aerosol", "size": "19 Ounce (Pack of 1)"}),
    ("Lysol Disinfectant Spray, Crisp Linen, 19 oz", {"item form": "Aerosol", "item volume": "19 Fluid Ounces"}),
    ("Febreze Air Freshener Aerosol 8.8 oz", {"item volume": "8.8 Fluid Ounces"}),
    # a title pack with no attribute to confirm it
    ("Dettol Antiseptic Liquid 500ml*2", {}),
    ("Clorox Bleach 1.89L Twin Pack", {}),
    ("Fairy Dishwashing Liquid Lemon 500ml x 2, Original Scent", {}),
    ("Car Wash Bucket 5 Gallon", {"item volume": "5 Gallons", "size": "5 Gallon"}),
    # final review (2026-11-23)
    ("Car Wash Soap 64 oz with 3 Pack of Premium Microfiber Towels",
     {"item volume": "64 Fluid Ounces", "unit count": "64.0 Fluid Ounces"}),
    ("Armor All Interior Cleaner Spray 16 fl oz with 2 Pack Cleaning Wipes", {"item volume": "16 Fluid Ounces"}),
    ("Multi-Surface Cleaner Concentrate 946 ml, Makes up to 8 Bottles", {"item volume": "946 Milliliters"}),
    ("Purex Liquid Laundry Detergent 2X 150 oz", {"item volume": "150 Fluid Ounces", "size": "150 Fl Oz"}),
    # two attributes agree with each other but not with the title's own size
    ("Dettol Antiseptic Liquid 1L", {"item volume": "1 Milliliters", "unit count": "1.0 Milliliters"}),
    ("Eco Strong Pet Stain and Odor Remover, for Cats Dogs I by 1: Gallon Value .9 Gallon, Size 9",
     {"item volume": "128 Fluid Ounces", "size": "3.79 l (Pack of 1)", "unit count": "3785.41 Milliliters"}),
])
def test_conservative_items_without_a_size(title, attrs):
    assert amazon_apify.amazon_size(title, attrs) is None


def test_a_can_sized_in_oz_gets_no_size():
    """Both 2026-11-23 smoke runs: a 17 oz can (Included Components "1 17 ounce can", case price
    SAR 723) was stored as 17 fl oz. Its oz are a net weight, like an aerosol's."""
    title = ("Simple Solution Urine Destroyer Enzymatic Cleaner | Pet Stain and Odor Remover with 2X "
             "Pro-bacteria Cleaning Power | 17 Ounces")
    attrs = {"item volume": "5 Liters", "size": "500 ml (Pack of 1)", "unit count": "17 Milliliters",
             "number of items": "1", "item form": "other", "item weight": "17 Ounces",
             "included components": "1 17 ounce can"}
    assert amazon_apify.amazon_size(title, attrs) is None
    assert amazon_apify.amazon_size("Carpet Foam Cleaner 22 oz Can", {"item volume": "22 Fluid Ounces"}) is None
    # Without the can, the same sources still give the size; "can" as a verb is not a can.
    assert amazon_apify.amazon_size(title, {**attrs, "included components": "1 bottle"}) == ("fl oz", 17.0)
    assert amazon_apify.amazon_size(
        "Odor Remover 32 Fl Oz, Can Be Used on Carpets",
        {"item volume": "32 Fluid Ounces", "included components": "Can be used on carpets"}) == ("fl oz", 32.0)
    # A can in ml is a volume.
    assert amazon_apify.amazon_size(
        "Carpet Foam Cleaner 650 ml", {"item volume": "650 Milliliters", "included components": "1 650 ml can"}
    ) == ("ml", 650.0)


def test_number_of_items_does_not_outvote_a_title_that_agrees_with_an_attribute():
    """2026-11-23 smoke run: a single 4 L bottle (checked by Mafer on the page) had Number of Items 2."""
    attrs = {"item volume": "148.13 Fluid Ounces", "size": "4 L", "unit count": "4000 Milliliters",
             "number of items": "2"}
    assert amazon_apify.amazon_size(
        "Simple Solution Patio and Decking Pet Stain and Odour Remover, Silver, 4 Litres", attrs) == ("ml", 4000.0)
    # A real 2-pack states it in the title, and Number of Items agrees.
    assert amazon_apify.amazon_size(
        "Simple Solution Extreme Stain & Odor Remover, Stain Cleaner, 32oz (2 Pack)",
        {"item volume": "64 Fluid Ounces", "size": "32 Fl Oz (Pack of 2)", "number of items": "2"}) == ("fl oz", 64.0)


@pytest.mark.parametrize("title, attrs, expected", [
    ("Dettol Antiseptic Liquid 500ml*2", {"size": "500 ml"}, ("ml", 1000.0)),   # a pack needs an attribute
    ("Dettol Antiseptic Liquid 500 ml * 3", {"item volume": "500 Milliliters"}, ("ml", 1500.0)),
    ("Clorox Bleach 1 L, Pack of Two", {"item volume": "1 Liters", "size": "1 l (Pack of 2)"}, ("ml", 2000.0)),
    ("Fairy Dish Soap 500 ml", {"item volume": "500 Milliliters", "size": "500 ml", "unit count": "500.0 Fl Oz"},
     ("ml", 500.0)),                                                    # the title confirms the ml label
    ("Dettol Disinfectant Spray Aerosol 450 ml", {"item form": "Aerosol", "item volume": "450 Milliliters"},
     ("ml", 450.0)),                                                    # an aerosol in ml is a volume
    ("Only Natural Pet Stain + Odor Remover 32 Fl Oz", {"item form": "Bar, Liquid", "item volume": "32 Fluid Ounces"},
     ("fl oz", 32.0)),                                                  # any liquid form wins
    # final review (2026-11-23): a title that shows its total is not multiplied again
    ("Dettol Hand Wash 400ml Twin Pack 2x200ml", {"item volume": "400 Milliliters"}, ("ml", 400.0)),
    ("Harpic Toilet Cleaner Liquid Original, 1.5L Twin Pack (2 x 750ml)", {"item volume": "1.5 Liters"}, ("ml", 1500.0)),
    ("Dettol Antibacterial Power Floor Cleaner Lemon 1.8L Twin Pack (2 x 900ml)",
     {"item volume": "1.8 Liters", "unit count": "1800.0 Milliliters", "number of items": "2"}, ("ml", 1800.0)),
    ("Fabric Softener 1L for Towels", {"item volume": "1 Liters", "unit count": "1000.0 Milliliters"}, ("ml", 1000.0)),
    ("Clorox Bleach 4L (1 Gallon)", {"item volume": "4 Liters"}, ("ml", 4000.0)),
    ("Clorox Bleach 1.89L Twin Pack", {"item volume": "1.89 Liters", "size": "1.89 l (Pack of 2)"}, ("ml", 3780.0)),
    ("Fairy 500ml x 2, Lemon", {"item volume": "1000 Milliliters", "size": "1 Litre"}, ("ml", 1000.0)),
    ("Simple Green Cleaner 32 oz (946 ml) x 2", {"item volume": "946 Milliliters", "number of items": "2"},
     ("ml", 1892.0)),                                                    # one unit of the pack confirms it
])
def test_conservative_items_with_a_size(title, attrs, expected):
    unit, amount = amazon_apify.amazon_size(title, attrs)
    assert unit == expected[0]
    assert amount == pytest.approx(expected[1])


@pytest.mark.parametrize("value", [32, 3.5, ["32 oz"], {"a": 1}, None])
def test_non_text_attributes_never_raise(value):
    attrs = {"size": value, "item volume": value, "unit count": value, "item form": value, "number of items": value}
    result = amazon_apify.amazon_size("good Cleaner 500 ml", attrs)
    assert result is None or result[1] > 0


def test_a_powder_in_oz_with_grams_is_dropped():
    assert amazon_apify.amazon_size("Bar Keepers Friend Cleanser Powder 21 oz (595 g)",
                                    {"item weight": "595 Grams"}) is None


def test_a_ml_attribute_is_only_relabeled_by_an_equal_fl_oz_number():
    # 500 ml next to 16.9 fl oz is a real 500 ml (they agree as volumes), not 500 fl oz.
    assert amazon_apify.amazon_size("good Cleaner", {"item volume": "16.9 Fluid Ounces", "size": "500 ml"}) == \
        ("fl oz", 16.9)
    # Two ml attributes with no fl oz source keep their ml label.
    assert amazon_apify.amazon_size("good Cleaner", {"size": "128 ml", "unit count": "128 Milliliters"}) == \
        ("ml", 128.0)
    # The title is never relabeled.
    assert amazon_apify.amazon_size("good Cleaner 32 ml", {"item volume": "32 Fluid Ounces"}) is None


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
    (None, "good Cleaner 16.9 fl oz", "16.9 Fluid Ounces", "unknown"),   # a fl oz size is not a signal
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
