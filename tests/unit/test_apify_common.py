"""Examples for `scraper/scrapers/apify_common.py` (F6): the Apify adapters' own parsing.

The shared parsers in `scraper/utils.py` keep bugs 1-4 (Mafer, 2026-11-22: fixed only in the
adapters); these examples pin the adapters' answers to the same inputs:
- bug 1 "1 litre" is 1000 ml; bug 2 weights ("2 kilograms", "907 Grams") and plain "oz" are never
  a volume; bug 3 "1,000 ml" is 1000; bug 4 ".5 L" is 500 ml; bug 10 "1e3 ml" has no size; bug 11
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
])
def test_parse_volume_examples(text, expected):
    unit, amount = common.parse_volume(text)
    assert unit == expected[0]
    assert amount == pytest.approx(expected[1])


@pytest.mark.parametrize("text", [
    "2 kilograms",          # bug 2: never grams, never a volume
    "907 Grams",
    "500 g",
    "12 oz",                # plain oz is a weight on Amazon: rejected
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


@pytest.mark.parametrize("value", [None, 12, 3.5])
def test_parse_volume_non_text_never_raises(value):
    result = common.parse_volume(value)
    assert result is None or (result[0] in ("ml", "fl oz") and result[1] > 0)


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
    ({"noon_global": True}, "imported"),                                       # 3
    ({"unit": "fl oz"}, "imported"),                                           # 4
    ({}, "unknown"),                                                           # 5
    ({"country_of_origin": "", "texts": ("Floor cleaner",), "noon_global": False, "unit": "ml"}, "unknown"),
    ({"noon_global": None, "unit": "units"}, "unknown"),
])
def test_derive_origin_rows(kwargs, expected):
    assert common.derive_origin(**kwargs) == expected


@pytest.mark.parametrize("kwargs, expected", [
    ({"country_of_origin": "Saudi Arabia", "unit": "fl oz"}, "local"),
    ({"texts": ("Made in KSA",), "noon_global": True}, "local"),
    ({"country_of_origin": "China", "texts": ("Made in KSA",)}, "imported"),
    ({"country_of_origin": "Saudi Arabia", "noon_global": True}, "local"),
    ({"noon_global": True, "unit": "ml"}, "imported"),
])
def test_derive_origin_precedence(kwargs, expected):
    assert common.derive_origin(**kwargs) == expected


@pytest.mark.parametrize("country", ["Saudi", "Saudi Arabia (KSA)", "KSA", "Made in Saudi Arabia", "sa"])
def test_non_canonical_saudi_country_is_local(country):
    assert common.derive_origin(country_of_origin=country) == "local"


@pytest.mark.parametrize("country", ["Samoa", "USA", "China", "Malaysia"])
def test_other_countries_are_imported(country):
    assert common.derive_origin(country_of_origin=country) == "imported"


def test_agreed_source_reports_the_original_index():
    assert common.agreed_source(["Cleaner", None, "500 ml", "500 Milliliters"]) == (2, ("ml", 500.0))
    assert common.agreed_source(["Cleaner 1 L", "1000 ml"]) == (0, ("ml", 1000.0))
    assert common.agreed_source(["1 L", "2 L"]) is None


def test_no_brand_list_decides_origin():
    """Origin comes only from the data: a well-known brand with no signal stays unknown."""
    assert common.derive_origin(texts=("Dettol antibacterial 500 ml", "Brand: Dettol")) == "unknown"


# --- sizes_agree / agreed_size ------------------------------------------------------------------------
def test_sizes_agree_examples():
    assert common.sizes_agree(common.parse_volume("5 Liters"), common.parse_volume("169 Fl Oz"))
    assert common.sizes_agree(("ml", 500.0), ("fl oz", 16.9))
    assert not common.sizes_agree(("ml", 500.0), ("ml", 750.0))
    assert common.sizes_agree(("ml", 10.0), ("ml", 10.9))   # 1 ml minimum tolerance
    assert not common.sizes_agree(("ml", 10.0), ("ml", 11.5))


def test_agreed_size_examples():
    assert common.agreed_size(["5 Liters", "907 Grams"]) is None
    assert common.agreed_size(["5 Liters"]) is None
    assert common.agreed_size([None, None, "500 ml"]) is None
    assert common.agreed_size([]) is None
    unit, amount = common.agreed_size(["Cleaner", "5 Liters", "907 Grams", "169.0 Fluid Ounces"])
    assert (unit, amount) == ("ml", pytest.approx(5000.0))
    # The value of source i of the first agreeing pair (i < j, in source order).
    assert common.agreed_size(["16.9 fl oz", "500 ml"]) == ("fl oz", pytest.approx(16.9))
    assert common.agreed_size(["750 ml", "1 L", "1000 Milliliters", "0.75 L"]) == ("ml", pytest.approx(750.0))


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
            "_origin": {"country_of_origin": None, "texts": (title,), "noon_global": None}}


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
    cand["_sku"], cand["_has_shipping"] = "Z1", False
    row = common.to_row(cand)
    assert set(row) == {"Product", "Price_SAR", "Company", "URL", "Unit of measurement", "Total quantity",
                        "Seller", "Origin"}
    assert row["Origin"] == "imported"  # fl oz
    assert common.to_row(_cand("good a"))["Origin"] == "unknown"
    assert not any(k.startswith("_") for k in row)
    assert math.isfinite(row["Total quantity"])
