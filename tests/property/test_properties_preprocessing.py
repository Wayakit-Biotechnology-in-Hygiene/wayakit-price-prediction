"""Properties of ``ml_model/1a_preprocess_data.py`` (price per liter and per piece).

``process_volumetric_data`` and ``process_unit_data`` return nothing: they write
``ml_model/competitor_*_processed.csv`` relative to the cwd. Here they run inside a temporary
directory (``in_directory``) and the CSV they leave is read back; nothing is ever written to the repo.

Valid row (C18 of ``docs/reliability-checklist.md``): recognized volume unit (or numeric
quantity in the per-piece case), quantity > 0 and price > 0. The two bugs found here (volumetric
with a negative quantity, bug 8, and price 0 in both paths) were fixed in F7a: the general
properties now generate those cases too, and the old counterexamples stay as ``@example``.
"""
import math

import numpy as np
import pandas as pd
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from tests.conftest import in_directory, load_module

preprocessing = load_module("ml_model/1a_preprocess_data.py")

pytestmark = [
    pytest.mark.property,
    # The production code does dropna(inplace=True) on a slice: pandas warns on every
    # call. The result is correct (df is local); it is silenced so it does not hide other warnings.
    pytest.mark.filterwarnings("ignore::pandas.errors.SettingWithCopyWarning"),
]

LITERS_PER_UNIT = {"ml": 1 / 1000, "l": 1.0, "fl oz": 0.0295735}
# Input spellings 1a reads as liters (written as "l" in the output).
LITER_SPELLINGS = {"litre", "litres", "liter", "liters"}
UNITS = ["ml", "ML", " ml ", "l", "L", " L", "fl oz", "FL OZ", "g", "G", "kg", "litre", "oz", "units"]

quantities = st.one_of(
    st.integers(-100, 100_000).map(float),
    st.floats(min_value=0.001, max_value=1e5),
)
prices = st.one_of(st.floats(min_value=0, max_value=1e5), st.just(float("nan")))


@pytest.fixture(scope="module")
def tmp_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("preprocessing")
    (path / "ml_model").mkdir()
    return path


def _run_volumetric(directory, rows):
    df = pd.DataFrame(rows, columns=["unit_of_measurement", "total_quantity", "price_sar"])
    df["type_of_product"] = "T"
    df["row_id"] = range(len(df))
    with in_directory(directory):
        preprocessing.process_volumetric_data(df.copy())
        output = pd.read_csv("ml_model/competitor_volumetric_processed.csv", float_precision="round_trip")
    return df, output


def _run_unit(directory, rows):
    df = pd.DataFrame(rows, columns=["total_quantity", "price_sar"])
    df["type_of_product"] = "T"
    df["row_id"] = range(len(df))
    with in_directory(directory):
        preprocessing.process_unit_data(df.copy())
        output = pd.read_csv("ml_model/competitor_unit_processed.csv", float_precision="round_trip")
    return df, output


volumetric_rows = st.lists(st.tuples(st.sampled_from(UNITS), quantities, prices), max_size=30)


def _valid_volumetric(input_df):
    unit = input_df["unit_of_measurement"].str.lower().str.strip()
    liters = unit.map(lambda u: LITERS_PER_UNIT.get("l" if u in LITER_SPELLINGS else u, np.nan))
    return input_df[
        liters.notna() & (input_df["price_sar"] > 0) & (input_df["total_quantity"] > 0)
        # a subnormal price can underflow to 0 per liter; 1a drops that row too
        & (input_df["price_sar"] / (input_df["total_quantity"] * liters) > 0)
    ]


@given(rows=volumetric_rows)
def test_volumetric_conversion_to_liters_and_price_per_liter(tmp_dir, rows):
    _, output = _run_volumetric(tmp_dir, rows)
    assert set(output["unit_of_measurement"]) <= set(LITERS_PER_UNIT), "only ml, l and fl oz; never g"
    for _, row in output.iterrows():
        expected = row["total_quantity"] * LITERS_PER_UNIT[row["unit_of_measurement"]]
        assert row["volume_liters"] == pytest.approx(expected, rel=1e-12)
        assert math.isfinite(row["price_per_liter"])
        assert row["price_per_liter"] == pytest.approx(row["price_sar"] / row["volume_liters"], rel=1e-12)


@given(rows=volumetric_rows)
def test_volumetric_volume_and_price_per_liter_positive(tmp_dir, rows):
    _, output = _run_volumetric(tmp_dir, rows)
    assert (output["volume_liters"] > 0).all()
    assert (output["price_per_liter"] > 0).all()


@given(rows=volumetric_rows)
@example(rows=[("ml", -500.0, 1.0)])  # bug 8 (fixed in F7a): used to give price_per_liter -2.0
@example(rows=[("ml", 500.0, 0.0)])   # C18 (fixed in F7a): price 0 used to give 0 SAR per liter
@example(rows=[("litre", 2.0, 30.0), ("kg", 2.0, 30.0)])  # bug 5 (fixed in F7a)
@example(rows=[("l", 4.0, 5e-324)])   # the price per liter underflows to 0: dropped
def test_volumetric_keeps_exactly_the_valid_rows(tmp_dir, rows):
    """The output has exactly the valid rows (volume unit, quantity > 0, price > 0)."""
    input_df, output = _run_volumetric(tmp_dir, rows)
    assert sorted(output["row_id"]) == sorted(_valid_volumetric(input_df)["row_id"])


