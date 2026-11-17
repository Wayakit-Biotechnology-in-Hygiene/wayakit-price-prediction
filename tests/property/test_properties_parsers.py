"""Properties of the size and count parsers in ``scraper/utils.py``.

Each test generates hundreds of inputs with Hypothesis and checks rules that must always hold
(never raises, ranges, allowed units, number round trip, normalization
relationships). If a rule fails because of a real production bug, the code is not fixed here
and the rule is not weakened: the general property excludes only the known counterexample (filtered
strategy) and keeps running, and the bug stays in a minimal ``xfail(strict=True)`` test that only
runs its ``@example`` (``phases=[Phase.explicit]``) and names the bug in ``reason``.
"""
import math

import pytest
from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st

from tests.conftest import load_module

utils = load_module("scraper/utils.py")

pytestmark = pytest.mark.property

# Canonical units returned by parse_volume_string and their normalization factor.
FACTOR_BY_UNIT = {"ml": 1, "L": 1000, "g": 1, "kg": 1000, "fl oz": 29.5735}

# Each written form accepted by the regular expression -> (expected canonical unit, factor).
VOLUME_FORMS = {
    "ml": ("ml", 1),
    "milliliters": ("ml", 1),
    "l": ("L", 1000),
    "ltr": ("L", 1000),
    "liter": ("L", 1000),
    "liters": ("L", 1000),
    "litre": ("L", 1000),
    "g": ("g", 1),
    "grams": ("g", 1),
    "kg": ("kg", 1000),
    "kilograms": ("kg", 1000),
    "oz": ("fl oz", 29.5735),
    "ounce": ("fl oz", 29.5735),
    "fl oz": ("fl oz", 29.5735),
    "floz": ("fl oz", 29.5735),
    "fluid ounce": ("fl oz", 29.5735),
    "fluid ounces": ("fl oz", 29.5735),
}
FORMS_WITH_BUG = {
    "litre": "BUG: '1 litre' returns unit='litre' and normalized=1.0 (not 1000): the liters branch "
    "looks for 'liter' inside the unit and 'litre' does not contain it (finding for pi-dev)",
    "kilograms": "BUG: '2 kilograms' returns unit='g' and normalized=2.0 (not unit='kg', 2000): "
    "'gram' is inside 'kilograms' and the grams branch is evaluated first (finding for pi-dev)",
}

FORMS_WITHOUT_BUG = [f for f in VOLUME_FORMS if f not in FORMS_WITH_BUG]

COUNT_UNITS = ["wipes", "count", "sheets", "sachets", "pack", "pcs", "pieces", "pc"]

# Product text without digits or symbols that would form another pattern; it ends in a space so
# the number stays separated from the previous word.
LETTERS = "abcdefhijkmnqrtuvwyzABCDEFHIJKMNQRTUVWYZ "
prefixes = st.text(alphabet=LETTERS, max_size=25).map(lambda s: (s.strip() + " ") if s.strip() else "")
suffixes = st.sampled_from(["", " bottle", ", refill", " - Wayakit", " (new)", "."])
casings = st.sampled_from([str.lower, str.upper, str.title, lambda s: s])

# Numbers as they appear in a title: integer or decimal with up to 3 digits, no scientific notation.
number_texts = st.one_of(
    st.integers(min_value=0, max_value=1_000_000).map(str),
    st.tuples(st.integers(0, 100_000), st.integers(0, 999)).map(lambda t: f"{t[0]}.{t[1]}"),
)

FRAGMENTS = [
    "ml", "ML", "l", "L", "ltr", "liter", "litre", "liters", "g", "kg", "grams", "kilograms",
    "oz", "fl oz", "floz", "ounce", "fluid ounce", "x", "X", "Pcs", "pack of", "box of", "wipes",
    "count", "-", "/", "(", ")", ",", ".", " ", "  ", "Wayakit", "cleaner", "٣", "５",
]
# "Almost realistic" titles: a mix of numbers, units and junk. Max. ~200 characters
# (a real title does not go beyond that; the thousands-of-digits case is separate).
arbitrary_texts = st.one_of(
    st.text(max_size=200),
    st.lists(
        st.one_of(st.sampled_from(FRAGMENTS), number_texts, st.text(max_size=4)),
        max_size=12,
    ).map(lambda parts: " ".join(parts)[:200]),
)
# Same, but without the word 'litre' (the only known case of a non-canonical unit, see FORMS_WITH_BUG).
texts_without_litre = st.one_of(
    st.text(max_size=200),
    st.lists(
        st.one_of(st.sampled_from([f for f in FRAGMENTS if f != "litre"]), number_texts, st.text(max_size=4)),
        max_size=12,
    ).map(lambda parts: " ".join(parts)[:200]),
).filter(lambda text: "litre" not in text.lower())


