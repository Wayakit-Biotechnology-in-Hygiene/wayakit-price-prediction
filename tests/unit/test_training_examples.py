"""Concrete examples for ``ml_model/2_train_models.py``: p10–p90 filter, quotations, identical
variants and the price per liter vs size elasticity."""
import joblib
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


def test_group_of_two_prices_does_not_disappear():
    """Bug 6 (fixed in F7a): limits 1.1 and 1.9 used to leave the type with 0 rows."""
    df = pd.DataFrame({"type_of_product": ["A", "A"], "price_per_liter": [1.0, 2.0]})
    assert filter_outliers(df, "price_per_liter")["price_per_liter"].tolist() == [1.0, 2.0]


def test_groups_of_three_or_more_are_still_trimmed():
    """From MIN_ROWS_TO_TRIM (3) rows p10-p90 applies as before: it always keeps the median row."""
    df = pd.DataFrame({"type_of_product": ["A"] * 3 + ["B"] * 4,
                       "price_per_liter": [1.0, 2.0, 1000.0, 1.0, 2.0, 3.0, 4.0]})
    output = filter_outliers(df, "price_per_liter")
    assert output.loc[output["type_of_product"] == "A", "price_per_liter"].tolist() == [2.0]
    assert output.loc[output["type_of_product"] == "B", "price_per_liter"].tolist() == [2.0, 3.0]


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


def test_each_quotation_appears_only_once():
    """Bug 7 (fixed in F7a): a SKU repeated in the catalog used to duplicate its quotes."""
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


def test_repeated_sku_keeps_the_row_with_a_size():
    """Bug 7: of two catalog rows of one SKU, the one with a size joins the quotes, even if it is
    not the first (otherwise the quote would fall out of both training sets)."""
    quotations = pd.DataFrame({"Product_ID": ["P1"], "approved_quote_price": [10.0]})
    products = _products(["P1", "P1"], ["Home", "Home"]).assign(Volume_Liters=[0.0, 1.0], Pack_quantity_Units=[0, 0])
    base = training.prepare_wayakit_training_data(quotations, products)
    assert base["Volume_Liters"].tolist() == [1.0]


def test_drop_implausible_sizes_removes_5_ml_or_less():
    df = pd.DataFrame({"type_of_product": ["A"] * 4, "volume_liters": [0.001, 0.005, 0.0051, 1.0],
                       "price_per_liter": [59980.0, 2000.0, 1000.0, 10.0]})
    assert training.drop_implausible_sizes(df)["volume_liters"].tolist() == [0.0051, 1.0]


def test_market_rows_leave_multipacks_out_and_stats_summarize_them():
    df = pd.DataFrame({"type_of_product": ["A", "A", "A", "B"], "price_per_liter": [10.0, 20.0, 5.0, 7.0],
                       "volume_liters": [0.5, 1.0, 2.0, 4.0], "is_multipack": [False, False, True, False]})
    rows = training.build_market_rows(df, "price_per_liter", "volume_liters")
    assert rows.columns.tolist() == training.MARKET_ROWS_COLUMNS
    assert rows.values.tolist() == [["A", 10.0, 0.5, False], ["A", 20.0, 1.0, False], ["B", 7.0, 4.0, False]]
    stats = training.market_stats(rows).set_index("type_of_product")
    assert stats.loc["A"].tolist() == [10.0, 20.0, 2, 0.5, 1.0]


def test_market_rows_without_multipack_column_keep_every_row():
    """A processed file from before F7a has no is_multipack column: every row is a single product."""
    df = pd.DataFrame({"type_of_product": ["A"], "price_per_item": [1.5], "total_quantity": [20.0]})
    rows = training.build_market_rows(df, "price_per_item", "total_quantity")
    assert rows.values.tolist() == [["A", 1.5, 20.0, False]]


