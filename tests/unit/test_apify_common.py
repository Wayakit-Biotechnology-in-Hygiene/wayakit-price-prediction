"""Examples for `scraper/scrapers/apify_common.py` (F6): the Apify adapters' own parsing.

The shared parsers in `scraper/utils.py` keep bugs 1-4 (Mafer, 2026-11-22: fixed only in the
adapters); these examples pin the adapters' answers to the same inputs:
- bug 1 "1 litre" is 1000 ml; bug 2 weights ("2 kilograms", "907 Grams", "Net Wt 16 oz") are never
  a volume, while a plain "oz" / "Ounces" is fl oz and a gallon 128 fl oz (Mafer, 2026-11-23); bug 3 "1,000 ml" is 1000; bug 4 ".5 L" is 500 ml; bug 10 "1e3 ml" has no size; bug 11
  a 5000-digit number gives None without raising;
- "3X concentrate" is never a pack multiplier (question 12 stays open for the shared parser);
- packs multiply (C10), counts for units mode, prices (thousands vs decimal comma, Arabic digits),
  SAR aliases, the Wayakit exclusion (our own brand, C09), origin precedence (data only), and the
  2-of-N size agreement used by Amazon.
"""
from __future__ import annotations

import math

import pytest

from tests.conftest import load_module

pytestmark = pytest.mark.unit

m = load_module("scraper/main.py")
from scrapers import apify_common as common  # noqa: E402

FLOZ_ML = 29.5735


# --- parse_volume -----------------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("1 litre", ("ml", 1000.0)),                       # bug 1
    ("Cleaner 1 Liter", ("ml", 1000.0)),
    ("1,000 ml", ("ml", 1000.0)),                      # bug 3
    (".5 L", ("ml", 500.0)),                           # bug 4
    ("500 Milliliters", ("ml", 500.0)),
    ("750ml", ("ml", 750.0)),
    ("2 Ltr", ("ml", 2000.0)),
    ("16.9 fl oz", ("fl oz", 16.9)),
    ("16.9 FL. OZ.", ("fl oz", 16.9)),
    ("169.0 Fluid Ounces", ("fl oz", 169.0)),
    ("3 x 500ml", ("ml", 1500.0)),
    ("3x500 ml", ("ml", 1500.0)),
    ("500 ml x 3", ("ml", 1500.0)),
    ("Pack of 6, 1 L", ("ml", 6000.0)),
    ("Cleaner 500ml Pack of 3", ("ml", 1500.0)),
    ("500 ml (Pack of 3)", ("ml", 1500.0)),
    ("Shampoo 400 ml, 2 Pack", ("ml", 800.0)),
    ("Detergent 3X concentrate 500 ml", ("ml", 500.0)),  # "3X" is not a multiplier
    # Plain oz and gallons are fl oz (2026-11-23 smoke run titles).
    ("12 oz", ("fl oz", 12.0)),
    ("Pet Stain and Odor Remover | 32 Ounces", ("fl oz", 32.0)),
    ("128 Ounce (Pack of 1)", ("fl oz", 128.0)),
    ("Clorox Pet Urine Remover, Spray Bottle, 24oz -709ml", ("fl oz", 24.0)),
    ("Stain Cleaner, 32oz (2 Pack)", ("fl oz", 64.0)),
    ("3 x 16 oz", ("fl oz", 48.0)),
    ("16 oz x 2", ("fl oz", 32.0)),
    ("Floral Fresh, 1 Gallon", ("fl oz", 128.0)),
    ("1 Gallons", ("fl oz", 128.0)),
    ("5 gal", ("fl oz", 640.0)),
    ("Meguiar's Car Wash, 64-Ounce", ("fl oz", 64.0)),                 # hyphenated (review, 2026-11-23)
    ("Lysol 19-Oz", ("fl oz", 19.0)),
    ("Simple Green 1-Gallon", ("fl oz", 128.0)),
    ("16 ozs", ("fl oz", 16.0)),
    ("500-ml", ("ml", 500.0)),
    # A fraction is never read, but a later explicit size is; a slash alone does not block a size.
    ("Pine-Sol, 1/2 Gal, 64 Fl Oz", ("fl oz", 64.0)),
    ("Bleach 3/1 Gallon (128 fl oz each)", ("fl oz", 128.0)),
    ("Dettol Antiseptic Liquid/500ml", ("ml", 500.0)),
    ("Fabric Softener Spring/3L", ("ml", 3000.0)),
    ("Cleaner for Floors, 1 Gallon", ("fl oz", 128.0)),
    ("Cleaner Up to 500 ml", ("ml", 500.0)),
    # A count is never a pack multiplier in parse_volume (the adapters treat such titles as ambiguous).
    ("Method All-Purpose Cleaner Spray, 28 oz, 4 Count", ("fl oz", 28.0)),
    ("12 oz - Pack of 6", ("fl oz", 72.0)),
])
def test_parse_volume_examples(text, expected):
    unit, amount = common.parse_volume(text)
    assert unit == expected[0]
    assert amount == pytest.approx(expected[1])


