"""Unit tests of the safe Odoo upload (`ml_model/odoo_api_price_suggestion.py`, C13, REVIEW rule 2).

A fake Odoo (`FakeOdoo`) keeps the `product.price.suggestion` and history tables in memory,
records every `execute_kw` call and can fail any (model, method) on demand. Nothing reads secrets
or reaches Odoo: `main()` always gets `session=` (or `--dry-run`, which must not touch it).

What it proves:
- full mode order: company guard → search → create → read → history create → unlink, and the
  unlink only ever receives the PREVIOUS IDs;
- create fails → exit 1, the old suggestions are untouched and no IDs file is left;
- archive or unlink fails → exit 1, the IDs file is kept, and `--cleanup-previous` finishes the
  job without archiving twice;
- more (or fewer) than one visible company → exit 1 before any write;
- `--dry-run` never touches the session and writes the records CSV;
- `build_records` / `check_parse_quality`: bad rows are skipped one by one, but no valid rows or
  more than 5 % skipped → exit 1.
"""
from __future__ import annotations

import json
import math

import pandas as pd
import pytest

from pi_odoo import OdooSession
from tests.conftest import load_module

pytestmark = pytest.mark.unit

up = load_module("ml_model/odoo_api_price_suggestion.py")
SUG, HIST = up.MODEL_NAME, up.HISTORY_MODEL_NAME
WRITES = {"create", "unlink", "write"}


class OdooDown(Exception):
    pass


class FakeOdoo:
    """In-memory Odoo with just what the upload uses."""

    def __init__(self, existing: int = 3, companies=("Fake KSA Co",), fail=()):
        self.companies = [{"id": i + 1, "name": n} for i, n in enumerate(companies)]
        self.next_id = 100
        self.table = {}
        for i in range(existing):
            self._insert(self.table, {"product_id_str": f"OLD-{i}", "suggested_price": 10.0 + i,
                                      "create_date": "2026-11-07 04:00:00"})
        self.history = {}
        self.fail = set(fail)
        self.calls = []

    def _insert(self, table, rec):
        self.next_id += 1
        table[self.next_id] = dict(rec)
        return self.next_id

    def execute_kw(self, db, uid, token, model, method, args, kwargs=None):
        self.calls.append((model, method))
        if (model, method) in self.fail:
            raise OdooDown(f"fake failure on {model}.{method}")
        if model == "res.company" and method == "search_read":
            return [dict(c) for c in self.companies]
        table = self.table if model == SUG else self.history
        if method == "search":
            assert args == [[]], args
            return sorted(table)
        if method == "create":
            return [self._insert(table, r) for r in args[0]]
        if method == "read":
            return [{"id": i, **{f: table[i].get(f) for f in kwargs["fields"]}} for i in args[0] if i in table]
        if method == "unlink":
            for i in args[0]:
                table.pop(i)
            return True
        raise AssertionError(f"unexpected call {model}.{method}")

    def session(self):
        return OdooSession(models=self, db="FAKE-db", uid=1, api_token="FAKE-token")

    def writes(self):
        return [c for c in self.calls if c[1] in WRITES]


def _row(i: int, **over) -> dict:
    row = {"Product_ID": f"FP-{i:03d}", "product_name": f"Fake product {i}", "product_type": "H1-Floor cleaner",
           "generic_product_type": "Floor cleaner", "subindustry": "Home", "industry": "Home care",
           "volume": "0.5 L", "cost_per_unit": 4.0, "predicted_price": 9.99, "predicted_price_per_unit": 19.98,
           "porcentaje_de_ganancia": 149.75, "model_confidence": 80.0, "market_min_found": 5.0,
           "market_max_found": 20.0, "competitors_count": 4, "clamped_to_market": False,
           "volume_outside_market": False}
    row.update(over)
    return row


@pytest.fixture
def report(tmp_path):
    def write(rows):
        p = tmp_path / "wayakit_prediction_report.csv"
        pd.DataFrame(rows).to_csv(p, index=False)
        return p
    return write


@pytest.fixture
def ids_file(tmp_path):
    return tmp_path / "logs" / "odoo_previous_suggestion_ids.json"


def _main(report_path, ids_file, *extra, odoo=None):
    argv = ["--input", str(report_path), "--ids-file", str(ids_file), *extra]
    return up.main(argv, session=odoo.session() if odoo else None)


# --- full mode ----------------------------------------------------------------------------------
def test_full_mode_creates_first_then_archives_and_unlinks_only_the_old_ids(report, ids_file):
    odoo = FakeOdoo(existing=3)
    old_ids = sorted(odoo.table)
    old = {i: dict(r) for i, r in odoo.table.items()}
    assert _main(report([_row(i) for i in range(5)]), ids_file, "--run_mode", "full", odoo=odoo) == 0
    assert odoo.calls == [("res.company", "search_read"), (SUG, "search"), (SUG, "create"), (SUG, "read"),
                          (HIST, "create"), (SUG, "unlink")]
    assert sorted(r["product_id_str"] for r in odoo.table.values()) == [f"FP-{i:03d}" for i in range(5)]
    assert not set(odoo.table) & set(old_ids)
    # History: every old record once, create_date → original_create_date, no id.
    assert sorted(r["product_id_str"] for r in odoo.history.values()) == sorted(r["product_id_str"] for r in old.values())
    assert all("original_create_date" in r and "create_date" not in r and "id" not in r
               for r in odoo.history.values())
    assert not ids_file.exists(), "the IDs file is deleted when the cleanup finishes"


