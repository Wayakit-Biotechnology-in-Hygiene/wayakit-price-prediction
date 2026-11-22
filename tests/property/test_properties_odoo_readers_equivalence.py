"""Equivalence of the F2-refactored Odoo readers with the base version (same records → same CSV).

F2 moved the secret read and the Odoo connection of `ml_model/odoo_api_products.py`,
`ml_model/odoo_api_competitor_products.py` and `ml_model/odoo_api_cotizations.py` into `main()`
and split the data processing into functions. The CSVs they write feed the ML flow, so the
refactor must not change a single byte for the same Odoo records.

How: the base version of each script (`PI_BASE_REF`, default `origin/server-improvement`, read
with `git show`) is executed in-process inside a temporary directory, with `boto3.session.Session`
and `xmlrpc.client.ServerProxy` replaced by fakes (fake secret, fake Odoo returning the generated
records): nothing reaches AWS or Odoo. The base can have either shape:
- pre-F2 (e.g. 8c278ca): the work happens at import time, so it runs with `runpy` as `__main__`;
- F2 and later (`def main(argv, ...)`, e.g. the base after the F3 cutover): it is imported and its
  `main(argv, session=<fake>)` is called with the same fake Odoo, exactly like the new version.
The new version runs `main(argv, session=<fake>)`. Both CSVs must be byte-identical, and both
versions must send the same `execute_kw` query (against a pre-F2 base, the sales window of
`odoo_api_cotizations.py` is the only intended difference: C12; an F2 base gets the same `today`).
Modules shared by both versions (`pi_odoo`, `log_config`, ...) come from the working tree.

If the base ref does not exist locally the test FAILS (`git fetch origin`), as the ML differential.
"""
from __future__ import annotations

import contextlib
import functools
import importlib.util
import json
import re
import runpy
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from pi_odoo import OdooSession
from tests.conftest import REPO_ROOT, in_directory, load_module
from tests.differential import _harness as dh

# The base scripts trigger SettingWithCopyWarning (pandas); it is noise from the old code, not a failure.
pytestmark = [pytest.mark.property,
              pytest.mark.filterwarnings("ignore::pandas.errors.SettingWithCopyWarning")]

FAKE_SECRET = {"ODOO_URL": "https://odoo.example.invalid", "ODOO_DB": "FAKE-db",
               "ODOO_USERNAME": "FAKE-user", "ODOO_API_TOKEN": "FAKE-token"}


@functools.lru_cache(maxsize=None)
def _base_source(rel: str) -> str:
    ref = dh.base_ref()
    r = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{ref}:{rel}"], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.fail(f"cannot read {rel} from base ref '{ref}' (run `git fetch origin` or set PI_BASE_REF): "
                    f"{r.stderr.strip()}", pytrace=False)
    return r.stdout


class FakeModels:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def execute_kw(self, db, uid, token, model, method, args, kwargs=None):
        self.calls.append((model, method, json.loads(json.dumps(args)), kwargs))
        return [dict(r) for r in self.records]


class FakeCommon:
    def version(self):
        return {"server_version": "17.0"}

    def authenticate(self, db, user, token, extra):
        return 1


class FakeSecretsClient:
    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps(FAKE_SECRET)}


class FakeBotoSession:
    def client(self, **kwargs):
        return FakeSecretsClient()


def _has_main_entry(source: str) -> bool:
    """True if the base script has the F2 shape: no import side effects, `main(argv, ...)` entry."""
    return re.search(r"^def main\(argv", source, re.MULTILINE) is not None


def _import_base(script: Path):
    """Imports a base script written to disk (F2 shape: importing it reads no secrets)."""
    spec = importlib.util.spec_from_file_location("pi_base_" + re.sub(r"\W", "_", script.stem), script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_session(models) -> OdooSession:
    return OdooSession(models=models, db="FAKE-db", uid=1, api_token="FAKE-token")


def _run_base(rel: str, records: list, workdir: Path, out_rel: str, **kwargs) -> tuple[Path, list]:
    """Runs the base script `rel` in `workdir` against fakes; returns (output CSV path, calls).

    `kwargs` (e.g. `today`) only reach an F2-shaped base; a pre-F2 base takes none.
    """
    load_module("ml_model/1a_preprocess_data.py")  # installs the file-less log_config in sys.modules
    source = _base_source(rel)
    script = workdir / rel
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(source, encoding="utf-8")
    out = workdir / out_rel
    out.parent.mkdir(parents=True, exist_ok=True)
    models = FakeModels(records)

    def server_proxy(url, *args, **kwargs):
        return FakeCommon() if url.endswith("/common") else models

    saved_path = list(sys.path)
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch("xmlrpc.client.ServerProxy", server_proxy))
        stack.enter_context(mock.patch("boto3.session.Session", FakeBotoSession))
        stack.enter_context(mock.patch.object(sys, "argv", [str(script)]))
        stack.enter_context(in_directory(workdir))
        try:
            if _has_main_entry(source):
                code = _import_base(script).main(["--output", str(out)], session=_fake_session(models), **kwargs)
                if code != 0:
                    pytest.fail(f"base {rel} returned {code!r} on valid records", pytrace=False)
            else:
                runpy.run_path(str(script), run_name="__main__")
        except SystemExit as e:  # the pre-F2 base scripts call exit() on errors
            pytest.fail(f"base {rel} exited with {e.code!r} on valid records", pytrace=False)
        finally:
            sys.path[:] = saved_path
    return out, models.calls