@pytest.mark.parametrize("text", [
    "2 kilograms",          # bug 2: never grams, never a volume
    "907 Grams",
    "500 g",
    "Net Wt. 16 oz",        # oz right after "net wt" / "net weight" is a weight
    "net weight: 16oz",
    "NET WT 8 OZ (227 g)",
    "Bar Keepers Friend Cleanser Powder 21 oz (595 g)",   # a gram equivalent right after the oz
    "Ajax powder 14 oz / 396g",
    "Bar Keepers Friend 21 oz Net Wt",
    "Clorox Splash-Less Bleach, 1/2 Gallon",   # fractions are not read (dropped, never 2 gallons)
    "1 1/2 Gallon",
    "1-1/2 Gallon",
    "Lysol Disinfectant Spray, 12/19 oz",      # case notation (12 x 19 oz), never 0.63 fl oz
    "Cleaner 4/5 L",
    "Dishwasher Pods 16 oz",                    # a plain oz next to a solid or an aerosol is a weight
    "Car Wax Paste 11 oz",
    "Meguiar's Clay Bar 3.5 oz",
    "Arm & Hammer Baking Soda 16 oz",
    "Odor Absorbing Gel Beads 14 oz",
    "Deodorant Stick 2.6 oz",
    "Febreze Air Freshener Aerosol 8.8 oz",
    "Dettol Liquid 1.000 ml",                   # 1 ml or 1000 ml? not read
    "Laundry Powder, 40 oz",
    "Tide PODS 16 ct, 15.2 oz",
    "2 lbs",
    "1e3 ml",               # bug 10: no exponent
    "9" * 5000 + " ml",     # bug 11: no ValueError
    "Toilet brush",
    "",
    "0 ml",
    "3X concentrate",
])
def test_parse_volume_rejects(text):
    assert common.parse_volume(text) is None


