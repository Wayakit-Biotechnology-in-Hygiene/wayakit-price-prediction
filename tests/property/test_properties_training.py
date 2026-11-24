"""Properties of ``ml_model/2_train_models.py`` (p10–p90 filter and quotation data).

Only functions are imported; ``main()`` does not run (it is guarded by ``__name__``) and the
production logger is replaced with one that writes no files (see ``tests/conftest.py``).
"""
import numpy as np
import pandas as pd
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from tests.conftest import load_module

training = load_module("ml_model/2_train_models.py")
filter_outliers = training.filter_outliers_with_percentiles

pytestmark = pytest.mark.property

METRIC = "price_per_liter"

prices = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)
rows = st.lists(
    st.tuples(st.sampled_from(["A", "B", "C", "D"]), prices),
    max_size=60,
)


def _df(generated_rows):
    df = pd.DataFrame(generated_rows, columns=["type_of_product", METRIC])
    # With 0 rows pandas leaves the column as object; in production it comes from a CSV and is float.
    df[METRIC] = df[METRIC].astype(float)
    df["row_id"] = range(len(df))  # stable identifier to compare rows
    return df


def _quantile(values, p):
    return float(np.quantile(np.asarray(values, dtype=float), p))


@given(rows, st.sampled_from([(0.10, 0.90), (0.0, 1.0), (0.25, 0.75), (0.05, 0.95)]))
def test_output_is_an_exact_subset_of_the_input(generated_rows, percentiles):
    df = _df(generated_rows)
    output = filter_outliers(df, METRIC, *percentiles)
    assert set(output.index) <= set(df.index)
    columns = ["type_of_product", METRIC, "row_id"]
    pd.testing.assert_frame_equal(output[columns], df.loc[output.index, columns])
    assert (output["approved_quote_price"] == 0.0).all()


@given(rows)
def test_does_not_modify_the_input(generated_rows):
    df = _df(generated_rows)
    copy = df.copy()
    filter_outliers(df, METRIC)
    pd.testing.assert_frame_equal(df, copy)


@given(rows, st.sampled_from([(0.10, 0.90), (0.25, 0.75), (0.05, 0.95)]))
def test_equals_filtering_by_quantiles_of_each_group(generated_rows, percentiles):
    """Equivalence with a reference implementation (numpy, linear interpolation).

    Rows within 1e-9 (relative) of a limit are skipped: pandas and numpy interpolate with
    different formulas and can differ in the last bit right at the edge.
    """
    p_low, p_high = percentiles
    df = _df(generated_rows)
    kept = set(filter_outliers(df, METRIC, p_low, p_high)["row_id"])
    for _, group in df.groupby("type_of_product"):
        if len(group) < training.MIN_ROWS_TO_TRIM:  # small types are kept whole (F7a, bug 6)
            assert set(group["row_id"]) <= kept
            continue
        low = _quantile(group[METRIC], p_low)
        high = _quantile(group[METRIC], p_high)
        for row_id, value in zip(group["row_id"], group[METRIC]):
            tolerance = 1e-9 * max(1.0, abs(low), abs(high))
            if low + tolerance < value < high - tolerance:
                assert row_id in kept, (row_id, value, low, high)
            elif value < low - tolerance or value > high + tolerance:
                assert row_id not in kept, (row_id, value, low, high)


@given(rows)
def test_percentiles_0_and_1_keep_everything(generated_rows):
    df = _df(generated_rows)
    assert len(filter_outliers(df, METRIC, 0.0, 1.0)) == len(df)


@given(rows)
def test_filtering_twice_can_only_remove_more(generated_rows):
    """Not idempotent by design: the p10 of the already filtered set is higher (see the example in
    tests/unit/test_training_examples.py). The property that does hold: f(f(x)) ⊆ f(x)."""
    once = filter_outliers(_df(generated_rows), METRIC)
    twice = filter_outliers(once.drop(columns="approved_quote_price"), METRIC)
    assert set(twice["row_id"]) <= set(once["row_id"])


@given(rows)
def test_groups_with_a_single_distinct_value_are_kept_whole(generated_rows):
    df = _df(generated_rows)
    output = filter_outliers(df, METRIC)
    for product_type, group in df.groupby("type_of_product"):
        if group[METRIC].nunique() == 1:
            assert (output["type_of_product"] == product_type).sum() == len(group)


@given(rows)
@example([("A", 1.0), ("A", 2.0)])  # bug 6 (fixed in F7a): limits 1.1 and 1.9 used to leave 0 rows
def test_no_group_disappears(generated_rows):
    df = _df(generated_rows)
    output = filter_outliers(df, METRIC)
    assert set(output["type_of_product"]) == set(df["type_of_product"])


@given(rows)
def test_small_groups_are_kept_whole(generated_rows):
    """F7a: a type with fewer than MIN_ROWS_TO_TRIM rows is not trimmed at all."""
    df = _df(generated_rows)
    output = filter_outliers(df, METRIC)
    for product_type, group in df.groupby("type_of_product"):
        if len(group) < training.MIN_ROWS_TO_TRIM:
            assert sorted(output.loc[output["type_of_product"] == product_type, "row_id"]) == sorted(group["row_id"])


@given(rows, st.lists(st.sampled_from([np.nan]), min_size=1, max_size=5))
def test_rows_without_price_never_pass(generated_rows, nans):
    df = _df(generated_rows + [("A", v) for v in nans])
    output = filter_outliers(df, METRIC)
    assert not output[METRIC].isna().any()


# --- prepare_wayakit_training_data --------------------------------------------------------------

SUBINDUSTRIES = ["Home", "Automotive", "Pets", "Hotels", "Aviation", "Airports", "Restaurants"]


@st.composite
def quotations_and_products(draw):
    ids = draw(st.lists(st.sampled_from([f"FP-{i:03d}" for i in range(8)]), unique=True, max_size=8))
    products = pd.DataFrame(
        {
            "Product_ID": ids,
            "Industry": ["Transportation"] * len(ids),
            "SubIndustry": [draw(st.sampled_from(SUBINDUSTRIES)) for _ in ids],
            "Generic product type": ["Cleaner"] * len(ids),
            "Type_of_product": [f"T-{i}" for i in ids],
            "Volume_Liters": [1.0] * len(ids),
        }
    )
    n = draw(st.integers(0, 20))
    quotations = pd.DataFrame(
        {
            "Product_ID": [draw(st.sampled_from([f"FP-{i:03d}" for i in range(10)])) for _ in range(n)],
            "Description": ["x"] * n,
            "approved_quote_price": [draw(st.floats(0, 1e5)) for _ in range(n)],
        }
    )
    return quotations, products


@given(quotations_and_products())
def test_prepare_quotations_channel_and_company_rules(data):
    quotations, products = data
    expected = int(quotations["Product_ID"].isin(products["Product_ID"]).sum())
    base = training.prepare_wayakit_training_data(quotations.copy(), products.copy())
    # With unique SKUs, each quotation that has a product appears exactly once.
    assert len(base) == expected
    assert (base["company"] == "Wayakit").all()
    is_b2c = base["subindustry"].isin(["Home", "Automotive", "Pets"])
    assert (base.loc[is_b2c, "channel"] == "B2C").all()
    assert (base.loc[~is_b2c, "channel"] == "B2B").all()
    assert {"industry", "subindustry", "generic_product_type", "type_of_product"} <= set(base.columns)
