"""Property tests of `sales_window` in `ml_model/odoo_api_cotizations.py` (C12).

- Equivalence with an obvious reference: start = same month and day one year earlier (day
  clamped to the month length) at 00:00:00; end = the run date at 23:59:59.
- Invariants: start < end, the window covers 365 or 366 days, and the run date and the day 12
  months earlier are inside it; nothing depends on a fixed year.
- Round trip: both strings parse back with the Odoo datetime format.
- Equivalence: a datetime gives the same window as its date.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from tests.conftest import load_module

pytestmark = pytest.mark.property

cot = load_module("ml_model/odoo_api_cotizations.py")
FMT = "%Y-%m-%d %H:%M:%S"
dates = st.dates(min_value=date(2001, 1, 1), max_value=date(2199, 12, 31))


def _reference(d: date) -> tuple[str, str]:
    y = d.year - 1
    start = date(y, d.month, min(d.day, calendar.monthrange(y, d.month)[1]))
    return f"{start.isoformat()} 00:00:00", f"{d.isoformat()} 23:59:59"


@given(dates)
@example(date(2028, 2, 29))
@example(date(2027, 1, 1))
@example(date(2026, 12, 31))
def test_window_matches_reference_and_invariants(d):
    start, end = cot.sales_window(d)
    assert (start, end) == _reference(d)
    s, e = datetime.strptime(start, FMT), datetime.strptime(end, FMT)
    assert s.strftime(FMT) == start and e.strftime(FMT) == end  # round trip
    assert s < e
    assert e.date() == d and e.time() == time(23, 59, 59) and s.time() == time(0, 0, 0)
    assert (d - s.date()).days in (365, 366)
    # The whole run date and the same day one year earlier are inside the window.
    assert s <= datetime.combine(d, time(0, 0)) and datetime.combine(d, time(23, 59, 59)) <= e
    assert s.date() <= d - timedelta(days=365)


@given(dates, st.times())
def test_datetime_gives_the_same_window_as_its_date(d, t):
    assert cot.sales_window(datetime.combine(d, t)) == cot.sales_window(d)
