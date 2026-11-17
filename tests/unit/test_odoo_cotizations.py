"""Unit tests of `ml_model/odoo_api_cotizations.py` (C12: rolling 12-month sales window).

A fake XML-RPC `models` object stands in for Odoo; `main()` gets the session and the date
injected, so nothing reads secrets or opens a connection. Outputs go to `tmp_path`.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import pandas as pd
import pytest

from pi_odoo import OdooSession
from tests.conftest import REPO_ROOT, load_module

pytestmark = pytest.mark.unit

cot = load_module("ml_model/odoo_api_cotizations.py")


class FakeModels:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def execute_kw(self, db, uid, token, model, method, args, kwargs=None):
        self.calls.append((model, method, args, kwargs))
        return list(self.records)


def _session(records):
    models = FakeModels(records)
    return OdooSession(models=models, db="FAKE-db", uid=1, api_token="FAKE-token"), models


LINES = [
    {"id": 1, "name": "[FP-001] Fake cleaner 1 L", "product_uom_qty": 4.0, "price_subtotal": 100.0},
    {"id": 2, "name": "[FP-002]   Fake wipes x80", "product_uom_qty": 2.0, "price_subtotal": 30.0},
]


# --- sales_window -------------------------------------------------------------------------------
@pytest.mark.parametrize("today, expected", [
    (date(2026, 11, 17), ("2025-11-17 00:00:00", "2026-11-17 23:59:59")),
    (date(2027, 1, 15), ("2026-01-15 00:00:00", "2027-01-15 23:59:59")),   # crosses a year
    (date(2027, 12, 31), ("2026-12-31 00:00:00", "2027-12-31 23:59:59")),
    (date(2028, 2, 29), ("2027-02-28 00:00:00", "2028-02-29 23:59:59")),   # leap day: clamped
    (date(2029, 3, 31), ("2028-03-31 00:00:00", "2029-03-31 23:59:59")),
])
def test_sales_window_is_the_last_12_months_including_both_ends(today, expected):
    assert cot.sales_window(today) == expected


def test_sales_window_with_a_datetime_uses_only_the_date():
    assert cot.sales_window(datetime(2026, 11, 17, 23, 59, 58)) == cot.sales_window(date(2026, 11, 17))
    assert cot.sales_window(datetime(2026, 11, 17, 0, 0, 1)) == cot.sales_window(date(2026, 11, 17))


def test_the_2026_run_includes_2026_sales():
    """The bug C12: with the year fixed to 2025, a 2026 sale never reached training."""
    start, end = cot.sales_window(date(2026, 11, 13))
    assert start <= "2026-06-30 12:00:00" <= end
    assert not (start <= "2025-06-30 12:00:00" <= end)


def test_no_year_is_hard_coded_in_the_query_code():
    """The domain must come from `sales_window`, never from literal dates (C12)."""
    source = (REPO_ROOT / "ml_model/odoo_api_cotizations.py").read_text(encoding="utf-8")
    code_lines = [ln for ln in source.splitlines() if not ln.lstrip().startswith("#")]
    literal_dates = [ln.strip() for ln in code_lines if re.search(r"['\"]20\d\d-\d\d-\d\d", ln)]
    assert literal_dates == [], literal_dates


# --- query and conversion -----------------------------------------------------------------------
def test_fetch_sends_the_window_in_the_domain_and_asks_only_the_needed_fields():
    session, models = _session(LINES)
    start, end = cot.sales_window(date(2026, 11, 17))
    assert cot.fetch_sale_lines(session, start, end) == LINES
    [(model, method, args, kwargs)] = models.calls
    assert (model, method) == ("sale.order.line", "search_read")
    domain = args[0]
    assert ["order_id.date_order", ">=", start] in domain
    assert ["order_id.date_order", "<=", end] in domain
    assert ["order_id.state", "=", "sale"] in domain
    assert ["name", "like", "[FP-%"] in domain
    assert kwargs == {"fields": ["name", "product_uom_qty", "price_subtotal"]}


def test_records_to_dataframe_extracts_id_description_and_unit_price():
    df = cot.records_to_dataframe(LINES)
    assert list(df.columns) == ["Product_ID", "Description", "approved_quote_price"]
    assert df["Product_ID"].tolist() == ["FP-001", "FP-002"]
    assert df["Description"].tolist() == ["Fake cleaner 1 L", "Fake wipes x80"]
    assert df["approved_quote_price"].tolist() == [25.0, 15.0]


# --- main ---------------------------------------------------------------------------------------
def test_main_writes_the_csv_and_returns_0(tmp_path):
    out = tmp_path / "wayakit_cotizations.csv"
    session, models = _session(LINES)
    assert cot.main(["--output", str(out)], session=session, today=date(2026, 11, 17)) == 0
    df = pd.read_csv(out, encoding="utf-8-sig")
    assert df["Product_ID"].tolist() == ["FP-001", "FP-002"]
    assert models.calls[0][2][0][-2:] == [["order_id.date_order", ">=", "2025-11-17 00:00:00"],
                                         ["order_id.date_order", "<=", "2026-11-17 23:59:59"]]


def test_zero_sale_lines_returns_1_and_keeps_the_previous_csv(tmp_path):
    out = tmp_path / "wayakit_cotizations.csv"
    out.write_text("Product_ID,Description,approved_quote_price\nFP-OLD,old,1.0\n", encoding="utf-8")
    before = out.read_text(encoding="utf-8")
    session, _ = _session([])
    assert cot.main(["--output", str(out)], session=session, today=date(2026, 11, 17)) == 1
    assert out.read_text(encoding="utf-8") == before
