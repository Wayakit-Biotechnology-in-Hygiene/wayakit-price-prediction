"""Offline E2E of the ML flow: 1a → 1b (full) → 2 → 3 with the frozen F1 data.

Runs the real scripts as subprocesses in a temporary copy of the code (see
`tests/e2e/_helpers.py`) and checks the flow outputs, not just the exit codes:
the scripts swallow exceptions and exit with 0 even when they fail.

Does not run `odoo_api_*` or the scrapers: no AWS, Odoo, Apify or Bedrock.
Skipped if `data/f1_before_2026-11-15/` is missing (folder ignored by git).
"""
from __future__ import annotations

import math

import joblib
import numpy as np
import pandas as pd
import pytest

from tests.e2e import _helpers as h

pytestmark = pytest.mark.e2e

# Report price columns (`generate_predictions` in `ml_model/3_predicted_prices.py`).
PRICE_COLUMNS = ["cost_per_unit", "predicted_price", "predicted_price_per_unit",
                 "porcentaje_de_ganancia", "market_min_found", "market_max_found", "model_confidence",
                 "market_median_found", "market_p75_found"]
FLOOR_FACTOR = 1.30  # `MARGIN_FLOOR` in `3_predicted_prices.py`
MIN_COMPETITORS_FOR_CLAMP = 3  # `MIN_COMPETITORS_FOR_CLAMP` in `3_predicted_prices.py`
# The report rounds to 2 decimals and goes through CSV; 0.01 covers rounding on both sides.
ROUND_TOL = 0.01


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    reason = h.data_missing_reason()
    if reason:
        pytest.skip(reason)
    workdir = tmp_path_factory.mktemp("e2e")
    result = h.prepare_and_run(workdir, label="E2E working tree")
    # Ends up in "Captured stdout" if any later check fails.
    print(result.trace())
    return result


@pytest.fixture(scope="module")
def report(run):
    return pd.read_csv(run.path(h.REPORT))


@pytest.fixture(scope="module")
def to_predict(run):
    return pd.read_csv(run.path(h.TO_PREDICT))


def _fail_rows(df: pd.DataFrame, mask: pd.Series, cols: list[str], title: str) -> str:
    bad = df.loc[mask, cols]
    return f"{title}: {int(mask.sum())} row(s). First ones:\n{bad.head(20).to_string()}"


def test_stages_succeed_and_leave_their_outputs(run):
    assert run.ok, run.trace()
    for stage in h.STAGES:
        for rel in stage.outputs:
            assert run.path(rel).stat().st_size > 0, f"{rel} is empty\n{run.trace()}"
    # log_config.py writes to ../logs relative to the copy: it must stay inside tmp.
    assert (run.repo_dir.parent / "logs").is_dir(), "the logs did not end up in <tmp>/logs"


def test_processed_competitors_are_consistent(run):
    vol = pd.read_csv(run.path(h.VOL_PROCESSED))
    unit = pd.read_csv(run.path(h.UNIT_PROCESSED))
    assert len(vol) > 0 and len(unit) > 0
    assert not vol["unit_of_measurement"].isin(["g", "kg"]).any(), "the volumetric set must not contain masses"
    assert (vol["price_sar"] > 0).all() and (unit["price_sar"] > 0).all(), "price 0 must not reach training (C18)"
    assert vol["is_multipack"].dtype == bool and unit["is_multipack"].dtype == bool
    assert set(vol["unit_of_measurement"]) <= {"ml", "l", "fl oz"}
    for df, col in ((vol, "price_per_liter"), (unit, "price_per_item")):
        values = df[col].to_numpy(dtype=float)
        assert np.isfinite(values).all(), f"{col} contains NaN or infinity"
    assert (vol["volume_liters"] > 0).all()
    assert (unit["total_quantity"] > 0).all()


def test_to_predict_list_follows_the_1b_rule(run, to_predict):
    """1b keeps the catalog products whose `Type_of_product` (stripped) exists among the competitors."""
    vol = pd.read_csv(run.path(h.VOL_PROCESSED))
    unit = pd.read_csv(run.path(h.UNIT_PROCESSED))
    known = set(vol["type_of_product"].unique()) | set(unit["type_of_product"].unique())
    catalog = pd.read_csv(run.path("ml_model/wayakit_products.csv"), encoding="utf-8-sig")
    expected = catalog[catalog["Type_of_product"].str.strip().isin(known)]
    assert len(to_predict) > 0, "no predictable product is left"
    assert to_predict["Product_ID"].astype(str).tolist() == expected["Product_ID"].astype(str).tolist()