unit_quantities = st.one_of(
    quantities,
    st.sampled_from(["12", "0", "-3", "abc", "", "1e3", " 7 "]),
)
unit_rows = st.lists(st.tuples(unit_quantities, prices), max_size=30)


def _valid_unit(input_df):
    quantity = pd.to_numeric(input_df["total_quantity"], errors="coerce")
    return input_df[(quantity > 0) & (input_df["price_sar"] > 0) & (input_df["price_sar"] / quantity > 0)]


@given(rows=unit_rows)
def test_unit_positive_quantity_and_price_per_piece(tmp_dir, rows):
    _, output = _run_unit(tmp_dir, rows)
    # An empty CSV is read with object columns; with rows, they are already float.
    quantity = output["total_quantity"].astype(float)
    price_per_piece = output["price_per_item"].astype(float)
    assert (quantity > 0).all()
    assert np.isfinite(price_per_piece).all()
    np.testing.assert_allclose(price_per_piece, output["price_sar"].astype(float) / quantity, rtol=1e-12)


@given(rows=unit_rows)
@example(rows=[(12.0, 0.0)])  # C18 (fixed in F7a): price 0 used to give 0 SAR per piece
@example(rows=[(4.0, 5e-324)])  # the price per piece underflows to 0: dropped
def test_unit_keeps_exactly_the_valid_rows(tmp_dir, rows):
    """The output has exactly the valid rows (numeric quantity > 0, price > 0)."""
    input_df, output = _run_unit(tmp_dir, rows)
    assert sorted(output["row_id"]) == sorted(_valid_unit(input_df)["row_id"])


# --- Case packs (audit 2026-11-25: AeroSense "<size> <container> x N", "N cartons", GoGreen
# Arabic cartons). No false positives on dimensions or on a single carton. -----------------------

NAMES = st.sampled_from(["Cabin Cleaner", "Microfiber Mat", "Leather Conditioner", "Avionics Wipes",
                         "Glass Cleaner Triple Action", "Floor Towel"])
ARABIC_NAMES = st.sampled_from(["مطهر و معقم الاسطح", "مزيل البقع للملابس البيضاء", "منظف الارضيات"])
SIZES = st.one_of(st.integers(1, 5000).map(str), st.decimals("0.1", "99.9", places=1).map(str))
VOLUME_WORDS = st.sampled_from(["ml", "ML", "litre", "liter", "L", "ltr"])
CONTAINERS = st.sampled_from(["spray bottle", "bottle", "can", "jerrycan", "canister", "drum", "pouch"])
LENGTHS = st.sampled_from(["cm", "mm", "m", "in", "inch", "inches", "ft"])
SCENTS = st.sampled_from(["", " - Fresh Lemon", " Linen Breeze"])


@given(name=NAMES, a=SIZES, b=SIZES, length=LENGTHS, size=SIZES, volume=VOLUME_WORDS, container=CONTAINERS,
       with_size=st.booleans())
@example(name="Microfiber Mat", a="40", b="40", length="cm", size="500", volume="ml", container="bottle",
         with_size=True)
def test_dimensions_are_never_a_case_pack(name, a, b, length, size, volume, container, with_size):
    """'40 x 40 cm', and a container followed by 'x 40 cm', are a dimension, never a pack."""
    prefix = f"{name} {size} {volume} {container}" if with_size else name
    for title in (f"{prefix} {a} x {b} {length}", f"{prefix} x {b} {length}", f"{prefix} x {b}{length}"):
        assert preprocessing.case_pack(title) == 1, title
        assert preprocessing.flag_multipacks([title], ["units"], [1.0]) == [False], title
        if with_size:
            assert preprocessing.flag_multipacks([title], ["ml"], [500.0]) == [False], title


@given(name=NAMES, pieces=st.integers(1, 5000), scent=SCENTS, arabic=ARABIC_NAMES, liters=st.integers(1, 30))
def test_a_single_carton_is_not_a_case_pack(name, pieces, scent, arabic, liters):
    """'1 carton (500 pcs)' is one package; an Arabic carton without a count of 2 or more too."""
    english = f"{name} - 1 carton ({pieces} pcs){scent}"
    assert preprocessing.case_pack(english) == 1
    assert preprocessing.flag_multipacks([english], ["units"], [float(pieces)]) == [False]
    for title in (f"كرتون {arabic} 1 حبة ({liters}لتر)", f"كرتون {arabic} {liters} لتر", f"{arabic} {liters} لتر"):
        assert preprocessing.case_pack(title) == 1, title
        assert preprocessing.flag_multipacks([title], ["l"], [float(liters)]) == [False], title


@given(name=NAMES, size=SIZES, volume=VOLUME_WORDS, container=CONTAINERS, n=st.integers(2, 9999), scent=SCENTS,
       arabic=ARABIC_NAMES, liters=st.integers(1, 30))
def test_case_notations_are_flagged(name, size, volume, container, n, scent, arabic, liters):
    """Every '<size> <container> x N', 'N cartons' and Arabic carton with N >= 2 is a multipack."""
    for title, unit in ((f"{name} - {size} {volume} {container} x {n}{scent}", "ml"),
                        (f"{name} - {n} cartons ({n * 500} pcs){scent}", "units"),
                        (f"كرتون {arabic} {n} حبة ({liters}لتر)", "l")):
        assert preprocessing.case_pack(title) == n, title
        assert preprocessing.flag_multipacks([title], [unit], [float(n * 500)]) == [True], title
