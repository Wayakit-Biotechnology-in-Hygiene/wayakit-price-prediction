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
- IDs file location (Pending 17, resolved 2026-11-18): without `--ids-file` it lives in the single
  log folder, `PI_LOG_DIR` if set and non-empty, else `<repo>/../logs` (never inside the code), so a
  `--cleanup-previous` run in a later process finds the file the failed upload left.
- Stability-brake state (2026-11-25): a real upload copies the report to `--state-file`
  (atomically) as soon as its create returns, before the cleanup, so a failed cleanup still leaves
  the new prices there; a dry run, a failed create and `--cleanup-previous` never write it; a
  failed copy keeps the upload, still runs the cleanup, removes the older state file and exits 1.
  Its default is the path `3_predicted_prices.py` reads.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import math
import os

import pandas as pd
import pytest

from pi_odoo import OdooSession
from tests.conftest import REPO_ROOT, load_module

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
        self.ids_file = None   # if set, the IDs file content is snapshotted at every call
        self.file_at = []      # (model, method, parsed file or None), one per call

    def _insert(self, table, rec):
        self.next_id += 1
        table[self.next_id] = dict(rec)
        return self.next_id

    def execute_kw(self, db, uid, token, model, method, args, kwargs=None):
        self.calls.append((model, method))
        if self.ids_file is not None:
            content = json.loads(self.ids_file.read_text(encoding="utf-8")) if self.ids_file.exists() else None
            self.file_at.append((model, method, content))
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
        if method == "search_count":
            [[field, op, ids]] = args[0]
            assert (field, op) == ("id", "in") and kwargs == {}, (args, kwargs)
            return sum(1 for i in ids if i in table)
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


def _state_file(ids_file):
    """The stability-brake state file of a test: next to its IDs file, in tmp."""
    return ids_file.parent / "state" / "last_uploaded_prediction_report.csv"


def _main(report_path, ids_file, *extra, odoo=None):
    argv = ["--input", str(report_path), "--ids-file", str(ids_file),
            "--state-file", str(_state_file(ids_file)), *extra]
    return up.main(argv, session=odoo.session() if odoo else None)


# --- full mode ----------------------------------------------------------------------------------
def test_full_mode_creates_first_then_archives_and_unlinks_only_the_old_ids(report, ids_file):
    odoo = FakeOdoo(existing=3)
    old_ids = sorted(odoo.table)
    old = {i: dict(r) for i, r in odoo.table.items()}
    assert _main(report([_row(i) for i in range(5)]), ids_file, "--run_mode", "full", odoo=odoo) == 0
    assert odoo.calls == [("res.company", "search_read"), (SUG, "search"), (SUG, "create"),
                          (SUG, "search_count"), (SUG, "read"), (HIST, "create"), (SUG, "unlink")]
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
    assert payload["created_ids"] == sorted(set(odoo.table) - set(old_ids)), "the new batch is recorded"
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


def test_held_rows_are_never_uploaded(report, ids_file, tmp_path):
    """Audit 2026-11-26 (D3, E2): a row with a `hold_reason` is left out, like a row without a size;
    an empty reason (written back as NaN by a CSV) is uploaded."""
    out = tmp_path / "dry.csv"
    rows = [_row(0, hold_reason=""), _row(1, hold_reason="cost is 0 or missing in Odoo"),
            _row(2, hold_reason="model-only price in Aviation (held for review)"), _row(3)]
    argv = ["--input", str(report(rows)), "--ids-file", str(ids_file), "--run_mode", "full",
            "--dry-run", "--dry-run-output", str(out)]
    assert up.main(argv, session=Untouchable()) == 0
    assert pd.read_csv(out)["product_id_str"].tolist() == ["FP-000", "FP-003"]


def test_held_rows_are_not_created_in_odoo(report, ids_file):
    odoo = FakeOdoo(existing=2)
    rows = [_row(0), _row(1, hold_reason="the name says 0.1 L but Volume_Liters is 8.4"), _row(2)]
    assert _main(report(rows), ids_file, "--run_mode", "full", odoo=odoo) == 0
    # The old suggestions are archived and removed; only the two uploadable rows are left.
    assert sorted(r["product_id_str"] for r in odoo.table.values()) == ["FP-000", "FP-002"]


def test_held_marks_only_non_empty_reasons():
    df = pd.DataFrame({"hold_reason": ["", None, float("nan"), "  ", "cost is 0"]})
    assert up.held(df).tolist() == [False, False, False, False, True]
    assert up.held(pd.DataFrame({"x": [1, 2]})).tolist() == [False, False]


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