def test_full_mode_with_an_empty_table_only_creates(report, ids_file):
    odoo = FakeOdoo(existing=0)
    assert _main(report([_row(1)]), ids_file, "--run_mode", "full", odoo=odoo) == 0
    assert odoo.calls == [("res.company", "search_read"), (SUG, "search"), (SUG, "create")]
    assert len(odoo.table) == 1 and not ids_file.exists()


def test_create_failure_exits_1_and_leaves_the_old_suggestions_untouched(report, ids_file):
    odoo = FakeOdoo(existing=3, fail={(SUG, "create")})
    before = {i: dict(r) for i, r in odoo.table.items()}
    assert _main(report([_row(i) for i in range(5)]), ids_file, "--run_mode", "full", odoo=odoo) == 1
    assert odoo.table == before and odoo.history == {}
    assert (SUG, "unlink") not in odoo.calls and (HIST, "create") not in odoo.calls
    assert not ids_file.exists(), "nothing to clean up after a failed create"


@pytest.mark.parametrize("failing", [(SUG, "read"), (HIST, "create"), (SUG, "unlink")])
def test_cleanup_failure_exits_1_keeps_the_ids_file_and_cleanup_previous_finishes(report, ids_file, failing):
    odoo = FakeOdoo(existing=3, fail={failing})
    old_ids = sorted(odoo.table)
    assert _main(report([_row(i) for i in range(4)]), ids_file, "--run_mode", "full", odoo=odoo) == 1
    assert len(odoo.table) == 3 + 4, "old and new rows: the table is never left empty"
    payload = json.loads(ids_file.read_text(encoding="utf-8"))
    assert payload["ids"] == old_ids and payload["model"] == SUG
    assert payload["archived"] is (failing == (SUG, "unlink"))

    # Another full upload refuses to start while the cleanup is pending.
    odoo.fail.clear()
    odoo.calls.clear()
    assert _main(report([_row(9)]), ids_file, "--run_mode", "full", odoo=odoo) == 1
    assert odoo.writes() == []

    # --cleanup-previous finishes: only the old IDs go, archived exactly once.
    odoo.calls.clear()
    history_before = len(odoo.history)
    assert _main(report([_row(9)]), ids_file, "--cleanup-previous", odoo=odoo) == 0
    assert odoo.calls[0] == ("res.company", "search_read")
    assert sorted(odoo.table) == sorted(set(odoo.table) - set(old_ids)) and len(odoo.table) == 4
    assert len(odoo.history) == 3, f"archived {len(odoo.history) - history_before} more; must be 3 in total"
    assert not ids_file.exists()


def test_overlapping_ids_never_unlink(report, ids_file):
    """If Odoo ever returned a new ID equal to an old one, nothing is deleted."""
    odoo = FakeOdoo(existing=1)
    old_id = next(iter(odoo.table))
    odoo_create = odoo._insert

    def insert(table, rec):
        return odoo_create(table, rec) if table is odoo.history else (table.__setitem__(old_id, dict(rec)) or old_id)

    odoo._insert = insert  # the suggestion create returns the old ID
    assert _main(report([_row(1)]), ids_file, "--run_mode", "full", odoo=odoo) == 1
    assert (SUG, "unlink") not in odoo.calls


# --- partial mode and guards --------------------------------------------------------------------
def test_partial_mode_only_creates(report, ids_file):
    odoo = FakeOdoo(existing=3)
    assert _main(report([_row(1), _row(2)]), ids_file, "--run_mode", "partial", odoo=odoo) == 0
    assert odoo.calls == [("res.company", "search_read"), (SUG, "create")]
    assert len(odoo.table) == 5 and not ids_file.exists()


@pytest.mark.parametrize("companies", [(), ("Fake KSA Co", "Fake MX Co")], ids=["none", "two"])
@pytest.mark.parametrize("mode", ["--run_mode=full", "--run_mode=partial", "--cleanup-previous"])
def test_not_exactly_one_company_exits_1_before_any_write(report, ids_file, companies, mode):
    odoo = FakeOdoo(existing=3, companies=companies)
    if mode == "--cleanup-previous":
        up.save_previous_ids(str(ids_file), sorted(odoo.table))
    assert _main(report([_row(1)]), ids_file, mode, odoo=odoo) == 1
    assert odoo.writes() == [] and odoo.calls[0] == ("res.company", "search_read")


def test_company_failure_is_not_a_guess(report, ids_file):
    odoo = FakeOdoo(fail={("res.company", "search_read")})
    assert _main(report([_row(1)]), ids_file, "--run_mode", "full", odoo=odoo) == 1
    assert odoo.writes() == []


