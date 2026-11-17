"""Differential tests of the ML flow: base ref vs working tree with the same data.

- `test_base_vs_new_has_no_unapproved_differences` runs 1a → 1b → 2 → 3 on both versions
  (see `tests/differential/_harness.py`) and fails if there are differences not listed in
  `approved_changes.json`. It needs `data/f1_before_2026-11-15/` (if the data is missing it is
  skipped, the only skip allowed) and the local base ref (if missing, the test FAILS: a missing ref
  is not missing data and must not pass silently).
- The rest are self-tests of the harness: they show the comparison DOES detect a difference
  (otherwise a test that always passes proves nothing). They need no data.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from tests.differential import _harness as dh
from tests.e2e import _helpers as h

REPORT = dh.REPORT_FILE


# ---------------------------------------------------------------------------------------------
# Real differential test
# ---------------------------------------------------------------------------------------------
@pytest.mark.differential
def test_base_vs_new_has_no_unapproved_differences(tmp_path):
    reason = h.data_missing_reason()
    if reason:
        pytest.skip(reason)
    ref = dh.base_ref()
    if h.resolve_ref(ref) is None:
        pytest.fail(f"the base ref '{ref}' does not exist locally: run `git fetch origin` or set "
                    "PI_BASE_REF. Only missing data allows skipping this test.", pytrace=False)
    result = dh.run_differential(tmp_path, ref=ref)  # WrongCoverage also makes it fail
    summary = result.summary()
    print(summary)
    print(result.base_run.trace())
    print(result.new_run.trace())
    assert not result.pending, summary


# ---------------------------------------------------------------------------------------------
# Harness self-tests (no data)
# ---------------------------------------------------------------------------------------------
def _report() -> pd.DataFrame:
    return pd.DataFrame({
        "Product_ID": ["A", "B", "B", "C"],
        "product_name": ["a", "b1", "b2", "c"],
        "predicted_price": [10.0, 20.5, 20.5, 7.25],
        "competitors_count": [3, 0, 0, 5],
        "clamped_to_market": [False, False, False, True],
    })


def _ok(**kw) -> dict:
    """Minimal valid approval for the report; `kw` changes or adds fields."""
    e = {"file": REPORT, "type": "value_changed", "column": "predicted_price",
         "expected_cells": 1, "reason": "test", "date": "2026-11-17", "approved_by": "Mafer"}
    e.update(kw)
    return e


def _without(e: dict, *fields: str) -> dict:
    return {k: v for k, v in e.items() if k not in fields}



@pytest.mark.unit
@pytest.mark.differential
def test_equal_tables_give_no_differences_even_if_column_order_changes():
    base = _report()
    new = base[list(reversed(base.columns))]
    assert dh.compare_frames(base, new, REPORT) == []


@pytest.mark.unit
@pytest.mark.differential
def test_detects_a_changed_value_and_respects_the_tolerance():
    base = _report()
    new = base.copy()
    new.loc[3, "predicted_price"] = 7.26
    diffs = dh.compare_frames(base, new, REPORT)
    assert diffs == [dh.Difference(REPORT, "value_changed", key="C#0", column="predicted_price",
                                   base=7.25, new=7.26)]
    tiny = base.copy()
    tiny.loc[3, "predicted_price"] = 7.25 + 1e-9
    assert dh.compare_frames(base, tiny, REPORT) == []


@pytest.mark.unit
@pytest.mark.differential
def test_repeated_ids_are_told_apart_by_occurrence():
    base = _report()
    new = base.copy()
    new.loc[2, "product_name"] = "other"
    diffs = dh.compare_frames(base, new, REPORT)
    assert [(d.key, d.column) for d in diffs] == [("B#1", "product_name")]


@pytest.mark.unit
@pytest.mark.differential
def test_detects_added_or_removed_rows_and_columns():
    base = _report()
    new = pd.concat([base.iloc[:3], pd.DataFrame({"Product_ID": ["D"], "product_name": ["d"],
                                                  "predicted_price": [1.0], "competitors_count": [0],
                                                  "clamped_to_market": [False]})], ignore_index=True)
    new = new.drop(columns=["clamped_to_market"]).assign(origin="local")
    types = {(d.type, d.key or d.column) for d in dh.compare_frames(base, new, REPORT)}
    assert ("row_added", "D#0") in types
    assert ("row_removed", "C#0") in types
    assert ("column_added", "origin") in types
    assert ("column_removed", "clamped_to_market") in types


@pytest.mark.unit
@pytest.mark.differential
def test_compare_output_dirs_detects_a_perturbed_value_in_the_csv(tmp_path):
    """In a tmp copy one report price is changed and the comparison reports it."""
    base_repo, new_repo = tmp_path / "base", tmp_path / "new"
    shape = pd.DataFrame({"type_of_product": ["x", "y"], "price_per_liter": [1.0, 2.0]})
    for repo in (base_repo, new_repo):
        (repo / "ml_model").mkdir(parents=True)
        _report().to_csv(repo / REPORT, index=False)
        for rel in dh.COUNT_FILES:
            shape.to_csv(repo / rel, index=False)
    assert dh.compare_output_dirs(base_repo, new_repo) == []

    perturbed = pd.read_csv(new_repo / REPORT)
    perturbed.loc[0, "predicted_price"] += 0.01
    perturbed.to_csv(new_repo / REPORT, index=False)
    shape.iloc[:1].to_csv(new_repo / h.UNIT_PROCESSED, index=False)

    diffs = dh.compare_output_dirs(base_repo, new_repo)
    assert {(d.file, d.type, d.key, d.column) for d in diffs} == {
        (REPORT, "value_changed", "A#0", "predicted_price"),
        (h.UNIT_PROCESSED, "row_count", None, None),
    }
    pending, approved = dh.split_approved(diffs, [])
    summary = dh.summarize(pending, approved)
    assert "predicted_price: 1" in summary and "A#0" in summary


@pytest.mark.unit
@pytest.mark.differential
def test_approvals_filter_only_what_they_cover():
    diffs = [
        dh.Difference(REPORT, "value_changed", key="A#0", column="predicted_price", base=1.0, new=2.0),
        dh.Difference(REPORT, "value_changed", key="A#0", column="product_name", base="a", new="b"),
    ]
    pending, approved = dh.split_approved(diffs, [_ok()])
    assert [d.column for d in pending] == ["product_name"]
    assert [d.column for d in approved] == ["predicted_price"]


@pytest.mark.unit
@pytest.mark.differential
@pytest.mark.parametrize("bad, reason", [
    (_without(_ok(), "reason"), "missing 'reason'"),
    (_ok(date="17/11/2026"), "YYYY-MM-DD"),
    (_ok(date="2026-02-30"), "does not exist"),
    (_without(_ok(), "approved_by"), "missing 'approved_by'"),
    (_ok(approved_by="pi-tester"), "can approve"),
    (_ok(approved_by="mafer"), "can approve"),
    (_ok(type="other"), "is not valid"),
    # type and column are required: without them the entry would be a wildcard for the whole file
    (_without(_ok(), "type"), "missing 'type'"),
    (_without(_ok(), "column"), "requires 'column'"),
    (_ok(type="column_added", column=""), "requires 'column'"),
    (_ok(type="row_added"), "takes no 'column'"),
    # expected_cells is required, integer ≥ 1
    (_without(_ok(), "expected_cells"), "missing 'expected_cells'"),
    (_ok(expected_cells=0), "integer ≥ 1"),
    (_ok(expected_cells="3"), "integer ≥ 1"),
    (_ok(expected_cells=True), "integer ≥ 1"),
    (_ok(expected_cells=2.0), "integer ≥ 1"),
    # compatible file and type; filters only where they make sense
    (_ok(file="ml_model/other.csv"), "is not compared"),
    (_ok(type="row_count", file=REPORT) | {"column": None}, "does not apply"),
    (_without(_ok(file=h.VOL_PROCESSED), "column") | {"type": "value_changed"}, "does not apply"),
    (_ok(type="column_removed", subindustry="Pets"), "'subindustry' is only allowed"),
    (_ok(subindustry=""), "'subindustry' is only allowed"),
    (_ok(key=None), "'key' is only allowed"),
    # unknown fields: a misspelled name would silently widen the approval
    (_ok(columns="predicted_price"), "not allowed"),
])
def test_invalid_approvals_are_rejected(bad, reason):
    with pytest.raises(dh.InvalidApprovals, match=reason):
        dh.validate_approvals([bad])


@pytest.mark.unit
@pytest.mark.differential
@pytest.mark.parametrize("good", [
    _ok(),
    _ok(key="A#0", subindustry="Pets", expected_cells=3),
    _without(_ok(type="row_added", key="D#0"), "column"),
    _without(_ok(type="row_removed", subindustry="Pets", expected_cells=40), "column"),
    _ok(type="column_added", column="origin"),
    _without(_ok(file=h.UNIT_PROCESSED, type="row_count"), "column"),
    _ok(file=h.TO_PREDICT, type="column_removed", column="x"),
])
def test_valid_approvals_are_accepted(good):
    dh.validate_approvals([good])


def _diffs_by_subindustry() -> list[dh.Difference]:
    """Base vs new with prices changed in 2 Pets rows and 1 Home care row."""
    base = _report().assign(subindustry=["Pets", "Pets", "Home care", "Pets"])
    new = base.copy()
    new.loc[[0, 3], "predicted_price"] += 1.0   # A and C (Pets)
    new.loc[2, "predicted_price"] += 1.0        # B#1 (Home care)
    return dh.compare_frames(base, new, REPORT)


@pytest.mark.unit
@pytest.mark.differential
def test_report_differences_carry_their_subindustry():
    diffs = _diffs_by_subindustry()
    assert {(d.key, d.subindustry) for d in diffs} == {("A#0", "Pets"), ("C#0", "Pets"),
                                                       ("B#1", "Home care")}


@pytest.mark.unit
@pytest.mark.differential
def test_subindustry_filter_does_not_approve_other_subindustries():
    """Case from r7 (C17): approving a Pets change must not hide a price change in another subindustry."""
    diffs = _diffs_by_subindustry()
    pending, approved = dh.split_approved(diffs, [_ok(subindustry="Pets", expected_cells=2)])
    assert {d.key for d in approved} == {"A#0", "C#0"}
    assert [(d.key, d.subindustry) for d in pending] == [("B#1", "Home care")]


@pytest.mark.unit
@pytest.mark.differential
@pytest.mark.parametrize("expected", [1, 3])
def test_coverage_different_from_expected_cells_fails(expected):
    """The entry covers 2 Pets cells: declaring more or fewer fails instead of silently approving."""
    diffs = _diffs_by_subindustry()
    with pytest.raises(dh.WrongCoverage, match=rf"expected {expected}, covers 2"):
        dh.split_approved(diffs, [_ok(subindustry="Pets", expected_cells=expected)])


@pytest.mark.unit
@pytest.mark.differential
def test_unfiltered_approval_that_covers_too_much_fails():
    """Without subindustry the same entry covers 3 cells; with expected_cells=2 that is an error."""
    with pytest.raises(dh.WrongCoverage, match="expected 2, covers 3"):
        dh.split_approved(_diffs_by_subindustry(), [_ok(expected_cells=2)])


@pytest.mark.unit
@pytest.mark.differential
def test_approval_that_no_longer_covers_anything_fails():
    unused = _without(_ok(file=h.VOL_PROCESSED, type="row_count"), "column")
    with pytest.raises(dh.WrongCoverage, match="expected 1, covers 0"):
        dh.split_approved(_diffs_by_subindustry(), [_ok(subindustry="Pets", expected_cells=2), unused])


@pytest.mark.unit
@pytest.mark.differential
def test_split_approved_validates_the_entries():
    with pytest.raises(dh.InvalidApprovals, match="can approve"):
        dh.split_approved(_diffs_by_subindustry(), [_ok(approved_by="someone else")])


@pytest.mark.unit
@pytest.mark.differential
def test_repo_approvals_file_is_valid():
    entries = dh.load_approvals()
    raw = json.loads(dh.APPROVALS_FILE.read_text(encoding="utf-8"))
    assert isinstance(raw["changes"], list) and entries == raw["changes"]
    dh.validate_approvals([raw["_example"]])  # the documented example follows the rules