# --- Review I2 (d9013ee): never let --cleanup-previous empty the table ---------------------------
def _ids_payload(ids, archived=False, **extra):
    return {"model": SUG, "saved_at_utc": "2026-11-17 04:00:00", "archived": archived, "ids": sorted(ids), **extra}


@pytest.mark.parametrize("created", ["missing", None, []], ids=["key-missing", "null", "empty"])
def test_cleanup_refuses_when_the_new_batch_was_never_confirmed(report, ids_file, created):
    """Upload killed between saving the IDs and the create: only the old suggestions exist."""
    odoo = FakeOdoo(existing=3)
    before = {i: dict(r) for i, r in odoo.table.items()}
    extra = {} if created == "missing" else {"created_ids": created}
    ids_file.parent.mkdir(parents=True, exist_ok=True)
    ids_file.write_text(json.dumps(_ids_payload(sorted(odoo.table), **extra)), encoding="utf-8")
    content = ids_file.read_text(encoding="utf-8")
    assert _main(report([_row(1)]), ids_file, "--cleanup-previous", odoo=odoo) == 1
    assert odoo.calls == [("res.company", "search_read")], "no Odoo call beyond the company check"
    assert odoo.table == before and odoo.history == {}
    assert ids_file.read_text(encoding="utf-8") == content, "the file is left as it was"


@pytest.mark.parametrize("archived", [False, True])
def test_cleanup_refuses_when_created_ids_no_longer_exist(report, ids_file, archived):
    odoo = FakeOdoo(existing=3)
    old = sorted(odoo.table)
    new_id = odoo._insert(odoo.table, {"product_id_str": "NEW-1"})
    up.save_previous_ids(str(ids_file), old, archived=archived, created_ids=[new_id, 99999])  # 99999 is gone
    before = {i: dict(r) for i, r in odoo.table.items()}
    assert _main(report([_row(1)]), ids_file, "--cleanup-previous", odoo=odoo) == 1
    assert odoo.calls == [("res.company", "search_read"), (SUG, "search_count")]
    assert odoo.table == before and odoo.history == {} and ids_file.exists()


@pytest.mark.parametrize("archived", [False, True])
def test_cleanup_with_a_confirmed_batch_finishes(report, ids_file, archived):
    odoo = FakeOdoo(existing=3)
    old = sorted(odoo.table)
    new_ids = [odoo._insert(odoo.table, {"product_id_str": f"NEW-{i}"}) for i in range(2)]
    up.save_previous_ids(str(ids_file), old, archived=archived, created_ids=new_ids)
    assert _main(report([_row(1)]), ids_file, "--cleanup-previous", odoo=odoo) == 0
    expected = [("res.company", "search_read"), (SUG, "search_count"), (SUG, "read")]
    expected += [] if archived else [(HIST, "create")]
    assert odoo.calls == expected + [(SUG, "unlink")]
    assert sorted(odoo.table) == sorted(new_ids)
    assert len(odoo.history) == (0 if archived else 3)
    assert not ids_file.exists()


def test_cleanup_when_the_old_ids_are_already_gone_only_removes_the_file(report, ids_file):
    odoo = FakeOdoo(existing=0)
    new_ids = [odoo._insert(odoo.table, {"product_id_str": "NEW-1"})]
    up.save_previous_ids(str(ids_file), [5, 6], created_ids=new_ids)
    assert _main(report([_row(1)]), ids_file, "--cleanup-previous", odoo=odoo) == 0
    assert odoo.calls == [("res.company", "search_read"), (SUG, "search_count"), (SUG, "read")]
    assert sorted(odoo.table) == new_ids and odoo.history == {} and not ids_file.exists()


def test_cleanup_refuses_overlapping_created_and_previous_ids(report, ids_file):
    odoo = FakeOdoo(existing=2)
    old = sorted(odoo.table)
    up.save_previous_ids(str(ids_file), old, created_ids=[old[0]])
    assert _main(report([_row(1)]), ids_file, "--cleanup-previous", odoo=odoo) == 1
    assert odoo.writes() == [] and (HIST, "create") not in odoo.calls and ids_file.exists()