def _check_structure(result, quantity_type):
    if result is None:
        return
    assert set(result) == {"quantity", "unit", "normalized"}
    q, n = result["quantity"], result["normalized"]
    assert isinstance(q, quantity_type)
    assert not (isinstance(q, float) and math.isnan(q))
    assert q >= 0
    assert n >= 0


# --- parse_volume_string ----------------------------------------------------------------------

@given(arbitrary_texts)
def test_volume_never_raises_and_returns_valid_structure(text):
    _check_structure(utils.parse_volume_string(text), float)


@given(texts_without_litre)
def test_volume_unit_always_canonical(text):
    result = utils.parse_volume_string(text)
    if result is not None:
        assert result["unit"] in FACTOR_BY_UNIT


@pytest.mark.parametrize("form", FORMS_WITHOUT_BUG)
@given(number=number_texts, prefix=prefixes, suffix=suffixes, casing=casings, space=st.sampled_from(["", " "]))
def test_volume_round_trip_and_normalization(form, number, prefix, suffix, casing, space):
    """'<prefix><q> <unit><suffix>' -> quantity == q, canonical unit and normalized == q * factor."""
    expected_unit, factor = VOLUME_FORMS[form]
    text = f"{prefix}{number}{space}{casing(form)}{suffix}"
    result = utils.parse_volume_string(text)
    assert result is not None, text
    assert result["quantity"] == float(number)
    assert result["unit"] == expected_unit
    assert result["normalized"] == pytest.approx(float(number) * factor, rel=1e-12)


@pytest.mark.xfail(strict=True, reason=FORMS_WITH_BUG["litre"])
@settings(phases=[Phase.explicit])
@given(arbitrary_texts)
@example("Wayakit cleaner 1 litre")
def test_bug_volume_litre_non_canonical_unit(text):
    result = utils.parse_volume_string(text)
    assert result["unit"] == "L" and result["normalized"] == 1000.0, result


@pytest.mark.xfail(strict=True, reason=FORMS_WITH_BUG["kilograms"])
@settings(phases=[Phase.explicit])
@given(number=number_texts)
@example(number="2")
def test_bug_volume_kilograms_read_as_grams(number):
    result = utils.parse_volume_string(f"Powder {number} kilograms")
    assert result["unit"] == "kg" and result["normalized"] == float(number) * 1000, result


@given(number=number_texts, form=st.sampled_from(sorted(VOLUME_FORMS)))
def test_volume_relationships_between_units(number, form):
    """L = 1000 ml, kg = 1000 g, fl oz = 29.5735 ml, on the canonical output."""
    result = utils.parse_volume_string(f"{number} {form}")
    if result is None or result["unit"] not in FACTOR_BY_UNIT:
        return  # the 'litre' form is already covered by test_bug_volume_litre_non_canonical_unit
    assert result["normalized"] == pytest.approx(
        result["quantity"] * FACTOR_BY_UNIT[result["unit"]], rel=1e-12
    )


# --- parse_count_string ------------------------------------------------------------------------

@given(arbitrary_texts)
def test_count_never_raises_and_returns_valid_structure(text):
    result = utils.parse_count_string(text)
    _check_structure(result, int)
    if result is not None:
        assert result["unit"] == "units"
        assert result["normalized"] == result["quantity"]


@given(
    n=st.integers(0, 1_000_000),
    unit=st.sampled_from(COUNT_UNITS),
    wet=st.sampled_from(["", "wet ", "Wet "]),
    prefix=prefixes,
    suffix=suffixes,
    casing=casings,
    space=st.sampled_from(["", " "]),
)
def test_count_round_trip_n_units(n, unit, wet, prefix, suffix, casing, space):
    text = f"{prefix}{n}{space}{wet}{casing(unit)}{suffix}"
    result = utils.parse_count_string(text)
    assert result == {"quantity": n, "unit": "units", "normalized": n}, text


@given(
    n=st.integers(0, 1_000_000),
    container=st.sampled_from(["pack", "box", "Pack", "BOX"]),
    prefix=prefixes,
    suffix=suffixes,
)
def test_count_round_trip_pack_of_n(n, container, prefix, suffix):
    text = f"{prefix}{container} of {n}{suffix}"
    assert utils.parse_count_string(text) == {"quantity": n, "unit": "units", "normalized": n}, text


