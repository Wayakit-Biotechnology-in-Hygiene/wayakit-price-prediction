"""Property tests of the pure parts of `scraper/main.py` (coverage report and output rows).

- Coverage: the report has one row per (subindustry, type_of_product, site, keyword), the same
  set of keys as the entries, in first-seen order; each numeric column adds up to the sum over
  the entries; aggregating the report again changes nothing (idempotence).
- `stats_delta` + `coverage_entry`: found / relevant / ai_failures are the counter increments.
- C08: `build_row_data` always has exactly `config.CSV_COLUMNS` and round-trips through
  `csv.DictWriter` (default `extrasaction='raise'`) and `csv.DictReader`, for any text: NUL is
  stripped (`strip_nul`, idempotent, identity on text without NUL).
- `temp_path_for`: same directory, different from the output, still a `.csv` when the output is.
"""
from __future__ import annotations

import csv
import io
import os

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.conftest import load_module

pytestmark = pytest.mark.property

m = load_module("scraper/main.py")
NUMERIC = ("found", "relevant", "rows_written", "ai_failures")

small_text = st.sampled_from(["Home", "Pets", "H1-Floor cleaner", "amazon", "saco", "glass cleaner", "x"])
counts = st.integers(0, 50)
entry = st.builds(
    lambda sub, top, site, kw, found, rel, rows, fails, err: {
        "subindustry": sub, "type_of_product": top, "site": site, "keyword": kw, "found": found,
        "relevant": rel, "rows_written": rows, "ai_failures": fails, "error": err},
    small_text, small_text, small_text, small_text, counts, counts, counts, counts,
    st.sampled_from(["", "", "TimeoutError", "WebDriverException", "RelevanceAgentFatalError"]))


def _key(row):
    return (row["subindustry"], row["type_of_product"], row["site"], row["keyword"])


@given(st.lists(entry, max_size=40))
def test_coverage_totals_add_up_and_keys_are_unique(entries):
    report = m.build_coverage_report(entries)
    keys = [_key(r) for r in report]
    assert len(keys) == len(set(keys))
    assert keys == list(dict.fromkeys(_key(e) for e in entries))  # first-seen order
    for col in NUMERIC:
        assert sum(r[col] for r in report) == sum(e[col] for e in entries)
    for r in report:
        errors = [e["error"] for e in entries if _key(e) == _key(r) and e["error"]]
        assert r["error"].split(";") if r["error"] else [] == list(dict.fromkeys(errors))
        assert list(r) == m.COVERAGE_COLUMNS
    assert m.build_coverage_report(report) == report  # idempotent
    assert all(r["relevant"] == 0 for r in m.zero_relevant_pairs(report))


@given(st.fixed_dictionaries({k: counts for k in ("evaluated", "relevant", "failures", "wipes_failures")}),
       st.fixed_dictionaries({k: counts for k in ("evaluated", "relevant", "failures", "wipes_failures")}),
       counts)
def test_coverage_entry_uses_the_counter_increments(before, increment, rows):
    after = {k: before[k] + increment[k] for k in before}
    delta = m.stats_delta(before, after)
    assert delta == increment
    e = m.coverage_entry("S", "T", "amazon", "k", delta, rows)
    assert (e["found"], e["relevant"], e["ai_failures"], e["rows_written"]) == (
        increment["evaluated"], increment["relevant"], increment["failures"] + increment["wipes_failures"], rows)


# Any text, NUL included: build_row_data strips "\x00" (the csv module cannot write it).
any_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=20)
cell = st.one_of(st.none(), any_text, st.integers(-10**6, 10**6),
                 st.floats(allow_nan=False, allow_infinity=False))
product = st.fixed_dictionaries({}, optional={k: cell for k in (
    "Product", "Price_SAR", "Company", "URL", "Unit of measurement", "Total quantity", "Extra key")})


@given(product, any_text, any_text, st.sampled_from(["amazon", "mumzworld", "saco"]))
def test_row_data_round_trips_through_the_csv(prod, sub, top, site):
    row = m.build_row_data(prod, "Industry", sub, top, "Generic", site, m.channel_for(sub), "2026-11-14")
    assert list(row) == m.config.CSV_COLUMNS
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=m.config.CSV_COLUMNS)
    writer.writeheader()
    writer.writerow(row)
    [back] = list(csv.DictReader(io.StringIO(buf.getvalue())))
    assert back == {k: ("" if v is None else str(v)) for k, v in row.items()}
    assert "\x00" not in buf.getvalue()
    for key, value in (("product", prod.get("Product")), ("type_of_product", top), ("subindustry", sub)):
        expected = value.replace("\x00", "") if isinstance(value, str) else value
        assert row[key] == expected


@given(any_text)
def test_strip_nul_is_idempotent_and_identity_without_nul(s):
    once = m.strip_nul(s)
    assert m.strip_nul(once) == once and "\x00" not in once
    if "\x00" not in s:
        assert once == s


@given(st.from_regex(r"[a-h][a-h_.-]{0,11}", fullmatch=True), st.sampled_from(["", ".csv", ".txt"]))
def test_temp_path_is_a_sibling(name, ext):
    out = os.path.join("some", "dir", name + ext)
    tmp = m.temp_path_for(out)
    assert os.path.dirname(tmp) == os.path.dirname(out)
    assert tmp != out
    if out.endswith(".csv"):
        assert tmp.endswith(".csv")