def test_saudi_reference_rows_are_odoo_retail_or_local_origin():
    """F7b: only a physical-store Odoo price or a scraped 'local' origin is a Saudi reference."""
    df = pd.DataFrame({
        "type_of_product": ["A"] * 6, "price_per_liter": [1.0] * 6, "volume_liters": [1.0] * 6,
        "source_channel": ["retail", " RETAIL ", "ecommerce", "b2b", "", None],
        "origin": ["unknown", "unknown", "unknown", "unknown", "local", "imported"],
    })
    assert training.is_saudi_reference(df).tolist() == [True, True, False, False, True, False]
    rows = training.build_market_rows(df, "price_per_liter", "volume_liters")
    assert rows["is_reference"].tolist() == [True, True, False, False, True, False]
    assert not training.is_saudi_reference(df.drop(columns=["source_channel", "origin"])).any()


# --- Identical variants count once (Mafer, 2026-11-25) -----------------------------------------
_SCENTS = ["Floor cleaner lemon", "Floor cleaner rose", "Floor cleaner oud", "Floor cleaner musk",
           "Floor cleaner lavender"]


def _variants(**columns):
    """Identical variants of one 3 L floor cleaner; as many rows as the columns given (5 by default)."""
    n = len(next(iter(columns.values()))) if columns else 5
    base = {"type_of_product": ["E2"] * n, "company": ["GoGreen"] * n, "volume_liters": [3.0] * n,
            "price_sar": [45.0] * n, "price_per_liter": [15.0] * n, "product": _SCENTS[:n]}
    base.update(columns)
    return pd.DataFrame(base)


def test_five_scents_of_one_product_become_one_row():
    """GG-02 / F7: five scents of a 3 L floor cleaner at one price were five market rows."""
    output = training.collapse_identical_variants(_variants(), "volume_liters", "volumetric")
    assert output["product"].tolist() == ["Floor cleaner lemon"]


def test_variants_differing_in_type_company_size_or_price_are_kept():
    df = _variants(type_of_product=["E2", "E6", "E2", "E2", "E2"],
                   company=["GoGreen", "GoGreen", "Other", "GoGreen", "GoGreen"],
                   volume_liters=[3.0, 3.0, 3.0, 5.0, 3.0], price_sar=[45.0, 45.0, 45.0, 45.0, 46.0])
    output = training.collapse_identical_variants(df, "volume_liters", "volumetric")
    assert output.index.tolist() == [0, 1, 2, 3, 4]


def test_company_is_compared_trimmed_and_case_insensitive_and_prices_to_the_cent():
    df = _variants(company=["GoGreen", " gogreen ", "GOGREEN", None, None], price_sar=[45.0, 45.001, 45.0, 45.0, 45.0])
    output = training.collapse_identical_variants(df, "volume_liters", "volumetric")
    assert output.index.tolist() == [0, 3, 4]  # brand-less rows are never collapsed


@pytest.mark.parametrize("placeholder", ["Brand not found", "Company not found", "generic", "", " Unknown ",
                                         "N/A", "na", "NONE", "nan", None, np.nan])
def test_rows_without_a_known_brand_are_never_collapsed(placeholder):
    """Without a brand, the same size and price is not evidence of the same product."""
    df = _variants(company=[placeholder] * 3, price_sar=[45.0, 45.0, 45.0])
    output = training.collapse_identical_variants(df, "volume_liters", "volumetric")
    assert output.index.tolist() == [0, 1, 2]


def test_a_known_brand_that_contains_a_placeholder_word_still_collapses():
    """Only a company that IS a placeholder is exempt, not one that merely contains the word."""
    df = _variants(company=["Generic Clean Co"] * 3, price_sar=[45.0, 45.0, 45.0])
    output = training.collapse_identical_variants(df, "volume_liters", "volumetric")
    assert output.index.tolist() == [0]


def test_a_multipack_and_a_single_bottle_of_the_same_total_never_collapse():
    df = _variants(company=["GoGreen"] * 2, price_sar=[45.0, 45.0])
    df["is_multipack"] = [True, False]
    output = training.collapse_identical_variants(df, "volume_liters", "volumetric")
    assert output.index.tolist() == [0, 1]