def _run_new(rel: str, records: list, out: Path, **kwargs) -> list:
    module = load_module(rel)
    models = FakeModels(records)
    code = module.main(["--output", str(out)], session=_fake_session(models), **kwargs)
    assert code == 0, f"new {rel} returned {code} on valid records"
    return models.calls


def _compare(rel: str, out_rel: str, records: list, **new_kwargs) -> tuple[list, list]:
    with tempfile.TemporaryDirectory(prefix="pi-odoo-eq-") as tmp:
        tmp = Path(tmp)
        base_out, base_calls = _run_base(rel, records, tmp / "base", out_rel, **new_kwargs)
        new_out = tmp / "new.csv"
        new_calls = _run_new(rel, records, new_out, **new_kwargs)
        base_bytes = base_out.read_bytes()
        new_bytes = new_out.read_bytes()
        assert new_bytes == base_bytes, (
            f"{rel}: CSV differs from base for the same records\n--- base ---\n"
            f"{base_bytes.decode('utf-8', 'replace')}\n--- new ---\n{new_bytes.decode('utf-8', 'replace')}")
    return base_calls, new_calls


# --- strategies ---------------------------------------------------------------------------------
text = st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), max_size=15)
many2one = st.one_of(st.just(False), st.tuples(st.integers(1, 999), text).map(list))
number = st.one_of(st.just(False), st.floats(min_value=0, max_value=1e4, allow_nan=False).map(
    lambda x: round(x, 4)))
odoo_datetime = st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2030, 12, 31)).map(
    lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))

products_fields = load_module("ml_model/odoo_api_products.py").FIELDS_TO_GET
_product_special = {"type_of_product_id": many2one, "subindustry_id": many2one, "industry_id": many2one,
                    "create_date": odoo_datetime}
product_record = st.fixed_dictionaries({
    "id": st.integers(1, 10**6),
    **{f: _product_special.get(f, st.one_of(text, number)) for f in products_fields},
})

competitor_record = st.fixed_dictionaries({
    "id": st.integers(1, 10**6),
    "date": st.dates(date(2020, 1, 1), date(2030, 12, 31)).map(date.isoformat),
    "product_channel": st.sampled_from(["b2b", "retail", "ecommerce", "other", False]),
    "classification_id": many2one,
    "product_category": st.one_of(text, st.just(False)),
    "subindustry_id": many2one,
    "industry_id": many2one,
    "generic_product_type": st.one_of(text, st.just(False)),
    "total_quantity": number,
    "uom": st.sampled_from(["ml", "g", "units", "kg", False]),
    "price_unit_sar": number,
    "company": st.one_of(text, st.just(False)),
})

sale_line = st.fixed_dictionaries({
    "id": st.integers(1, 10**6),
    "name": st.tuples(st.from_regex(r"FP-[0-9A-Z]{1,6}", fullmatch=True), text).map(
        lambda t: f"[{t[0]}] {t[1]}"),
    "product_uom_qty": st.floats(min_value=0.5, max_value=1e4, allow_nan=False).map(lambda x: round(x, 2)),
    "price_subtotal": st.floats(min_value=0.01, max_value=1e6, allow_nan=False).map(lambda x: round(x, 2)),
})


# --- tests --------------------------------------------------------------------------------------
@given(st.lists(product_record, min_size=1, max_size=6))
def test_products_csv_is_identical_to_base(records):
    base_calls, new_calls = _compare("ml_model/odoo_api_products.py", "ml_model/wayakit_products.csv", records)
    assert new_calls == base_calls, "the product.master query changed"


@given(st.lists(competitor_record, min_size=1, max_size=6))
@example([{"id": 1, "date": "2026-11-14", "product_channel": "retail", "classification_id": [3, "H1-Floor cleaner"],
           "product_category": "Home", "subindustry_id": [2, "Home"], "industry_id": [1, "Home care"],
           "generic_product_type": "Floor cleaner", "total_quantity": 1000.0, "uom": "ml",
           "price_unit_sar": 12.5, "company": "Fake brand"}])
def test_competitor_products_csv_is_identical_to_base(records):
    base_calls, new_calls = _compare("ml_model/odoo_api_competitor_products.py",
                                     "ml_model/competitor_products_from_odoo.csv", records)
    assert new_calls == base_calls, "the competitor.product query changed"