def test_report_has_one_row_per_predictable_product(report, to_predict):
    assert len(report) == len(to_predict)
    assert report["Product_ID"].astype(str).tolist() == to_predict["Product_ID"].astype(str).tolist()


def test_report_has_no_nan_prices(report):
    missing = [c for c in PRICE_COLUMNS if c not in report.columns]
    assert not missing, f"columns missing from the report: {missing}"
    nan_mask = report[PRICE_COLUMNS].isna().any(axis=1)
    assert not nan_mask.any(), _fail_rows(report, nan_mask, ["Product_ID", *PRICE_COLUMNS], "prices with NaN")
    assert np.isfinite(report[PRICE_COLUMNS].to_numpy(dtype=float)).all()


def test_final_price_respects_the_margin_floor(report):
    # Since F7a the floor is rounded up to the cent: the price is never below cost × 1.30.
    floor = report["cost_per_unit"] * FLOOR_FACTOR
    bad = report["predicted_price"] < floor - 1e-9 * np.maximum(1.0, floor)
    assert not bad.any(), _fail_rows(report, bad, ["Product_ID", "cost_per_unit", "predicted_price"],
                                     f"final price < cost × {FLOOR_FACTOR}")


def test_clamp_to_market_range(report):
    assert (report["market_min_found"] <= report["market_max_found"] + 1e-9).all()
    few = report["competitors_count"] < MIN_COMPETITORS_FOR_CLAMP
    bad_flag = few & report["clamped_to_market"]
    assert not bad_flag.any(), _fail_rows(report, bad_flag, ["Product_ID", "competitors_count"],
                                          "clamped with fewer than 3 competitors")

    # With >= 3 competitors and a model price > 0, the price stays within the SKU's band
    # [min, p75 x size] (F7a), unless the margin floor lifts it above the ceiling (the floor always
    # wins). The ceiling applies at every size; a volumetric size above the market's size range is
    # never lifted to the minimum, a pack of pieces is (the report does not say which side, so
    # outside rows skip that check).
    # Every ceiling is at most the raw maximum: a quantile of the raw rows, or (since 2026-11-26,
    # E3) a size-adjusted p75 capped at the rows' highest price per unit.
    assert (report["market_p75_found"] <= report["market_max_found"] + ROUND_TOL).all()
    # Only larger rows are adjusted, up, and at most as if 4 times larger (b >= -1): a size-adjusted
    # ceiling is never below the minimum and at most 4 times the maximum.
    adjusted = (report["market_match"] == "size_adjusted") & (report["competitors_count"] > 0)
    assert (report.loc[adjusted, "market_p75_found"] >= report.loc[adjusted, "market_min_found"] - ROUND_TOL).all()
    assert (report.loc[adjusted, "market_p75_found"]
            <= 4 * report.loc[adjusted, "market_max_found"] + ROUND_TOL).all()
    clamped_rows = ~few & (report["predicted_price_per_unit"] > 0)
    assert clamped_rows.any(), "no product went through the clamp; the check would prove nothing"
    floor = report["cost_per_unit"] * FLOOR_FACTOR
    upper = np.maximum(report["market_p75_found"], floor)
    below = (clamped_rows & ~report["volume_outside_market"]
             & (report["predicted_price"] < report["market_min_found"] - ROUND_TOL))
    # The family rule (2026-11-26, D4) may raise a SKU above its ceiling to a smaller sibling's
    # price: test_family_rule_orders_total_prices checks those rows.
    above = clamped_rows & ~report["family_raised"] & (report["predicted_price"] > upper + ROUND_TOL)
    cols = ["Product_ID", "predicted_price", "market_min_found", "market_max_found", "cost_per_unit"]
    assert not below.any(), _fail_rows(report, below, cols, "price below the market minimum")
    assert not above.any(), _fail_rows(report, above, cols, "price above the market maximum and the floor")


def test_family_rule_orders_total_prices(report):
    """2026-11-26 (D4): among SKUs not held from the upload, none costs less in total than a strictly
    smaller SKU of the same subindustry, type and kind; a raised SKU costs exactly the dearest one."""
    assert report["family_raised"].dtype == bool
    sized = report[report["has_size"] & (report["hold_reason"].fillna("") == "")].copy()
    sized["size"] = sized["volume"].str.split(" ").str[0].astype(float)
    sized["kind"] = sized["volume"].str.split(" ").str[1]
    for _, family in sized.groupby(["subindustry", "product_type", "kind"]):
        for _, row in family.iterrows():
            smaller = family.loc[family["size"] < row["size"], "predicted_price"]
            if len(smaller):
                assert row["predicted_price"] >= smaller.max() - 1e-9, row.to_dict()
            if row["family_raised"]:
                assert len(smaller) and row["predicted_price"] == pytest.approx(smaller.max()), row.to_dict()
    held = report["hold_reason"].fillna("") != ""
    assert not (held & report["family_raised"]).any()