# Final review of the 2026-11-23 rules: each case was a confirmed regression or misfire.
@pytest.mark.parametrize("title, expected", [
    # A title that already shows the total is never multiplied again by its pack phrase.
    ("Dettol Hand Wash 400ml Twin Pack 2x200ml", ("ml", 400.0)),
    ("Lux Hand Wash Twin Pack 1L (2x500ml)", ("ml", 1000.0)),
    ("Fairy Dishwashing Liquid 1L Twin Pack (2 x 500ml)", ("ml", 1000.0)),
    ("Clorox Bleach 2L Double Pack 2x1L", ("ml", 2000.0)),
    ("Clorox Bleach 3.78L Twin Pack 2x1.89L", ("ml", 3780.0)),
    ("Glass Cleaner 1 Liter (2 Packs of 500 ml)", ("ml", 1000.0)),
    ("Dettol Hand Wash 400ml Pack of 2 2x200ml", ("ml", 400.0)),
    ("Dettol 500 ml x 2 = 1 L", ("ml", 1000.0)),
    ("Pet Odor Spray 120ml, 2 Bottles", ("ml", 240.0)),
    ("Shampoo 1 L Pack of 3", ("ml", 3000.0)),
    # The same quantity in a rounded US unit is not a second product.
    ("Clorox Bleach 4L (1 Gallon)", ("ml", 4000.0)),
    ("Glass Cleaner 500ml/16oz", ("ml", 500.0)),
    ("Glass Cleaner 1L / 32oz", ("ml", 1000.0)),
    ("Car Shampoo 20L / 5 Gallon", ("ml", 20000.0)),
    ("Cleaner 1 L (33.8 fl oz)", ("ml", 1000.0)),
    ("Bleach 1 Gallon / 4 L", ("ml", 4000.0)),
    # One product, not a kit: the number is the size, a sprayer or brush is part of the bottle.
    ("Fabric Softener 1L for Towels", ("ml", 1000.0)),
    ("Floor Cleaner 1L for Mop", ("ml", 1000.0)),
    ("Car Shampoo 500ml Foam Sprayer", ("ml", 500.0)),
    ("Windex Glass Cleaner 500ml Trigger Sprayer", ("ml", 500.0)),
    ("3M Microfiber Cloth Cleaner 500 ml", ("ml", 500.0)),
    ("Fairy Dishwashing Liquid 750ml +25% Extra", ("ml", 750.0)),
    ("Gyeon Q2M Bathe+ 500 ml", ("ml", 500.0)),
    ("Ceramic Coating 9H+ 30ml", ("ml", 30.0)),
    ("Pet Stain Remover for Set-In Stains 500 ml", ("ml", 500.0)),
    ("WD-40 Bottle with Trigger Spray 500ml", ("ml", 500.0)),            # the 40 of WD-40 is not a pack
    ("Cleaner 500 ml SİX PACK", ("ml", 500.0)),                           # never raises
    # A kit of different products, a yield or a refill set: no size.
    ("Car Wash Soap 64 fl oz with 3 Pack of Premium Microfiber Towels", None),
    ("Car Wash Shampoo 1L with 2 Packs of Microfiber Towels", None),
    ("Car Shampoo 1L with Twin Pack Microfiber Towels", None),
    ("Car Shampoo 1L Pack of Ten Towels", None),
    ("Glass Cleaner 500 ml with 2 Pack Wipes", None),
    ("Car Wash Soap 16 fl oz & Microfiber Towels 3 Pack", None),
    ("Car Wash Soap 473 ml with Microfiber Towels (Pack of 3)", None),
    ("Car Wash Shampoo 1L with Microfiber Towel", None),
    ("Glass Cleaner 500ml with Microfiber Cloth", None),
    ("Car Wash Shampoo 1 L + Microfiber Towel", None),
    ("Car Polish 500 ml with 2 packs of microfiber", None),
    ("Dog Shampoo 500ml / Dog Conditioner 500ml", None),
    ("Leather Cleaner 250 ml, Leather Conditioner 250 ml", None),
    ("Spray 450ml + Wipes 40 Units", None),
    ("Dettol Hand Wash 1.5L, 3 Units of 500ml", None),
    ("AC Cleaner Spray 500ml for 2 Units", None),
    ("Multi-Surface Cleaner Concentrate 946 ml, Makes up to 8 Bottles", None),
    ("Glass Cleaner Refill 1 L, Fills 2 Bottles", None),
    ("Concentrated Cleaner Refill 100 ml, Makes 4 Bottles", None),
    ("Glass Cleaner 500 ml with 2 Refills", None),
    ("Purex Liquid Laundry Detergent 2X 150 fl oz", None),               # 2X concentrate or 2 bottles?
    # Fifth review round (2026-11-23).
    ("Simple Green Cleaner 32 oz (946 ml) x 2", ("ml", 1892.0)),          # "x N" after a bracket
    ("Dettol Antiseptic Liquid (500 ml) x 2", ("ml", 1000.0)),
    ("Fairy Dishwashing Liquid Lemon 500ml x 2, Original Scent", ("ml", 1000.0)),
    ("Fairy Dishwashing Liquid 500ml x 2.", ("ml", 1000.0)),
    ("Dettol 500ml (x2)", ("ml", 1000.0)),
    ("Dettol 500ml each x 2", ("ml", 1000.0)),
    ("Dettol Antiseptic Liquid 500ml, x2", ("ml", 1000.0)),
    ("Dettol 2 x 500 ml (16.9 fl oz)", ("ml", 1000.0)),
    ("Dettol Antiseptic 1 L (2 x 500 ml / 16.9 fl oz)", ("ml", 1000.0)),
    ("Bleach 4 x 3.78 L (1 Gallon each)", ("ml", 15120.0)),
    ("Phosphate Free Microfiber Detergent 1L", ("ml", 1000.0)),
    ("Fabric Softener 3L for Bedding and Towels", ("ml", 3000.0)),
    ("Microfiber Wash 1L, Safe for All Microfiber Towels and Cloths", ("ml", 1000.0)),
    ("Two Bucket Wash Car Shampoo 1L", ("ml", 1000.0)),
    ("Car Shampoo 1L for 2 Bucket Method", ("ml", 1000.0)),
    ("Spray 750ml, Pack of 2", ("ml", 1500.0)),
    ("Bleach 3.78 L (128 fl oz)", ("ml", 3780.0)),
    ("Value Pack 2 x 1 L", ("ml", 2000.0)),
    ("Dettol 500ml x 2.5L", None),
    ("Car Shampoo 1L, Microfiber Towel 3 Pack", None),                   # kit after a comma, dash, slash or bar
    ("Car Wash Soap 64 fl oz, Wash Mitt, 2 Pack", None),
    ("Car Shampoo 1 L / Microfiber Towels (Pack of 3)", None),
    ("Car Shampoo 1L - Microfiber Cloth - Pack of 2", None),
    ("Tire Shine Spray 500ml, Foam Applicator Pads, 2 Pack", None),
    ("Car Wash Shampoo 1L | Wash Mitt", None),
    ("Car Shampoo 1L incl. Microfiber Towels, Pack of 2", None),          # "incl.", "w/", "Included"
    ("Car Shampoo 1L w/ Microfiber Towel, Pack of 2", None),
    ("Car Wash Shampoo 1L - Microfiber Towel Included", None),
    ("Car Shampoo 1L (Towel Included)", None),
    ("Car Shampoo 1L with 2 Pack Chamois", None),
    ("Car Wash Shampoo 1L with 2 Pack Clay Bars", None),
    ("Leather Cleaner 250ml, Conditioner 250ml", None),                  # any word between two sizes
    ("Car Shampoo 1L Conditioner 1L", None),
    ("Window Cleaner 750 ml Trigger, 750 ml Refill", None),
    ("Fabric Softener 1L Blue, 1L Pink", None),
    ("Leather Cleaner 250 ml, Leather Conditioner 500 ml, Pack of 2", None),
    ("Wheel Cleaner 500 ml, Tire Shine 1 L, Twin Pack", None),
    ("Clorox Bleach 1.89L 1+1", None),                                     # offers and unread packs
    ("Fairy 1+1 Free 750ml", None),
    ("Harpic Toilet Cleaner 750 ml, 2+1", None),
    ("Clorox Bleach 1.89L Pack 2", None),
    ("Floor Cleaner 1L (Carton of 12)", None),
    ("Car Wash Bucket 5 Gallon", None),                                   # a gallon container
    ("Pump Sprayer 2 Gallon", None),
])
def test_final_review_titles(title, expected):
    got = common.title_volume(title, explicit_only=True)
    assert got == (expected if expected is None else (expected[0], pytest.approx(expected[1])))


@pytest.mark.parametrize("value", ["N.A.", "N/A.", "Not applicable.", "Not Known", "null", "Unspecified",
                                   "See Package.", "Multiple Countries", "TBD"])