@given(st.lists(sale_line, min_size=1, max_size=6))
def test_sales_csv_is_identical_to_base_and_only_the_window_changes(records):
    # The fake Odoo ignores the domain, so both versions get the same lines; the only intended
    # difference is the date window (pre-F2 base: fixed 2025; new: rolling 12 months up to `today`;
    # an F2-shaped base gets the same `today`, so its window must match too).
    base_calls, new_calls = _compare("ml_model/odoo_api_cotizations.py", "ml_model/wayakit_cotizations.csv",
                                     records, today=date(2026, 11, 17))
    assert len(base_calls) == len(new_calls) == 1
    (bm, bmeth, bargs, bkw), (nm, nmeth, nargs, nkw) = base_calls[0], new_calls[0]
    assert (bm, bmeth, bkw) == (nm, nmeth, nkw)

    def without_dates(domain):
        return [c for c in domain if c[0] != "order_id.date_order"]

    assert without_dates(nargs[0]) == without_dates(bargs[0])
    assert [c for c in nargs[0] if c[0] == "order_id.date_order"] == [
        ["order_id.date_order", ">=", "2025-11-17 00:00:00"], ["order_id.date_order", "<=", "2026-11-17 23:59:59"]]


# --- scraper/odoo_api_connection_products.py (scraping list) --------------------------------------
# It no longer talks to Odoo, but the pre-F2 base read the secret and authenticated before
# building the list; with the fakes above it runs offline. An F2-shaped base is imported and its
# `main(argv)` called, like the new version. Same products + modifiers CSVs must
# give a byte-identical `analysis-odoo.csv` (it decides what gets scraped).
small = st.sampled_from(["H1-Floor cleaner", "H2-Glass cleaner", "P1-Pet wipes", "C1-Car shampoo", ""])
product_row = st.fixed_dictionaries({
    "Product_ID": st.from_regex(r"FP-[0-9]{1,4}", fullmatch=True),
    "Type_of_product": small,
    "SubIndustry": st.sampled_from(["Home", "Pets", "Land transportation", "Land Transportation"]),
    "Industry": st.sampled_from(["Home care", "Transportation"]),
    "Generic product type": st.sampled_from(["Cleaner", "Wipes", "Shampoo"]),
})
modifier_row = st.fixed_dictionaries({
    "Type of product": small,
    "Search Modifiers": st.one_of(st.just(""), st.from_regex(r"(amazon|saco|fine): ?[a-z ]{1,10}", fullmatch=True)),
})


@given(st.lists(product_row, min_size=1, max_size=8), st.lists(modifier_row, max_size=5))
@example([{"Product_ID": "FP-1", "Type_of_product": "H1-Floor cleaner", "SubIndustry": "Home",
           "Industry": "Home care", "Generic product type": "Cleaner"}],
         [{"Type of product": "H1-Floor cleaner", "Search Modifiers": "amazon: floor cleaner"}])
def test_scraping_list_is_identical_to_base(products, modifiers):
    import pandas as pd

    rel = "scraper/odoo_api_connection_products.py"
    with tempfile.TemporaryDirectory(prefix="pi-odoo-eq-") as tmp:
        tmp = Path(tmp)
        products_csv, modifiers_csv = tmp / "products.csv", tmp / "modifiers.csv"
        pd.DataFrame(products).to_csv(products_csv, index=False, encoding="utf-8-sig")
        pd.DataFrame(modifiers, columns=["Type of product", "Search Modifiers"]).to_csv(modifiers_csv, index=False)
        argv = ["--input_odoo_products_file", str(products_csv), "--modifiers_file", str(modifiers_csv)]

        base_out, new_out = tmp / "base.csv", tmp / "new.csv"
        source = _base_source(rel)
        script = tmp / "base" / rel
        script.parent.mkdir(parents=True)
        script.write_text(source, encoding="utf-8")
        load_module("ml_model/1a_preprocess_data.py")  # file-less log_config
        saved_path = list(sys.path)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("xmlrpc.client.ServerProxy", lambda url, *a, **k: FakeCommon()))
            stack.enter_context(mock.patch("boto3.session.Session", FakeBotoSession))
            stack.enter_context(mock.patch.object(sys, "argv", [str(script), *argv,
                                                                "--output_analysis_file", str(base_out)]))
            try:
                if _has_main_entry(source):
                    code = _import_base(script).main([*argv, "--output_analysis_file", str(base_out)])
                    if code != 0:
                        pytest.fail(f"base {rel} returned {code!r} on valid inputs", pytrace=False)
                else:
                    runpy.run_path(str(script), run_name="__main__")
            except SystemExit as e:
                pytest.fail(f"base {rel} exited with {e.code!r} on valid inputs", pytrace=False)
            finally:
                sys.path[:] = saved_path

        assert load_module(rel).main([*argv, "--output_analysis_file", str(new_out)]) == 0
        assert new_out.read_bytes() == base_out.read_bytes(), (
            f"scraping list differs from base\n--- base ---\n{base_out.read_text('utf-8-sig')}"
            f"\n--- new ---\n{new_out.read_text('utf-8-sig')}")