def test_full_flow_records_created_ids_before_archiving(report, ids_file):
    odoo = FakeOdoo(existing=3)
    old = sorted(odoo.table)
    odoo.ids_file = ids_file
    assert _main(report([_row(i) for i in range(4)]), ids_file, "--run_mode", "full", odoo=odoo) == 0
    new_ids = sorted(odoo.table)
    seen = {(mdl, meth): content for mdl, meth, content in odoo.file_at}
    # Before the create: previous IDs saved, no created_ids yet.
    assert seen[(SUG, "create")]["ids"] == old and seen[(SUG, "create")]["created_ids"] is None
    # From the existence check on, created_ids is on disk; archived only after the history create.
    for call in ((SUG, "search_count"), (SUG, "read"), (HIST, "create")):
        assert seen[call]["created_ids"] == new_ids, call
        assert seen[call]["archived"] is False, call
    assert seen[(SUG, "unlink")]["archived"] is True and seen[(SUG, "unlink")]["created_ids"] == new_ids
    assert not ids_file.exists()


def test_full_flow_with_a_missing_new_batch_refuses_before_archiving(report, ids_file):
    """If Odoo does not see the new records right after the create, nothing old is touched."""
    odoo = FakeOdoo(existing=3)
    real = odoo.execute_kw

    def lying_count(db, uid, token, model, method, args, kwargs=None):
        if method == "search_count":
            odoo.calls.append((model, method))
            return 0
        return real(db, uid, token, model, method, args, kwargs)

    odoo.execute_kw = lying_count
    assert _main(report([_row(i) for i in range(2)]), ids_file, "--run_mode", "full", odoo=odoo) == 1
    assert (SUG, "read") not in odoo.calls and (SUG, "unlink") not in odoo.calls and odoo.history == {}
    assert json.loads(ids_file.read_text())["created_ids"], "kept for --cleanup-previous"


def test_full_upload_refuses_while_a_confirmed_batch_awaits_cleanup(report, ids_file):
    odoo = FakeOdoo(existing=3)
    up.save_previous_ids(str(ids_file), sorted(odoo.table), created_ids=[500])
    content = ids_file.read_text(encoding="utf-8")
    assert _main(report([_row(1)]), ids_file, "--run_mode", "full", odoo=odoo) == 1
    assert odoo.calls == [("res.company", "search_read")]
    assert ids_file.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("created", ["missing", None])
def test_full_upload_discards_an_unconfirmed_stale_file_and_replaces_everything(report, ids_file, created):
    """Killed before/while creating: the next full run reads ALL current IDs (incl. any batch the
    killed create committed) and replaces them."""
    odoo = FakeOdoo(existing=2)
    stray = odoo._insert(odoo.table, {"product_id_str": "BATCH-OF-KILLED-RUN"})
    extra = {} if created == "missing" else {"created_ids": None}
    ids_file.parent.mkdir(parents=True, exist_ok=True)
    ids_file.write_text(json.dumps(_ids_payload(sorted(set(odoo.table) - {stray}), **extra)), encoding="utf-8")
    assert _main(report([_row(i) for i in range(3)]), ids_file, "--run_mode", "full", odoo=odoo) == 0
    assert sorted(r["product_id_str"] for r in odoo.table.values()) == ["FP-000", "FP-001", "FP-002"]
    assert len(odoo.history) == 3 and not ids_file.exists()


def test_malformed_ids_file_refuses_the_full_upload(report, ids_file):
    odoo = FakeOdoo(existing=1)
    ids_file.parent.mkdir(parents=True, exist_ok=True)
    ids_file.write_text(json.dumps(_ids_payload([1], created_ids="not-a-list")), encoding="utf-8")
    assert _main(report([_row(1)]), ids_file, "--run_mode", "full", odoo=odoo) == 1
    assert odoo.writes() == []


def test_partial_upload_ignores_the_ids_file(report, ids_file):
    odoo = FakeOdoo(existing=1)
    up.save_previous_ids(str(ids_file), sorted(odoo.table), created_ids=[500])
    assert _main(report([_row(1)]), ids_file, "--run_mode", "partial", odoo=odoo) == 0
    assert odoo.calls == [("res.company", "search_read"), (SUG, "create")] and ids_file.exists()


def test_ids_file_round_trip_and_old_format(tmp_path):
    p = tmp_path / "ids.json"
    up.save_previous_ids(str(p), [3, 1], archived=True, created_ids=[9, 7])
    payload = up.load_previous_ids(str(p))
    assert (payload["ids"], payload["archived"], payload["created_ids"]) == ([1, 3], True, [7, 9])
    p.write_text(json.dumps({"model": SUG, "ids": [1], "archived": False}))  # pre-I2 file
    assert up.load_previous_ids(str(p)).get("created_ids") is None