def test_placeholder_countries_are_not_an_origin(value):
    assert common.derive_origin(country_of_origin=value) == "unknown"


@pytest.mark.parametrize("text", ["1 oz " * 1000, "9 oz " * 1000, "500 ml " * 3000, "wt" + " " * 20000 + "x 5 oz",
                                  "Cleaner 16 oz, " * 400])
def test_many_size_tokens_are_fast(text):
    import time
    start = time.perf_counter()
    common.parse_volume(text), common.title_volume(text), common.title_volume(text, explicit_only=True)
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize("title, expected", [
    # Noon, and Amazon's title-only rule: nothing confirms the title, so a plain oz or a gallon
    # alone is not enough (Mafer, 2026-11-23). Only ml, liters and fl oz count.
    ("Spray 32 oz", None),
    ("Pet Stain and Odor Remover | 32 Ounces", None),
    ("Bleach 1 Gallon", None),
    ("Lysol Disinfectant Spray, Crisp Linen, 19 oz", None),
    ("Clorox Pet Urine Remover, Spray Bottle, 24oz -709ml", ("ml", 709.0)),   # the ml says it
    ("Cleaner 16.9 fl oz", ("fl oz", 16.9)),
    ("Cleaner 500 ml", ("ml", 500.0)),
    ("Pack of 3, 500 ml each", ("ml", 1500.0)),
    ("Net Wt 16 oz Cleaner 473 ml", None),                                      # a weight first
    ("Car Wash Kit 500 ml", None),                                              # still ambiguous
])
def test_title_volume_explicit_only(title, expected):
    assert common.title_volume(title, explicit_only=True) == (expected if expected is None else
                                                              (expected[0], pytest.approx(expected[1])))


@pytest.mark.parametrize("text, expected", [
    ("500 ml", common.EXPLICIT), ("16.9 fl oz", common.EXPLICIT), ("1 L", common.EXPLICIT),
    ("32 oz", common.PLAIN_OZ), ("1 Gallon", common.GALLON), ("500 g", None), ("Brush", None), (None, None),
    (32, None),
])
def test_volume_kind(text, expected):
    assert common.volume_kind(text) == expected


@pytest.mark.parametrize("text", [
    "1" + " " * 5000 + "x", "3 x 500" + " " * 5000, "with 2 " + "a " * 3000, "pack of 2" + " " * 5000 + "towels",
    "makes 1" + " " * 5000 + "x", "9" * 5000 + " oz and " + "9" * 5000 + " ml",
])
def test_long_odd_texts_are_fast(text):
    """No regex here may backtrack quadratically on a long run of spaces (review, 2026-11-23)."""
    import time
    start = time.perf_counter()
    common.parse_volume(text), common.volume_pack(text), common.title_volume(text), common.title_is_ambiguous(text)
    assert time.perf_counter() - start < 0.5


# --- title_volume: ambiguous titles give no size (conservative mode, Mafer 2026-11-23) --------------

@pytest.mark.parametrize("title", [
    # a dose, a dilution or a capacity (review rounds 1-3, 2026-11-23)
    "Tetra AquaSafe Water Conditioner, Treats up to 50 Gallons, 8.45 oz",
    "Car Wash Shampoo Concentrate, Makes 100 Gallons, 16 oz",
    "Aquarium Water Conditioner, Treats 50-100 Gallons, 4 oz",
    "Water conditioner treats 10 to 50 gallons, 4 oz",
    "Aquarium Water Conditioner for 55 Gallon Tanks, 8 oz",
    "Makes up to 2-4 Gallons, 64 oz",
    "Snow Foam, Use 1 oz per Gallon, 16 oz",
    "Car Shampoo Concentrate, 1 oz makes 1 gallon, 16 fl oz",
    "Concentrate 2 oz makes 1 gallon",
    "Car Shampoo Concentrate 1 L makes 100 L",
    "Snow Foam 1 oz/gallon, 32 oz",
    "Mix 1 oz to 1 gallon of water, 16 oz",
    "Degreaser 32 oz, dilute with 1 gallon water",
    "Floor Cleaner 1 L, use 30 ml per 5 L",
    "Pond Treatment 16 oz, covers 5,000 gallons",
    "Large Capacity Spray Bottle 500 ml",
    "Wash Bucket 5 Gallon Tank",
    # a count of 2 or more: bottles, pods, wipes or an accessory
    "Method All-Purpose Cleaner Spray, 28 oz, 4 Count",
    "Engine Flush 1 L, 12 pk",
    # pack notations the parser does not read
    "Bleach 1 gal Bottle, 6/Carton",
    "Bleach 1 Gallon, Case of 6",
    "Glass Cleaner 1 L Duo",
    "Car Care Bundle 16 oz",
    "Shampoo 500 ml Multipack",
    "Dish Soap 500 ml Multi-Pack",
    # a kit of different products is not stored: it cannot be classified (Mafer, 2026-11-23)
    "Odor Eliminator 32 oz Spray + 3 Refills",
    "Car Wash Soap 64 oz with 2 Pack Wash Mitts",
    "Car Wash Soap 64 oz with two microfiber towels",
    "Car Wash Shampoo 1L with Pack of 3 Microfiber Towels",
    "Car Shampoo 500ml + Free Pack of 3 Sponges",
    "Car Wash Soap 64 oz and 3 Pack Microfiber Towels",
    "Turtle Wax Car Wash Kit 16 fl oz Shampoo, Wax and Microfiber",
    "Leather Care Gift Set 250 ml",
    "Leather Cleaner 16 fl oz and Leather Conditioner 16 fl oz",
    "Dog Shampoo 16 fl oz & Conditioner 16 fl oz",
    # two sizes that are not the same thing said twice
    "Dettol Liquid 500ml and 1L Refill",
    "Windex Glass Cleaner 23 fl oz Spray Bottle and 67.6 fl oz Refill",
    "Dog Shampoo Concentrate, 1 fl oz to 32 fl oz Water, 16 fl oz Bottle",
    "Car Shampoo Concentrate 30ml to 1L Water, 500ml Bottle",
    "Car Shampoo Concentrate, Use 50 ml in 10 L Bucket, 1 L",
    "Dog Shampoo 250 ml - 2 Units",            # "N Units": bottles or wipes?
    "Floral Fresh 32Oz, Blue, 32 Oz Spray",    # same shape as "1L Blue, 1L Pink" (two scents)
    # "1.000 ml": 1 ml or 1000 ml?
    "Dettol Liquid 1.000 ml",
    None,
    "",
])
def test_title_volume_refuses_ambiguous_titles(title):
    assert common.title_volume(title) is None