def test_identical_multipacks_still_collapse_and_a_missing_flag_is_a_single_bottle():
    df = _variants(company=["GoGreen"] * 4, price_sar=[45.0] * 4)
    df["is_multipack"] = [True, True, False, None]
    output = training.collapse_identical_variants(df, "volume_liters", "volumetric")
    assert output.index.tolist() == [0, 2]


def test_a_saudi_reference_row_is_the_one_kept_and_the_order_stays():
    df = _variants(origin=["unknown", "unknown", "local", "unknown", "unknown"])
    df = pd.concat([pd.DataFrame({"type_of_product": ["A"], "company": ["X"], "volume_liters": [1.0],
                                  "price_sar": [9.0], "price_per_liter": [9.0], "product": ["first"],
                                  "origin": ["unknown"]}), df], ignore_index=True)
    output = training.collapse_identical_variants(df, "volume_liters", "volumetric")
    assert output["product"].tolist() == ["first", "Floor cleaner oud"]
    assert training.is_saudi_reference(output).tolist() == [False, True]


def test_units_variants_use_the_piece_count():
    df = pd.DataFrame({"type_of_product": ["W"] * 3, "company": ["B"] * 3, "total_quantity": [80.0, 80.0, 40.0],
                       "price_sar": [20.0, 20.0, 20.0], "price_per_item": [0.25, 0.25, 0.5]})
    assert training.collapse_identical_variants(df, "total_quantity", "units").index.tolist() == [0, 2]


def test_collapse_of_an_empty_frame_is_empty():
    empty = _variants().iloc[:0]
    assert training.collapse_identical_variants(empty, "volume_liters", "volumetric").empty


# --- Price per liter vs size elasticity (Mafer, 2026-11-25) ------------------------------------
def _market(rows):
    return pd.DataFrame(rows, columns=training.MARKET_ROWS_COLUMNS)


def _power_law(product_type, level, b, sizes):
    return [(product_type, level * s ** b, s, False) for s in sizes]


def test_elasticity_of_an_exact_power_law_ignores_the_level_of_each_type():
    """Within-type demeaning: a type 10 times dearer per liter does not bias the slope."""
    rows = _power_law("A", 10.0, -0.7, [0.5, 1.0, 4.0, 20.0]) + _power_law("B", 100.0, -0.7, [0.75, 5.0, 20.0])
    b, raw, types_used, rows_used = training.estimate_size_elasticity(_market(rows))
    assert raw == pytest.approx(-0.7) and b == pytest.approx(-0.7)
    assert (types_used, rows_used) == (2, 7)


@pytest.mark.parametrize("true_b, expected", [(-2.0, -1.0), (0.2, -0.3), (-0.45, -0.45)])
def test_elasticity_is_clipped_to_the_plausible_range(true_b, expected):
    b, raw, _, _ = training.estimate_size_elasticity(_market(_power_law("A", 10.0, true_b, [0.5, 1.0, 5.0])))
    assert raw == pytest.approx(true_b) and b == pytest.approx(expected)


def test_types_with_fewer_than_3_rows_or_one_size_do_not_count():
    rows = (_power_law("A", 10.0, -0.9, [0.5, 5.0])                       # 2 rows
            + [("B", p, 1.0, False) for p in (5.0, 9.0, 30.0)]            # one size
            + _power_law("C", 10.0, -0.5, [0.5, 1.0, 4.0]))
    b, raw, types_used, rows_used = training.estimate_size_elasticity(_market(rows))
    assert raw == pytest.approx(-0.5) and (types_used, rows_used) == (1, 3)


def test_elasticity_falls_back_to_the_default_when_it_cannot_be_measured():
    for rows in ([], [("A", 10.0, 1.0, False)] * 4, _power_law("A", 10.0, -0.7, [1.0, 2.0])):
        assert training.estimate_size_elasticity(_market(rows)) == (training.ELASTICITY_DEFAULT, None, 0, 0)