def test_confidence_and_counts_in_range(report):
    assert report["model_confidence"].between(0, 100).all()
    assert (report["competitors_count"] >= 0).all()
    assert report["clamped_to_market"].dtype == bool
    assert report["volume_outside_market"].dtype == bool
    assert report["volume_capped"].dtype == bool and report["has_size"].dtype == bool
    assert not (report["volume_capped"] & ~report["volume_outside_market"]).any()
    # F7b: no reference rows in F1, so every band uses all rows; without a band (fewer than 3
    # competitor rows), no ceiling source.
    has_band = report["competitors_count"] >= MIN_COMPETITORS_FOR_CLAMP
    assert (report.loc[has_band, "ceiling_source"] == "all").all()
    assert report.loc[~has_band, "ceiling_source"].fillna("").eq("").all()
    # 2026-11-25: model-only prices (no band) are flagged; the trained elasticity replaces the
    # all-sizes band of volumetric SKUs; packs of pieces keep it.
    assert report["low_confidence"].dtype == bool
    assert (report["low_confidence"] == ~has_band).all()
    volumetric = report["volume"].astype(str).str.endswith(" L")
    sized = report["has_size"]
    assert set(report.loc[volumetric & sized, "market_match"].fillna("")) <= {"size", "size_adjusted", ""}
    assert set(report.loc[~volumetric & sized, "market_match"].fillna("")) <= {"size", "all", ""}
    assert (report.loc[volumetric & sized, "market_match"] == "size_adjusted").any()
    # No previous upload in the temporary copy: nothing is braked.
    assert report["previous_price"].isna().all() and not report["stability_limited"].any()


def test_products_without_a_size_get_only_the_floor(report):
    """No-size parent templates: flagged has_size False, priced at the floor, never clamped; the
    upload skips them (F7a)."""
    no_size = ~report["has_size"]
    assert no_size.any(), "the F1 data has 26 products without a size; the check would prove nothing"
    rows = report[no_size]
    assert (rows["volume"] == "0 units").all() and (rows["competitors_count"] == 0).all()
    floor = rows["cost_per_unit"] * FLOOR_FACTOR
    assert ((rows["predicted_price"] - floor).abs() <= 0.01 + 1e-9).all()


def test_models_saved_and_loadable(run):
    model_dir = run.path(h.MODEL_DIR)
    vol_model = joblib.load(model_dir / "volumetric_model.joblib")
    vol_cols = joblib.load(model_dir / "volumetric_model_columns.joblib")
    unit_model = joblib.load(model_dir / "unit_model.joblib")
    unit_cols = joblib.load(model_dir / "unit_model_columns.joblib")
    assert type(vol_model).__name__ == "RandomForestRegressor"
    assert vol_model.n_features_in_ == len(vol_cols)
    assert unit_model.n_features_in_ == len(unit_cols)
    for name in ("vol_market_stats.joblib", "unit_market_stats.joblib"):
        stats = joblib.load(model_dir / name)
        assert {"type_of_product", "market_min", "market_max", "competitor_count"} <= set(stats.columns)
        assert len(stats) > 0 and not math.isnan(float(stats["market_min"].min()))
    for name in ("vol_market_rows.joblib", "unit_market_rows.joblib"):
        rows = joblib.load(model_dir / name)
        assert rows.columns.tolist() == ["type_of_product", "price_per_unit", "size", "is_reference"]
        # F1 predates F7b: no Odoo retail channel and no scraper origin, so no Saudi reference rows.
        assert rows["is_reference"].dtype == bool and not rows["is_reference"].any()
        assert len(rows) > 0 and (rows["price_per_unit"] > 0).all() and (rows["size"] > 0).all()
    assert (joblib.load(model_dir / "vol_market_rows.joblib")["size"] > 0.005).all(), "5 mL or less is dropped"
    # 2026-11-25: one b per channel plus the pooled one (''), each in 2_train's clipped range.
    elasticity = joblib.load(model_dir / "vol_size_elasticity.joblib")
    assert isinstance(elasticity, dict) and set(elasticity) == {"", "B2B", "B2C"}, elasticity
    assert all(-1.0 <= b <= -0.3 for b in elasticity.values()), elasticity