@pytest.mark.parametrize("title, expected", [
    ("Cleaner for Floors, 1 Gallon", ("fl oz", 128.0)),
    ("Cleaner Up to 500 ml", ("ml", 500.0)),
    ("Stain Cleaner, 32oz (2 Pack)", ("fl oz", 64.0)),
    ("Cleaner 500ml Pack of 3", ("ml", 1500.0)),
    ("Only Natural Pet Advanced Dog Stain + Odor Remover with Oxy, 32 Fl Oz", ("fl oz", 32.0)),  # "+" without a number
    ("Enzymatic Cleaner with 2X Pro-Bacteria Cleaning Power | 32 Ounces", ("fl oz", 32.0)),     # "with 2X" is not an item
    ("Makes floors shine, 1 L", ("ml", 1000.0)),                                               # "makes" without a number
    ("Cleaner 500 ml, 1 Count", ("ml", 500.0)),
    ("Pet Treats Odor Remover 500 ml", ("ml", 500.0)),                                         # "treats" without a number
    # Not a kit and not a dose (review, 2026-11-23).
    ("Hand Sanitizer 500 ml with 70% Alcohol", ("ml", 500.0)),
    ("Cleaner 500 ml with 3 enzymes", ("ml", 500.0)),
    ("Lysol Triple Action Toilet Cleaner 750 ml", ("ml", 750.0)),
    ("Car Shampoo 500 ml - Makes 50 Washes", ("ml", 500.0)),
    ("Floor Cleaner 1L covers 200 m2", ("ml", 1000.0)),
    ("Dr.Beckmann Pet Stain remover shampoo with Cleaning brush 650 ml", ("ml", 650.0)),
    ("Non-Stick Pan Cleaner 500 ml", ("ml", 500.0)),
    # The same size said twice is not a conflict.
    ("Clorox Pet Urine Remover, Spray Bottle, 24oz -709ml", ("fl oz", 24.0)),
    ("Cleaner 16.9 fl oz (500 ml)", ("fl oz", 16.9)),
    ("Cleaner 1.5 L (3 x 500 ml)", ("ml", 1500.0)),
    ("Cleaner 500ml, Pack of 3 (1.5 L total)", ("ml", 1500.0)),
    # A multipack of the same product is kept with its total (Mafer, 2026-11-23).
    ("Glass Cleaner 32 oz Twin Pack", ("fl oz", 64.0)),
    ("Antibacterial Spray 400ml Twinpack", ("ml", 800.0)),
    ("Cleaner 500 ml Double Pack", ("ml", 1000.0)),
    ("Cleaner 750ml Triple Pack", ("ml", 2250.0)),
    ("Floor Cleaner 1 Liter Pack of Six", ("ml", 6000.0)),
    ("Floor Cleaner 1 Liter Six Pack", ("ml", 6000.0)),
    ("Clorox Bleach 1 L (2 Packs)", ("ml", 2000.0)),
    ("Shampoo 500ML*2", ("ml", 1000.0)),
    ("Shampoo 2*500ml", ("ml", 1000.0)),
    ("Dog Shampoo 250 ml Two Bottles", ("ml", 500.0)),
    ("Pine-Sol Cleaner 1 L, 2 Jugs", ("ml", 2000.0)),
])
def test_title_volume_keeps_plain_titles(title, expected):
    unit, amount = common.title_volume(title)
    assert unit == expected[0]
    assert amount == pytest.approx(expected[1])


@pytest.mark.parametrize("value", [None, 12, 3.5])
def test_parse_volume_non_text_never_raises(value):
    result = common.parse_volume(value)
    assert result is None or (result[0] in ("ml", "fl oz") and result[1] > 0)