def test_size_curve_rows_leave_multipacks_out_and_keep_every_other_row():
    """The elasticity is measured on single products (a case total is not a shelf size), before
    the trim, so the input rows are all there except the multipacks."""
    df = pd.DataFrame({"type_of_product": ["A", "A", "A"], "price_per_liter": [30.0, 10.0, 5.0],
                       "volume_liters": [0.5, 5.0, 20.0], "is_multipack": [False, False, True],
                       "origin": ["local", "unknown", "unknown"]})
    rows = training.size_curve_rows(df)
    assert rows.columns.tolist() == training.MARKET_ROWS_COLUMNS
    assert rows.values.tolist() == [["A", 30.0, 0.5, True], ["A", 10.0, 5.0, False]]
    assert len(training.size_curve_rows(df.drop(columns="is_multipack"))) == 3


def _vol_rows(channel, product_type, level, b, sizes, **extra):
    """Volumetric competitor rows (as main passes them to save_size_elasticity) on an exact power law."""
    df = pd.DataFrame({"type_of_product": product_type, "channel": channel, "volume_liters": sizes,
                       "price_per_liter": [level * s ** b for s in sizes]})
    for name, value in extra.items():
        df[name] = value
    return df


def test_save_size_elasticity_writes_one_b_per_channel_and_the_pooled_one(tmp_path):
    """Bulk B2B prices fall less with size than retail ones: each channel gets its own b."""
    df = pd.concat([_vol_rows("B2B", "A", 10.0, -0.5, [1.0, 5.0, 20.0]),
                    _vol_rows("B2C", "B", 30.0, -0.9, [0.5, 1.0, 4.0])], ignore_index=True)
    pooled, _, _, _ = training.estimate_size_elasticity(training.size_curve_rows(df))
    elasticities = training.save_size_elasticity(df, str(tmp_path))
    assert set(elasticities) == {"", "B2B", "B2C"}
    assert elasticities["B2B"] == pytest.approx(-0.5) and elasticities["B2C"] == pytest.approx(-0.9)
    assert elasticities[""] == pytest.approx(pooled) and -0.9 < pooled < -0.5
    assert joblib.load(tmp_path / training.ELASTICITY_FILE) == elasticities


def test_a_channel_that_cannot_be_estimated_gets_the_pooled_b(tmp_path):
    """B2C has one size only: no slope of its own, so it uses the pooled one (here the B2B rows')."""
    df = pd.concat([_vol_rows("B2B", "A", 10.0, -0.5, [1.0, 5.0, 20.0]),
                    _vol_rows(" B2C ", "B", 30.0, -0.9, [1.0, 1.0, 1.0])], ignore_index=True)
    elasticities = training.save_size_elasticity(df, str(tmp_path))
    assert set(elasticities) == {"", "B2B", "B2C"}  # the channel is compared trimmed
    assert elasticities["B2C"] == elasticities[""] == pytest.approx(-0.5)
    assert joblib.load(tmp_path / training.ELASTICITY_FILE) == elasticities


def test_without_any_estimate_every_channel_gets_the_default(tmp_path):
    df = _vol_rows("B2B", "A", 10.0, -0.5, [1.0, 5.0])  # 2 rows: not enough for a slope
    elasticities = training.save_size_elasticity(df, str(tmp_path))
    assert elasticities == {"": training.ELASTICITY_DEFAULT, "B2B": training.ELASTICITY_DEFAULT}


def test_save_size_elasticity_leaves_multipacks_out_and_works_without_a_channel_column(tmp_path):
    """A case total is not a shelf size (size_curve_rows); without `channel` only the pooled b."""
    df = _vol_rows("B2C", "A", 10.0, -0.8, [0.5, 1.0, 4.0, 24.0], is_multipack=[False, False, False, True])
    df.loc[3, "price_per_liter"] = 100.0  # a multipack priced off the curve must not move b
    elasticities = training.save_size_elasticity(df.drop(columns="channel"), str(tmp_path))
    assert elasticities == {"": pytest.approx(-0.8)}
    assert joblib.load(tmp_path / training.ELASTICITY_FILE) == elasticities
