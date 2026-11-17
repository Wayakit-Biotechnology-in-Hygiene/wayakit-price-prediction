"""Concrete examples for the parsers in ``scraper/utils.py``.

They complement the properties in ``tests/property/test_properties_parsers.py`` with titles
similar to real ones and with the minimal counterexamples of the bugs found (each one as
``xfail(strict=True)``: when pi-dev fixes the bug, the test passes, the strict xfail marks it
as a failure and the mark has to be removed).
"""
import pytest

from tests.conftest import load_module

utils = load_module("scraper/utils.py")

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Dettol Antibacterial Surface Cleaner 500ml", {"quantity": 500.0, "unit": "ml", "normalized": 500.0}),
        ("Fairy Dishwashing Liquid 1.5 L", {"quantity": 1.5, "unit": "L", "normalized": 1500.0}),
        ("Clorox Bleach 3.78 Liters", {"quantity": 3.78, "unit": "L", "normalized": 3780.0}),
        ("Glass cleaner 32 fl oz", {"quantity": 32.0, "unit": "fl oz", "normalized": 32 * 29.5735}),
        ("Car wax 16 FL. OZ", None),  # "FL." with a dot is not supported; documented here
        ("Detergent powder 2 kg", {"quantity": 2.0, "unit": "kg", "normalized": 2000.0}),
        ("Detergent powder 500 g", {"quantity": 500.0, "unit": "g", "normalized": 500.0}),
        ("Microfiber cloth", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_volume_string_titles(title, expected):
    result = utils.parse_volume_string(title)
    if expected is None:
        assert result is None
    else:
        assert result["quantity"] == pytest.approx(expected["quantity"])
        assert result["unit"] == expected["unit"]
        assert result["normalized"] == pytest.approx(expected["normalized"])


@pytest.mark.xfail(
    strict=True,
    reason="BUG: 'Shampoo 1,000 ml' returns quantity=0.0: the regex does not accept a thousands "
    "separator and takes only '000' (finding for pi-dev)",
)
def test_volume_with_thousands_separator():
    assert utils.parse_volume_string("Shampoo 1,000 ml")["quantity"] == 1000.0


@pytest.mark.xfail(
    strict=True,
    reason="BUG: '.5 L' returns quantity=5.0 (5 L, not 0.5 L): the regex requires a digit before "
    "the decimal point (finding for pi-dev)",
)
def test_decimal_volume_without_leading_zero():
    assert utils.parse_volume_string("Bleach .5 L")["quantity"] == 0.5


@pytest.mark.parametrize(
    "title, count",
    [
        ("Sanitizing Wipes 80 wipes", 80),
        ("Dettol 40 Wet Wipes", 40),
        ("Paper towels pack of 6", 6),
        ("Gloves Box of 100", 100),
        ("Sachets 12/box", 12),
        ("Wipes 72 pcs", 72),
        ("Microfiber cloth", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_count_string_titles(title, count):
    result = utils.parse_count_string(title)
    if count is None:
        assert result is None
    else:
        assert result == {"quantity": count, "unit": "units", "normalized": count}


@pytest.mark.parametrize(
    "title, count",
    [("Microfiber 12-Piece Set", 12), ("Cleaning rags 50 - rags", 50), ("Wipes 80-wipes", 80), ("Sponge", None), ("", None)],
)
def test_parse_saco_count_string_titles(title, count):
    result = utils.parse_saco_count_string(title)
    assert (result or {}).get("quantity") == count


@pytest.mark.parametrize(
    "title, quantity, unit",
    [
        ("Hand soap (6 Bottles x 500 ml)", 3000.0, "ml"),
        ("Floor cleaner 4 Pcs x 5 Ltr", 20.0, "L"),
        ("Dish soap 12 x 750 ml", 9000.0, "ml"),
        ("Dish soap 750 ml", 750.0, "ml"),
    ],
)
def test_parse_volume_with_multiplier_titles(title, quantity, unit):
    result = utils.parse_volume_with_multiplier(title)
    assert result["quantity"] == pytest.approx(quantity)
    assert result["unit"] == unit


@pytest.mark.xfail(
    strict=True,
    reason="BUG: '(2 Bottles x 0.00001 ml)' returns quantity=2e-05 but normalized=5.0: the total "
    "is parsed again with f'{total}', which writes '2e-05' and the regex reads '05'. Happens with "
    "totals < 1e-4 or >= 1e16 (unrealistic; minor finding for pi-dev)",
)
def test_multiplier_scientific_notation_keeps_normalization():
    result = utils.parse_volume_with_multiplier("(2 Bottles x 0.00001 ml)")
    assert result["normalized"] == pytest.approx(result["quantity"])


@pytest.mark.xfail(
    strict=True,
    reason="Possible BUG (confirm with Mafer): 'Detergent 3X concentrate 500 ml' returns 1500 ml; "
    "the concentration '3X' is taken as 3 pieces and the price per liter drops to a third "
    "(finding for pi-dev)",
)
def test_multiplier_does_not_confuse_concentration_with_pieces():
    assert utils.parse_volume_with_multiplier("Detergent 3X concentrate 500 ml")["quantity"] == 500.0


@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason="Minor BUG: a number with more than 4300 digits ('9'*4301 + ' wipes') raises ValueError in "
    "int() because of the Python 3.10.7+ limit; the count parsers do not catch it. Unrealistic in "
    "titles, but it breaks the 'never raises' rule (finding for pi-dev)",
)
def test_count_huge_number_does_not_raise():
    utils.parse_count_string("9" * 4301 + " wipes")


def test_aerosense_examples():
    assert utils.extract_aerosense_units("AeroSense refill (6 pcs)") == 6
    assert utils.extract_aerosense_units("AeroSense 3-pack x 2") == 6
    assert utils.extract_aerosense_units("AeroSense refill") is None