def test_units_mode_packs_ignore_oz_and_gallons():
    """The oz / gallon change is volume mode only (Mafer, 2026-11-23): units-mode counts are unchanged."""
    assert common.parse_count("Car detailing brush kit with 16 oz cleaner x 2") is None
    assert common.parse_count("2 x 13 Gallon trash bags") is None
    assert common.parse_count("Pet wipes 6 oz x 3") is None
    assert common.pack_count("3 x 16 oz") == 1
    assert common.volume_pack("3 x 16 oz") == 3
    assert common.volume_pack("Lysol Spray 19 fl oz x 3") == 3
    assert common.volume_pack("Method 28 oz, 4 Count") == 1, "a count is never a bottle pack"


@pytest.mark.parametrize("text, expected", [
    ("Method 28 oz, 4 Count", True), ("Tide PODS 16 ct, 15.2 oz", True), ("Engine Flush 1 L, 12 pk", True),
    ("60pcs Pet Ear Wipes", True), ("Cleaner 500 ml, 1 Count", False), ("Cleaner 500 ml", False), (None, False),
])
def test_has_count(text, expected):
    assert common.has_count(text) is expected


def test_first_size_token_wins_and_a_weight_first_rejects():
    assert common.parse_volume("Cleaner 500 ml refill 1 L") == ("ml", 500.0)
    assert common.parse_volume("Powder 907 g makes 5 L") is None


# --- parse_count ------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("80 wet wipes", 80),
    ("3 x 80 wipes", 240),
    ("Pack of 3, 80 wipes", 240),
    ("pack of 80 wipes", 80),
    ("Set of 4", 4),
    ("Microfiber cloths 12 pcs", 12),
    ("Dog wipes 100 count", 100),
    ("3 Pack", 3),
])
def test_parse_count_examples(text, expected):
    assert common.parse_count(text) == expected


@pytest.mark.parametrize("text", ["Toilet brush", "", None, "0 wipes", "Pack of 1"])
def test_parse_count_none(text):
    assert common.parse_count(text) is None


# --- pack_count -------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("3 x 500ml", 3), ("500ml x 3", 3), ("Pack of 6", 6), ("set of 2", 2), ("4-Pack", 4),
    ("6 bottles", 6), ("Detergent 3X concentrate", 1), ("Cleaner 500 ml", 1), ("", 1),
    ("Pack of 500", 1),  # outside 1..100
])
def test_pack_count(text, expected):
    assert common.pack_count(text) == expected


# --- dimensions and breakdowns are never a pack (review round 1, C10) ---------------------------------
@pytest.mark.parametrize("text, expected", [
    ("Microfiber Cleaning Cloths, 8 Pack, 40 x 40 cm", 8),
    ("12 Pcs Microfiber Towel 30x30cm", 12),
    ("Microfiber Towel 30x30cm, Pack of 12", 12),
    ("Car brush 5 pcs 25 x 8 cm", 5),
    ("Pack of 8, 40 x 40 cm", 8),
    ("3 x 80 (240 wipes)", 240),          # the breakdown does not multiply the stated total
    ("80 wipes x 3", 240),                # a multiplier tied to the count still counts
    ("12 pcs x 30x30cm", 12),
])
def test_parse_count_ignores_dimensions(text, expected):
    assert common.parse_count(text) == expected


@pytest.mark.parametrize("text", ["Microfiber Cleaning Cloth 40 x 40 cm", "Brush 25 x 8 cm", 'Mat 20 x 30"'])
def test_a_dimension_alone_is_no_count(text):
    assert common.parse_count(text) is None


@pytest.mark.parametrize("text, expected", [
    ("Car Shampoo 1 L + Microfiber 40 x 40 cm", 1000.0),
    ("Dish Liquid 1.5 L (3 x 500 ml)", 1500.0),   # total, not total x breakdown
    ("Spray bottle 500 ml 25 x 8 cm", 500.0),
    ("500ml x 3 x 2cm", 500.0),
    ("2 x 1.5 L", 3000.0),
    ("Cleaner 1 L x 2 Pack", 2000.0),
])
def test_parse_volume_ignores_dimensions_and_breakdowns(text, expected):
    assert common.parse_volume(text) == ("ml", pytest.approx(expected))


@pytest.mark.parametrize("text", ["40 x 40 cm", "30x30cm", "25 x 8 mm", "Cloth 40 x 40 cm, Pack of 8"])
def test_pack_count_never_reads_a_dimension(text):
    assert common.pack_count(text) in (1, 8)
    assert common.pack_count(text) != 40 and common.pack_count(text) != 30 and common.pack_count(text) != 25