# --- IDs file location: the single log folder ---------------------------------------------------
IDS_NAME = "odoo_previous_suggestion_ids.json"
_fresh_counter = itertools.count()


def _fresh_upload_module():
    """Imports the upload script again, so PREVIOUS_IDS_FILE is computed from the current env.

    `load_module` caches the module (imported once, with whatever env pytest had); this does not.
    Importing writes nothing (the file-less log_config stand-in is already in sys.modules).
    """
    load_module("ml_model/odoo_api_price_suggestion.py")  # makes sure the stand-in log_config is installed
    path = REPO_ROOT / "ml_model" / "odoo_api_price_suggestion.py"
    spec = importlib.util.spec_from_file_location(f"pi_test_upload_fresh_{next(_fresh_counter)}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ids_file_default_is_in_repo_parent_logs_never_inside_the_code(monkeypatch):
    monkeypatch.delenv("PI_LOG_DIR", raising=False)
    fresh = _fresh_upload_module()
    expected = os.path.join(os.path.dirname(str(REPO_ROOT)), "logs", IDS_NAME)
    assert fresh.PREVIOUS_IDS_FILE == expected
    assert fresh.parse_args([]).ids_file == expected
    assert os.path.isabs(expected) and ".." not in expected.split(os.sep)
    assert not expected.startswith(str(REPO_ROOT) + os.sep), "the IDs file must not live in the code folder"


def test_ids_file_empty_pi_log_dir_is_the_default(monkeypatch):
    monkeypatch.setenv("PI_LOG_DIR", "")
    fresh = _fresh_upload_module()
    assert fresh.PREVIOUS_IDS_FILE == os.path.join(os.path.dirname(str(REPO_ROOT)), "logs", IDS_NAME)


@pytest.mark.parametrize("value", ["{tmp}/state", "{tmp}/a/../state/", "rel-state"])
def test_ids_file_default_follows_pi_log_dir(monkeypatch, tmp_path, value):
    monkeypatch.chdir(tmp_path)  # a relative value is resolved against the current folder
    monkeypatch.setenv("PI_LOG_DIR", value.format(tmp=tmp_path))
    fresh = _fresh_upload_module()
    expected = os.path.join(os.path.abspath(value.format(tmp=tmp_path)), IDS_NAME)
    assert fresh.PREVIOUS_IDS_FILE == expected
    assert fresh.parse_args([]).ids_file == expected
    assert fresh.parse_args(["--ids-file", str(tmp_path / "x.json")]).ids_file == str(tmp_path / "x.json"), \
        "--ids-file still overrides the default"


def test_log_dir_helper_ignores_the_process_env_when_given_one(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_LOG_DIR", str(tmp_path / "from-process-env"))
    assert up.log_dir({}, "/srv/pi/releases/abc123") == "/srv/pi/releases/logs"
    assert up.log_dir({"PI_LOG_DIR": ""}, "/srv/pi/current") == "/srv/pi/logs"
    assert up.log_dir({"PI_LOG_DIR": "/srv/pi/logs"}, "/srv/pi/releases/abc123") == "/srv/pi/logs"


def test_cleanup_previous_in_a_later_process_finds_the_ids_file_in_pi_log_dir(monkeypatch, tmp_path, report):
    """The point of the single folder: a failed full upload leaves the file there and a later
    `--cleanup-previous` (new process, new import, no --ids-file) finds it and finishes."""
    state = tmp_path / "state"
    monkeypatch.setenv("PI_LOG_DIR", str(state))
    odoo = FakeOdoo(existing=3, fail={(SUG, "unlink")})
    old_ids = sorted(odoo.table)
    path = report([_row(i) for i in range(4)])
    first = _fresh_upload_module()
    assert first.main(["--input", str(path), "--run_mode", "full"], session=odoo.session()) == 1
    assert (state / IDS_NAME).is_file(), "the failed cleanup left its IDs file in PI_LOG_DIR"
    odoo.fail.clear()
    later = _fresh_upload_module()
    assert later.main(["--input", str(path), "--cleanup-previous"], session=odoo.session()) == 0
    assert not set(odoo.table) & set(old_ids) and len(odoo.table) == 4
    assert not (state / IDS_NAME).exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state", "wayakit_prediction_report.csv"]


# --- products without a size (F7a) --------------------------------------------------------------
def test_without_size_reads_the_flag_and_the_volume():
    df = pd.DataFrame([_row(0), _row(1, volume="0 units"), _row(2, volume="0.0 L"),
                       _row(3, has_size=False), _row(4, volume="20 units", has_size=True)])
    assert up.without_size(df).tolist() == [False, True, True, True, False]
    # A report from before F7a has no has_size column: the volume alone decides.
    assert up.without_size(df.drop(columns="has_size")).tolist() == [False, True, True, False, False]


def test_products_without_a_size_are_never_uploaded(report, ids_file, tmp_path):
    """Their price is only the cost x 1.30 floor: they are left out of the batch, and they do not
    count as unparseable rows (40 % of the report here, far above the 5 % limit)."""
    out = tmp_path / "dry.csv"
    rows = [_row(0), _row(1, volume="0 units", has_size=False), _row(2, has_size=True),
            _row(3, volume="0 units", has_size=False), _row(4, has_size=True)]
    argv = ["--input", str(report(rows)), "--ids-file", str(ids_file), "--run_mode", "full",
            "--dry-run", "--dry-run-output", str(out)]
    assert up.main(argv, session=Untouchable()) == 0
    assert pd.read_csv(out)["product_id_str"].tolist() == ["FP-000", "FP-002", "FP-004"]


def test_skipped_row_numbers_stay_the_report_rows_after_no_size_rows_are_left_out(report):
    """The warning names the report's own row even after main() leaves the no-size rows out."""
    rows = [_row(0, volume="0 units", has_size=False)] + [_row(i) for i in range(1, 30)]
    rows[5]["predicted_price"] = "not a number"  # report row 6
    df = pd.read_csv(report(rows))
    records, skipped = up.build_records(df[~up.without_size(df)])
    assert [(n, pid) for n, pid, _ in skipped] == [(6, "FP-005")] and len(records) == 28


# --- Stability-brake state: the report of the last real upload (2026-11-25) --------------------
@pytest.mark.parametrize("mode", ["full", "partial"])
def test_a_successful_upload_saves_the_report_as_the_brake_state(report, ids_file, mode):
    path = report([_row(i) for i in range(3)])
    state = _state_file(ids_file)
    assert _main(path, ids_file, "--run_mode", mode, odoo=FakeOdoo(existing=2)) == 0
    assert state.read_bytes() == path.read_bytes()
    assert [p.name for p in state.parent.iterdir()] == [state.name], "no temporary file is left"


def test_a_new_upload_replaces_the_previous_state(report, ids_file):
    state = _state_file(ids_file)
    state.parent.mkdir(parents=True)
    state.write_text("Product_ID,predicted_price\nOLD,1.0\n", encoding="utf-8")
    path = report([_row(1)])
    assert _main(path, ids_file, "--run_mode", "full", odoo=FakeOdoo(existing=1)) == 0
    assert state.read_bytes() == path.read_bytes()


def test_a_dry_run_never_writes_the_state(report, ids_file, tmp_path):
    argv = ["--input", str(report([_row(1)])), "--ids-file", str(ids_file), "--run_mode", "full",
            "--state-file", str(_state_file(ids_file)), "--dry-run", "--dry-run-output", str(tmp_path / "dry.csv")]
    assert up.main(argv, session=Untouchable()) == 0
    assert not _state_file(ids_file).exists()


def test_a_failed_create_never_writes_the_state(report, ids_file):
    """Nothing new is in Odoo: the previous state stays as it was."""
    state = _state_file(ids_file)
    state.parent.mkdir(parents=True)
    state.write_text("previous upload", encoding="utf-8")
    odoo = FakeOdoo(existing=2, fail={(SUG, "create")})
    assert _main(report([_row(1)]), ids_file, "--run_mode", "full", odoo=odoo) == 1
    assert state.read_text(encoding="utf-8") == "previous upload"


@pytest.mark.parametrize("fail", [(SUG, "read"), (HIST, "create"), (SUG, "unlink")])
def test_a_failed_cleanup_still_leaves_the_new_prices_as_the_state(report, ids_file, fail):
    """The create returned, so Odoo shows the new prices: the state is written before the cleanup
    and stays written when the cleanup fails (exit 1, IDs file kept for --cleanup-previous)."""
    state = _state_file(ids_file)
    state.parent.mkdir(parents=True)
    state.write_text("previous upload", encoding="utf-8")
    path = report([_row(1)])
    assert _main(path, ids_file, "--run_mode", "full", odoo=FakeOdoo(existing=2, fail={fail})) == 1
    assert state.read_bytes() == path.read_bytes()
    assert ids_file.exists()


def test_cleanup_previous_never_writes_the_state(report, ids_file):
    state = _state_file(ids_file)
    path = report([_row(1)])
    odoo = FakeOdoo(existing=2, fail={(SUG, "unlink")})
    assert _main(path, ids_file, "--run_mode", "full", odoo=odoo) == 1
    state.write_text("written by the upload", encoding="utf-8")
    odoo.fail.clear()
    assert _main(path, ids_file, "--cleanup-previous", odoo=odoo) == 0
    assert not ids_file.exists()
    assert state.read_text(encoding="utf-8") == "written by the upload"


def test_a_failed_state_copy_and_a_failed_cleanup_exit_1_without_a_stale_state(report, ids_file, monkeypatch):
    """Both fail: the cleanup's own error is reported (IDs file kept for --cleanup-previous) and
    no older state is left to brake against."""
    state = _state_file(ids_file)
    state.parent.mkdir(parents=True)
    state.write_text("previous upload", encoding="utf-8")
    real_replace = os.replace

    def failing_replace(src, dst):
        if str(dst) == str(state):
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(up.os, "replace", failing_replace)
    odoo = FakeOdoo(existing=2, fail={(SUG, "unlink")})
    assert _main(report([_row(1)]), ids_file, "--run_mode", "full", odoo=odoo) == 1
    assert ids_file.exists() and list(state.parent.iterdir()) == []


def test_a_failed_state_copy_keeps_the_upload_removes_the_stale_state_and_exits_1(report, ids_file, monkeypatch):
    state = _state_file(ids_file)
    state.parent.mkdir(parents=True)
    state.write_text("previous upload", encoding="utf-8")
    odoo = FakeOdoo(existing=2)
    old_ids = set(odoo.table)
    real_replace = os.replace

    def failing_replace(src, dst):
        if str(dst) == str(state):
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(up.os, "replace", failing_replace)
    assert _main(report([_row(i) for i in range(3)]), ids_file, "--run_mode", "full", odoo=odoo) == 1
    # The upload stands: the new batch is in Odoo and the previous one was archived and removed.
    assert len(odoo.table) == 3 and not set(odoo.table) & old_ids and len(odoo.history) == 2
    assert not ids_file.exists()
    # No stale state (the next run must not brake against prices that are no longer uploaded),
    # and no temporary file left behind.
    assert list(state.parent.iterdir()) == []


def test_upload_raises_state_file_error_with_the_created_ids(report, tmp_path):
    odoo = FakeOdoo(existing=0)
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    records, _ = up.build_records(pd.DataFrame([_row(1)]))
    with pytest.raises(up.StateFileError) as error:
        up.upload(odoo.session(), records, "partial", str(tmp_path / "ids.json"),
                  report_file=str(report([_row(1)])), state_file=str(blocker / "state.csv"))
    assert error.value.created_ids == sorted(odoo.table) and len(odoo.table) == 1


def test_upload_without_a_report_file_writes_no_state(tmp_path):
    odoo = FakeOdoo(existing=0)
    records, _ = up.build_records(pd.DataFrame([_row(1)]))
    state = tmp_path / "state" / "s.csv"
    up.upload(odoo.session(), records, "partial", str(tmp_path / "ids.json"), state_file=str(state))
    assert not state.parent.exists()


@pytest.mark.parametrize("value", [None, "", "{tmp}/pi-logs"])
def test_state_default_is_where_the_prediction_reads_the_previous_report(monkeypatch, tmp_path, value):
    """The upload writes the state where 3_predicted_prices.py's --previous_report looks by default:
    <log folder>/state/last_uploaded_prediction_report.csv, outside the code."""
    if value is None:
        monkeypatch.delenv("PI_LOG_DIR", raising=False)
    else:
        monkeypatch.setenv("PI_LOG_DIR", value.format(tmp=tmp_path))
    fresh = _fresh_upload_module()
    prediction = load_module("ml_model/3_predicted_prices.py")
    expected = os.path.join(fresh.log_dir(), "state", "last_uploaded_prediction_report.csv")
    assert fresh.LAST_UPLOADED_REPORT_FILE == expected == fresh.parse_args([]).state_file
    assert prediction.parse_args([]).previous_report == expected
    assert not expected.startswith(str(REPO_ROOT) + os.sep), "state never lives in the code folder"