# --- dry run ------------------------------------------------------------------------------------
class Untouchable:
    def __getattr__(self, name):
        raise AssertionError(f"--dry-run touched the session ({name})")


def test_dry_run_never_touches_the_session_and_writes_the_records(report, ids_file, tmp_path):
    out = tmp_path / "dry.csv"
    rows = [_row(i) for i in range(3)]
    argv = ["--input", str(report(rows)), "--ids-file", str(ids_file), "--run_mode", "full",
            "--dry-run", "--dry-run-output", str(out)]
    assert up.main(argv, session=Untouchable()) == 0
    df = pd.read_csv(out)
    assert df["product_id_str"].tolist() == ["FP-000", "FP-001", "FP-002"]
    assert df["suggested_price"].tolist() == [9.99] * 3
    assert not ids_file.exists()


def test_dry_run_still_fails_on_a_broken_report(report, ids_file, tmp_path):
    rows = [_row(i, predicted_price="n/a") for i in range(3)]
    argv = ["--input", str(report(rows)), "--ids-file", str(ids_file), "--dry-run",
            "--dry-run-output", str(tmp_path / "dry.csv")]
    assert up.main(argv, session=Untouchable()) == 1
    assert up.main(["--input", str(tmp_path / "missing.csv"), "--dry-run"], session=Untouchable()) == 1


def test_dry_run_cleanup_previous_only_reads_the_file(ids_file):
    up.save_previous_ids(str(ids_file), [3, 1, 2])
    assert up.main(["--ids-file", str(ids_file), "--cleanup-previous", "--dry-run"], session=Untouchable()) == 0
    assert json.loads(ids_file.read_text())["ids"] == [1, 2, 3]


def test_missing_report_exits_1_without_touching_odoo(tmp_path, ids_file):
    odoo = FakeOdoo()
    assert _main(tmp_path / "missing.csv", ids_file, "--run_mode", "full", odoo=odoo) == 1
    assert odoo.calls == []


# --- records ------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    {"Product_ID": None}, {"Product_ID": "  "}, {"volume": "abc L"}, {"volume": "nan L"},
    {"predicted_price": "n/a"}, {"predicted_price": math.inf}, {"cost_per_unit": math.nan},
    {"competitors_count": 2.5},
], ids=["no-id", "blank-id", "volume-text", "volume-nan", "price-text", "price-inf", "cost-nan",
        "count-not-int"])
def test_bad_rows_are_skipped_one_by_one(bad):
    df = pd.DataFrame([_row(1), _row(2, **bad), _row(3)])
    records, skipped = up.build_records(df)
    assert [r["product_id_str"] for r in records] == ["FP-001", "FP-003"]
    assert len(skipped) == 1 and skipped[0][0] == 2


def test_record_fields_and_types():
    [rec], [] = up.build_records(pd.DataFrame([_row(7, volume="12 units", product_type=float("nan"))]))
    assert rec == {"product_id_str": "FP-007", "volume_units": 12.0, "product_type": False,
                   "generic_product_type": "Floor cleaner", "subindustry": "Home", "industry": "Home care",
                   "product_name": "Fake product 7", "production_cost": 4.0, "suggested_price": 9.99,
                   "profit": 149.75, "predicted_price_per_unit": 19.98, "model_confidence": 80.0,
                   "market_min_found": 5.0, "market_max_found": 20.0, "competitors_count": 4}
    assert type(rec["competitors_count"]) is int


def test_missing_column_skips_every_row_and_exits_1(report, ids_file):
    rows = [{k: v for k, v in _row(i).items() if k != "predicted_price"} for i in range(3)]
    odoo = FakeOdoo()
    assert _main(report(rows), ids_file, "--run_mode", "partial", odoo=odoo) == 1
    assert odoo.calls == []


@pytest.mark.parametrize("n_bad, n_total, ok", [(1, 20, True), (2, 20, False), (0, 1, True), (1, 1, False)])
def test_more_than_5_percent_skipped_exits_1(report, ids_file, n_bad, n_total, ok):
    rows = [_row(i, predicted_price="n/a") if i < n_bad else _row(i) for i in range(n_total)]
    odoo = FakeOdoo(existing=2)
    code = _main(report(rows), ids_file, "--run_mode", "full", odoo=odoo)
    assert code == (0 if ok else 1)
    if not ok:
        assert odoo.calls == [], "a broken report must not reach Odoo"


def test_history_records_do_not_mutate_the_input():
    old = [{"id": 5, "create_date": "2026-11-07 04:00:00", "suggested_price": 1.0}]
    copy = [dict(r) for r in old]
    assert up.history_records(old) == [{"original_create_date": "2026-11-07 04:00:00", "suggested_price": 1.0}]
    assert old == copy


def test_load_previous_ids_rejects_other_files(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"model": "res.partner", "ids": [1]}))
    with pytest.raises(up.UploadError):
        up.load_previous_ids(str(p))