# --- parse_price ------------------------------------------------------------------------------------
@pytest.mark.parametrize("value, expected", [
    ("1,234.50", 1234.5), ("SAR 45", 45.0), ("٤٥", 45.0), ("١٢٫٥", 12.5), ("12,5", 12.5),
    (19.999, 20.0), (7, 7.0), ("ر.س 30.00", 30.0), ("SAR. 12", 12.0),
])
def test_parse_price_examples(value, expected):
    assert common.parse_price(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", [0, -3, "abc", float("nan"), float("inf"), None, "", "0.00", "-5", "0.001"])
def test_parse_price_none(value):
    assert common.parse_price(value) is None


# --- currency, Wayakit, text ------------------------------------------------------------------------
@pytest.mark.parametrize("currency", ["SAR", "sar", " SAR ", "ر.س", "﷼", "SR"])
def test_is_sar_true(currency):
    assert common.is_sar(currency) is True


@pytest.mark.parametrize("currency", ["AED", "USD", None, "", "$"])
def test_is_sar_false(currency):
    assert common.is_sar(currency) is False


@pytest.mark.parametrize("value", ["WAYAKIT", "Waya Kit", "waya-kit", "Wayakit Pet Odor Spray", "وايا كيت"])
def test_is_wayakit_true(value):
    assert common.is_wayakit(value) is True
    assert common.is_wayakit(None, "", value) is True


@pytest.mark.parametrize("values", [("SACO",), ("Dettol", "Amazon.sa"), (None, ""), ()])
def test_is_wayakit_false(values):
    assert common.is_wayakit(*values) is False


def test_flatten_text_and_clean():
    text = common.flatten_text({"a": ["Made in", {"b": "KSA"}], "n": 5, "none": None})
    assert "Made in" in text and "KSA" in text and "5" in text
    assert len(common.flatten_text({"x": "y" * 10000})) <= 5000
    assert len(common.flatten_text(["abc"] * 10, limit=7)) <= 7
    assert common.flatten_text(None) == ""
    assert common.clean("  a \n b\t ") == "a b"
    assert common.clean(None) == ""


# --- derive_origin (one test per precedence row, then the precedence itself) ------------------------
@pytest.mark.parametrize("kwargs, expected", [
    ({"country_of_origin": "Saudi Arabia"}, "local"),                          # 1 local
    ({"country_of_origin": "  KSA "}, "local"),
    ({"country_of_origin": "المملكة العربية السعودية"}, "local"),
    ({"country_of_origin": "China"}, "imported"),                              # 1 imported
    ({"texts": ("Floor cleaner", "Made in KSA")}, "local"),                     # 2
    ({"texts": ("Made in Saudi Arabia",)}, "local"),
    ({"texts": ("made in the kingdom of saudi arabia",)}, "local"),
    ({"texts": ("صنع في السعودية",)}, "local"),
    ({}, "unknown"),                                                           # 3
    ({"country_of_origin": "", "texts": ("Floor cleaner",)}, "unknown"),
    ({"texts": ("Pet Stain Remover 32 fl oz",)}, "unknown"),                   # a fl oz size is not a signal
])
def test_derive_origin_rows(kwargs, expected):
    assert common.derive_origin(**kwargs) == expected


@pytest.mark.parametrize("kwargs, expected", [
    ({"country_of_origin": "Saudi Arabia", "texts": ("Made in USA",)}, "local"),
    ({"country_of_origin": "China", "texts": ("Made in KSA",)}, "imported"),
])
def test_derive_origin_precedence(kwargs, expected):
    assert common.derive_origin(**kwargs) == expected


@pytest.mark.parametrize("country", ["Saudi", "Saudi Arabia (KSA)", "KSA", "Made in Saudi Arabia", "sa",
                                     "Saudia Arabia", "Kingdom of Saudia Arabia", "Saudiarabia", "K.S.A",
                                     "السعوديه", "المملكة العربية السعوديه"])
def test_non_canonical_saudi_country_is_local(country):
    assert common.derive_origin(country_of_origin=country) == "local"


@pytest.mark.parametrize("country", ["Samoa", "USA", "China", "Malaysia"])
def test_other_countries_are_imported(country):
    assert common.derive_origin(country_of_origin=country) == "imported"


@pytest.mark.parametrize("country", ["Unknown", "N/A", "Not Applicable", "Not specified", "-", "Local", "", None])
def test_a_value_that_names_no_country_is_not_an_origin(country):
    """Only an actual country can say imported (review, 2026-11-23)."""
    assert common.derive_origin(country_of_origin=country) == "unknown"
    assert common.derive_origin(country_of_origin=country, texts=("Made in KSA",)) == "local"


def test_first_agreeing_skips_pairs_that_are_not_allowed():
    sizes = [("fl oz", 19.0), ("fl oz", 19.0), ("fl oz", 19.0)]
    assert common.first_agreeing(sizes, allowed=lambda i, j: False) is None
    assert common.first_agreeing(sizes, allowed=lambda i, j: (i, j) == (1, 2)) == (1, ("fl oz", 19.0))


def _first(sources):
    return common.first_agreeing([common.parse_volume(t) if t is not None else None for t in sources])


def test_first_agreeing_reports_the_original_index():
    assert _first(["Cleaner", None, "500 ml", "500 Milliliters"]) == (2, ("ml", 500.0))
    assert _first(["Cleaner 1 L", "1000 ml"]) == (0, ("ml", 1000.0))
    assert _first(["1 L", "2 L"]) is None


def test_derive_origin_takes_no_size_unit():
    """A size in fl oz does not make a product imported (Mafer, 2026-11-23): `derive_origin`
    no longer takes the unit at all."""
    with pytest.raises(TypeError):
        common.derive_origin(unit="fl oz")


def test_derive_origin_takes_no_noon_shipping_flag():
    """2026-11-23 smoke run: Noon's `shipping_banner_details.global` is the same generic message on
    every product, so it is no longer an argument (Mafer, 2026-11-23)."""
    with pytest.raises(TypeError):
        common.derive_origin(noon_global=True)


def test_first_agreeing_works_on_parsed_sizes():
    assert common.first_agreeing([None, ("ml", 500.0), None, ("fl oz", 16.9)]) == (1, ("ml", 500.0))
    assert common.first_agreeing([("ml", 500.0), None]) is None
    assert common.first_agreeing([]) is None


def test_no_brand_list_decides_origin():
    """Origin comes only from the data: a well-known brand with no signal stays unknown."""
    assert common.derive_origin(texts=("Dettol antibacterial 500 ml", "Brand: Dettol")) == "unknown"


# --- sizes_agree / first_agreeing ------------------------------------------------------------------------
def test_sizes_agree_examples():
    assert common.sizes_agree(common.parse_volume("5 Liters"), common.parse_volume("169 Fl Oz"))
    assert common.sizes_agree(("ml", 500.0), ("fl oz", 16.9))
    assert not common.sizes_agree(("ml", 500.0), ("ml", 750.0))
    assert common.sizes_agree(("ml", 10.0), ("ml", 10.9))   # 1 ml minimum tolerance
    assert not common.sizes_agree(("ml", 10.0), ("ml", 11.5))


def test_first_agreeing_examples():
    assert _first(["5 Liters", "907 Grams"]) is None
    assert _first(["5 Liters"]) is None
    assert _first([None, None, "500 ml"]) is None
    assert _first([]) is None
    _, (unit, amount) = _first(["Cleaner", "5 Liters", "907 Grams", "169.0 Fluid Ounces"])
    assert (unit, amount) == ("ml", pytest.approx(5000.0))
    # The value of source i of the first agreeing pair (i < j, in source order).
    assert _first(["16.9 fl oz", "500 ml"])[1] == ("fl oz", pytest.approx(16.9))
    assert _first(["750 ml", "1 L", "1000 Milliliters", "0.75 L"])[1] == ("ml", pytest.approx(750.0))


# --- select_relevant / to_row -------------------------------------------------------------------------
class StubAgent:
    def __init__(self, units=None):
        self.relevance_calls, self.wipes_calls = [], []
        self.units = units or {}

    def filter_relevant(self, titles, query):
        self.relevance_calls.append((list(titles), query))
        return ["good" in t for t in titles]

    def extract_wipes_units_batch(self, titles):
        self.wipes_calls.append(list(titles))
        return [self.units.get(t, 0) for t in titles]


def _cand(title, qty=500.0, unit="ml"):
    return {"Product": title, "Price_SAR": 10.0, "Company": "B", "URL": f"https://x.invalid/{title}",
            "Unit of measurement": unit, "Total quantity": qty, "Seller": "S",
            "_origin": {"country_of_origin": None, "texts": (title,)}}


def test_select_relevant_drops_no_size_before_the_ai():
    agent = StubAgent()
    rows, reasons = common.select_relevant([_cand("good a"), _cand("good b", qty=None), _cand("bad c")],
                                           "floor cleaner", "volume", agent)
    assert [r["Product"] for r in rows] == ["good a"]
    assert agent.relevance_calls == [(["good a", "bad c"], "floor cleaner")]
    assert agent.wipes_calls == []
    assert reasons == {"no_size": 1, "not_relevant": 1}


def test_select_relevant_without_candidates_makes_no_ai_call():
    agent = StubAgent()
    rows, reasons = common.select_relevant([_cand("good b", qty=None)], "x", "volume", agent)
    assert rows == [] and agent.relevance_calls == [] and reasons["no_size"] == 1


def test_select_relevant_wipes_uses_the_batch_and_keeps_sizeless_until_then():
    agent = StubAgent(units={"good wipes a": 80})
    cands = [_cand("good wipes a", qty=None, unit="units"), _cand("good wipes b", qty=None, unit="units"),
             _cand("bad wipes", qty=None, unit="units")]
    rows, reasons = common.select_relevant(cands, "wet wipes", "units", agent)
    assert agent.relevance_calls[0][0] == ["good wipes a", "good wipes b", "bad wipes"]
    assert agent.wipes_calls == [["good wipes a", "good wipes b"]]
    assert [(r["Product"], r["Total quantity"], r["Unit of measurement"]) for r in rows] == [
        ("good wipes a", 80, "units")]
    assert reasons == {"not_relevant": 1, "no_size": 1}


def test_select_relevant_units_mode_without_wipes_keyword_has_the_size_gate():
    agent = StubAgent()
    rows, reasons = common.select_relevant([_cand("good brush", qty=None, unit="units")], "toilet brush",
                                           "units", agent)
    assert rows == [] and agent.relevance_calls == [] and agent.wipes_calls == []


def test_select_relevant_propagates_agent_errors():
    class Boom(StubAgent):
        def filter_relevant(self, titles, query):
            raise m.RelevanceAgentFatalError("fake AccessDeniedException")

    with pytest.raises(m.RelevanceAgentFatalError):
        common.select_relevant([_cand("good a")], "x", "volume", Boom())


def test_to_row_has_exactly_the_contract_keys():
    cand = _cand("good a", qty=473.0, unit="fl oz")
    cand["_sku"] = "Z1"
    row = common.to_row(cand)
    assert set(row) == {"Product", "Price_SAR", "Company", "URL", "Unit of measurement", "Total quantity",
                        "Seller", "Origin"}
    assert row["Origin"] == "unknown"  # a fl oz size is not an origin signal
    assert common.to_row(_cand("good a"))["Origin"] == "unknown"
    assert not any(k.startswith("_") for k in row)
    assert math.isfinite(row["Total quantity"])