@given(n=st.integers(0, 1_000_000), container=st.sampled_from(["box", "pack", "count"]), space=st.sampled_from(["", " "]))
def test_count_round_trip_n_per_container(n, container, space):
    text = f"{n}{space}/{space}{container}"
    assert utils.parse_count_string(text)["quantity"] == n


# --- parse_saco_count_string -------------------------------------------------------------------

@given(arbitrary_texts)
def test_saco_never_raises_and_returns_valid_structure(text):
    result = utils.parse_saco_count_string(text)
    _check_structure(result, int)
    if result is not None:
        assert result["unit"] == "units"
        assert result["normalized"] == result["quantity"]


@given(
    n=st.integers(0, 1_000_000),
    word=st.sampled_from(["piece", "wipes", "rags"]),
    casing=casings,
    left=st.sampled_from(["", " "]),
    right=st.sampled_from(["", " "]),
    prefix=prefixes,
)
def test_saco_round_trip(n, word, casing, left, right, prefix):
    text = f"{prefix}{n}{left}-{right}{casing(word)}"
    assert utils.parse_saco_count_string(text) == {"quantity": n, "unit": "units", "normalized": n}


# --- parse_volume_with_multiplier --------------------------------------------------------------

MULTIPLIER_UNITS = {"ml": 1, "milliliters": 1, "l": 1000, "ltr": 1000, "liter": 1000, "liters": 1000}
# Decimals with 1 to 3 digits and >= 0.001: m*q never drops below 1e-4 nor reaches 1e16, which is
# where Python writes floats in scientific notation (that case has its own xfail in unit/).
base_quantities = st.one_of(
    st.integers(0, 100_000).map(str),
    st.tuples(st.integers(0, 10_000), st.integers(1, 999)).map(lambda t: f"{t[0]}.{t[1]:03d}"),
)


@given(arbitrary_texts)
def test_multiplier_never_raises_and_is_consistent(text):
    """If it returns something, normalized == quantity * factor of its unit (besides the structure)."""
    result = utils.parse_volume_with_multiplier(text)
    _check_structure(result, float)
    if result is not None and result["unit"] in FACTOR_BY_UNIT:
        assert result["normalized"] == pytest.approx(
            result["quantity"] * FACTOR_BY_UNIT[result["unit"]], rel=1e-9
        )


@given(
    m=st.integers(1, 48),
    q=base_quantities,
    unit=st.sampled_from(sorted(MULTIPLIER_UNITS)),
    layout=st.sampled_from(["simple", "officesupply", "gogreen"]),
    x=st.sampled_from(["x", "X"]),
)
def test_multiplier_total_is_m_times_q(m, q, unit, layout, x):
    """'6 x 500 ml', '(6 Bottles x 500 ml)' and '6 Pcs x 500 ml' -> quantity 3000 and consistent normalized."""
    template = {
        "simple": "Cleaner {m} {x} {q} {u}",
        "officesupply": "Cleaner ({m} Bottles {x} {q} {u})",
        "gogreen": "Cleaner {m} Pcs {x} {q} {u}",
    }[layout]
    text = template.format(m=m, x=x, q=q, u=unit)
    result = utils.parse_volume_with_multiplier(text)
    assert result is not None, text
    total = m * float(q)
    assert result["quantity"] == pytest.approx(total, rel=1e-12)
    assert result["normalized"] == pytest.approx(total * MULTIPLIER_UNITS[unit], rel=1e-9)


# --- extract_aerosense_units -------------------------------------------------------------------

@given(arbitrary_texts)
def test_aerosense_never_raises(text):
    result = utils.extract_aerosense_units(text)
    assert result is None or (isinstance(result, int) and result >= 0)


@given(n=st.integers(0, 100_000), prefix=prefixes)
def test_aerosense_pcs_in_parentheses(n, prefix):
    assert utils.extract_aerosense_units(f"{prefix}({n} pcs)") == n


@given(a=st.integers(0, 1000), b=st.integers(0, 1000), prefix=prefixes, space=st.sampled_from(["", " "]))
def test_aerosense_pack_times_units_is_product(a, b, prefix, space):
    assert utils.extract_aerosense_units(f"{prefix}{a}-pack{space}x{space}{b}") == a * b
