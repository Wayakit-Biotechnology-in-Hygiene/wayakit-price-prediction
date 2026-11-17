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

# Report price columns (`ml_model/3_predicted_prices.py:139-152`).
PRICE_COLUMNS = ["cost_per_unit", "predicted_price", "predicted_price_per_unit",
                 "porcentaje_de_ganancia", "market_min_found", "market_max_found", "model_confidence"]
FLOOR_FACTOR = 1.30  # `3_predicted_prices.py:135`
MIN_COMPETITORS_FOR_CLAMP = 3  # `3_predicted_prices.py:129`
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
    assert not vol["unit_of_measurement"].isin(["g"]).any(), "the volumetric set must not contain grams"
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
    # predicted_price = round(max(model, cost × 1.30), 2), so it never drops by more than half a cent.
    floor = report["cost_per_unit"] * FLOOR_FACTOR
    bad = report["predicted_price"] < floor - 0.005 - 1e-9
    assert not bad.any(), _fail_rows(report, bad, ["Product_ID", "cost_per_unit", "predicted_price"],
                                     f"final price < cost × {FLOOR_FACTOR}")


def test_clamp_to_market_range(report):
    assert (report["market_min_found"] <= report["market_max_found"] + 1e-9).all()
    few = report["competitors_count"] < MIN_COMPETITORS_FOR_CLAMP
    bad_flag = few & report["clamped_to_market"]
    assert not bad_flag.any(), _fail_rows(report, bad_flag, ["Product_ID", "competitors_count"],
                                          "clamped with fewer than 3 competitors")

    # With >= 3 competitors and a model price > 0, the price stays within the market [min, max],
    # unless the margin floor lifts it above the maximum (the floor always wins).
    clamped_rows = ~few & (report["predicted_price_per_unit"] > 0)
    assert clamped_rows.any(), "no product went through the clamp; the check would prove nothing"
    floor = report["cost_per_unit"] * FLOOR_FACTOR
    upper = np.maximum(report["market_max_found"], floor)
    below = clamped_rows & (report["predicted_price"] < report["market_min_found"] - ROUND_TOL)
    above = clamped_rows & (report["predicted_price"] > upper + ROUND_TOL)
    cols = ["Product_ID", "predicted_price", "market_min_found", "market_max_found", "cost_per_unit"]
    assert not below.any(), _fail_rows(report, below, cols, "price below the market minimum")
    assert not above.any(), _fail_rows(report, above, cols, "price above the market maximum and the floor")


def test_confidence_and_counts_in_range(report):
    assert report["model_confidence"].between(0, 100).all()
    assert (report["competitors_count"] >= 0).all()
    assert report["clamped_to_market"].dtype == bool
    assert report["volume_outside_market"].dtype == bool


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
