"""Property tests of the upload records (`ml_model/odoo_api_price_suggestion.py`).

- Invariant: `len(records) + len(skipped) == len(df)`, records keep the order of the valid rows.
- Equivalence with the base loop (copied below as the reference, `_old_record`): for every row the
  old loop accepted with finite numbers, `row_to_record` gives the same dict, except that empty text
  fields are now False instead of NaN (NaN is not a valid Odoo text value). Rows with NaN/inf numbers,
  which the old loop sent, are now rejected (F2 decision).
- `check_parse_quality` raises iff there are no records or skipped / total > 5 %.
- `history_records` is idempotent on already-converted records and never contains `id` or
  `create_date`.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from tests.conftest import load_module

pytestmark = pytest.mark.property

up = load_module("ml_model/odoo_api_price_suggestion.py")

TEXT = ["product_type", "generic_product_type", "subindustry", "industry", "product_name"]
FLOATS = ["cost_per_unit", "predicted_price", "porcentaje_de_ganancia", "predicted_price_per_unit",
          "model_confidence", "market_min_found", "market_max_found"]


def _old_record(row) -> dict:
    """The base loop body (origin/server-improvement, odoo_api_price_suggestion.py:83-102)."""
    volume_str = str(row['volume']).split(' ')[0]
    volume_float = float(volume_str)
    return {
        'product_id_str': row['Product_ID'],
        'product_type': row['product_type'],
        'generic_product_type': row['generic_product_type'],
        'subindustry': row['subindustry'],
        'industry': row['industry'],
        'volume_units': volume_float,
        'production_cost': float(row['cost_per_unit']),
        'suggested_price': float(row['predicted_price']),
        'profit': float(row['porcentaje_de_ganancia']),
        'product_name': row['product_name'],
        'predicted_price_per_unit': float(row['predicted_price_per_unit']),
        'model_confidence': float(row['model_confidence']),
        'market_min_found': float(row['market_min_found']),
        'market_max_found': float(row['market_max_found']),
        'competitors_count': int(row['competitors_count']),
    }


finite = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)
number = st.one_of(finite, st.sampled_from([math.nan, math.inf, -math.inf]), st.just("n/a"))
text = st.one_of(st.text(min_size=1, max_size=12), st.just(math.nan))
volume = st.one_of(st.tuples(finite, st.sampled_from(["L", "units", "mL"])).map(lambda t: f"{t[0]} {t[1]}"),
                   st.sampled_from(["abc L", "", "nan L", "inf L"]))
row = st.fixed_dictionaries({
    "Product_ID": st.one_of(st.from_regex(r"FP-[A-Z]{3}-[0-9]{5}", fullmatch=True), st.just(math.nan), st.just(" ")),
    "volume": volume,
    "competitors_count": st.one_of(st.integers(0, 50), st.just(2.5), st.just(math.nan)),
    **{c: text for c in TEXT},
    **{c: number for c in FLOATS},
})
COLUMNS = ["Product_ID", "volume", "competitors_count", *TEXT, *FLOATS]
# Only rows the upload accepts (for the equivalence with the base loop, which needs valid rows).
valid_row = st.fixed_dictionaries({
    "Product_ID": st.from_regex(r"FP-[A-Z]{3}-[0-9]{5}", fullmatch=True),
    "volume": st.tuples(finite, st.sampled_from(["L", "units", "mL"])).map(lambda t: f"{t[0]} {t[1]}"),
    "competitors_count": st.integers(0, 50),
    **{c: text for c in TEXT},
    **{c: finite for c in FLOATS},
})


def _is_valid(r) -> bool:
    pid = r["Product_ID"]
    if not isinstance(pid, str) or not pid.strip():
        return False
    try:
        v = float(str(r["volume"]).split(" ")[0])
        nums = [float(r[c]) for c in FLOATS] + [float(r["competitors_count"])]
    except ValueError:
        return False
    return math.isfinite(v) and all(math.isfinite(x) for x in nums) and float(r["competitors_count"]).is_integer()


@given(st.lists(row, max_size=15))
def test_records_plus_skipped_is_every_row_in_order(rows):
    df = pd.DataFrame(rows, columns=COLUMNS)
    records, skipped = up.build_records(df)
    assert len(records) + len(skipped) == len(df)
    valid = [r for r in rows if _is_valid(r)]
    assert [rec["product_id_str"] for rec in records] == [str(r["Product_ID"]) for r in valid]
    assert [s[0] for s in skipped] == [i + 1 for i, r in enumerate(rows) if not _is_valid(r)]


@given(valid_row)
@example({"Product_ID": "FP-AVI-00100", "volume": "0.7 L", "competitors_count": 0, "product_type": "A1-Leather cleaner",
          "generic_product_type": "Leather and vinyl cleaner", "subindustry": "Aviation", "industry": "Transportation",
          "product_name": "Fake leather cleaner [700 mL]", "cost_per_unit": 11.486999999999998,
          "predicted_price": 39.14, "porcentaje_de_ganancia": 240.73, "predicted_price_per_unit": 55.91,
          "model_confidence": 0.0, "market_min_found": 0.0, "market_max_found": 0.0})
def test_valid_rows_match_the_base_loop(r):
    series = pd.Series(r)
    old = _old_record(series)
    for c in TEXT:
        if isinstance(old[c], float) and math.isnan(old[c]):
            old[c] = False  # intended change: NaN text → False
    assert up.row_to_record(series) == old


@given(st.integers(0, 200), st.integers(0, 200))
def test_parse_quality_threshold(n_records, n_skipped):
    records = [{}] * n_records
    skipped = [(1, "x", "bad")] * n_skipped
    should_fail = n_records == 0 or n_skipped / (n_records + n_skipped) > 0.05
    if should_fail:
        with pytest.raises(up.UploadError):
            up.check_parse_quality(records, skipped)
    else:
        up.check_parse_quality(records, skipped)


@given(st.lists(st.fixed_dictionaries({"id": st.integers(1, 10**6)}, optional={
    "create_date": st.text(max_size=19), "suggested_price": finite, "product_id_str": st.text(max_size=8)})))
def test_history_records_drop_id_and_rename_create_date(old):
    hist = up.history_records(old)
    assert len(hist) == len(old)
    assert all("id" not in h and "create_date" not in h for h in hist)
    assert [h.get("original_create_date") for h in hist] == [o.get("create_date") for o in old]
    assert up.history_records(hist) == hist  # idempotent once converted
