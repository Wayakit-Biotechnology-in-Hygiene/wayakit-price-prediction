"""Concrete examples for ``ml_model/2_train_models.py``: p10–p90 filter and quotations."""
import numpy as np
import pandas as pd
import pytest

from tests.conftest import load_module

training = load_module("ml_model/2_train_models.py")
filter_outliers = training.filter_outliers_with_percentiles

pytestmark = pytest.mark.unit


def test_filter_removes_extremes_per_group():
    df = pd.DataFrame(
        {
            "type_of_product": ["A"] * 11 + ["B"],
            "price_per_liter": [float(v) for v in range(11)] + [99.0],
        }
    )
    output = filter_outliers(df, "price_per_liter")
    # Group A (0..10): p10 = 1 and p90 = 9 -> 1..9 remain. Group B (1 row) is kept.
    assert output.loc[output["type_of_product"] == "A", "price_per_liter"].tolist() == [float(v) for v in range(1, 10)]
    assert output.loc[output["type_of_product"] == "B", "price_per_liter"].tolist() == [99.0]
    assert "approved_quote_price" in output and (output["approved_quote_price"] == 0.0).all()


def test_filter_is_not_idempotent_by_design():
    """Documents the behavior: applying the filter twice trims the extremes again."""
    df = pd.DataFrame({"type_of_product": ["A"] * 20, "price_per_liter": [float(v) for v in range(1, 21)]})
    once = filter_outliers(df, "price_per_liter")
    twice = filter_outliers(once.drop(columns="approved_quote_price"), "price_per_liter")
    assert len(twice) < len(once)


def test_filter_drops_rows_without_product_type():
    """Documents the behavior: groupby ignores an empty type_of_product, its limits become NaN
    and those rows are lost without warning (there are none in the F1 data)."""
    df = pd.DataFrame({"type_of_product": ["A", None], "price_per_liter": [5.0, 5.0]})
    output = filter_outliers(df, "price_per_liter")
    assert output["type_of_product"].tolist() == ["A"]


@pytest.mark.xfail(
    strict=True,
    reason="BUG: a type_of_product with exactly 2 rows of different prices ends up with no rows. Counterexample: "
    "'A' with [1.0, 2.0] -> limits 1.1 and 1.9 -> 0 rows (finding for pi-dev)",
)
def test_group_of_two_prices_does_not_disappear():
    df = pd.DataFrame({"type_of_product": ["A", "A"], "price_per_liter": [1.0, 2.0]})
    assert len(filter_outliers(df, "price_per_liter")) > 0


def _products(ids, subindustries):
    return pd.DataFrame(
        {
            "Product_ID": ids,
            "Industry": ["Transportation"] * len(ids),
            "SubIndustry": subindustries,
            "Generic product type": ["Cleaner"] * len(ids),
            "Type_of_product": ["T"] * len(ids),
        }
    )


def test_prepare_quotations_channel_and_columns():
    quotations = pd.DataFrame(
        {"Product_ID ": ["P1", "P2", "P3", "PX"], "approved_quote_price": [10.0, 20.0, 30.0, 40.0]}
    )
    products = _products(["P1", "P2", "P3"], ["Home", "Hotels", "Pets"])
    base = training.prepare_wayakit_training_data(quotations, products)
    assert base.sort_values("Product_ID")["channel"].tolist() == ["B2C", "B2B", "B2C"]
    assert (base["company"] == "Wayakit").all()
    assert len(base) == 3  # PX has no product: the join is inner


def test_prepare_quotations_channel_is_case_sensitive():
    """Documents the behavior: lowercase 'home' falls into B2B (same trap as
    'Land transportation' in the scraper). In the F1 data they are all capitalized."""
    quotations = pd.DataFrame({"Product_ID": ["P1"], "approved_quote_price": [10.0]})
    base = training.prepare_wayakit_training_data(quotations, _products(["P1"], ["home"]))
    assert base["channel"].tolist() == ["B2B"]


@pytest.mark.xfail(
    strict=True,
    reason="BUG: if a SKU repeats in wayakit_products_to_predict_odoo.csv, the merge duplicates its "
    "quotations and they weigh double in training. Counterexample: 1 quotation for 'P1' and 'P1' "
    "twice in products -> 2 rows. The F1 data has 4 repeated SKUs and 162 quotations "
    "give 163 rows (finding for pi-dev)",
)
def test_each_quotation_appears_only_once():
    quotations = pd.DataFrame({"Product_ID": ["P1"], "approved_quote_price": [10.0]})
    products = _products(["P1", "P1"], ["Home", "Home"])
    assert len(training.prepare_wayakit_training_data(quotations, products)) == 1


def test_train_and_evaluate_is_deterministic():
    """random_state=42: same data -> same trees (basis of the differential tests).

    Note: with n_jobs=-1, ``predict`` sums the trees in threads and the order of the sum changes
    between calls, so the prediction can differ in the last bit (~1e-15) even with the same
    model. Prediction comparisons must use a tolerance (or n_jobs=1).
    """
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"volume_liters": rng.uniform(0.1, 20, 60), "t_B": rng.integers(0, 2, 60)})
    y = pd.Series(3 + 2 * X["t_B"] + rng.normal(0, 0.1, 60))
    a = training.train_and_evaluate(X, y, "test")
    b = training.train_and_evaluate(X, y, "test")
    for tree_a, tree_b in zip(a.estimators_, b.estimators_):
        np.testing.assert_array_equal(tree_a.tree_.threshold, tree_b.tree_.threshold)
        np.testing.assert_array_equal(tree_a.tree_.value, tree_b.tree_.value)
    np.testing.assert_allclose(a.predict(X), b.predict(X), rtol=1e-12, atol=0)
    a.set_params(n_jobs=1)
    b.set_params(n_jobs=1)
    np.testing.assert_array_equal(a.predict(X), b.predict(X))
