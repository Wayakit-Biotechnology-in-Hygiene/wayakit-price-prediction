"""E2E C03: the changed scripts fail loudly when their input is missing, and the upload dry run works.

Each script runs as a subprocess with `cwd` in a temporary copy of the code (`scraper/`,
`ml_model/`, `log_config.py`, as the E2E harness copies it: no `pi_secrets.py`, no `pi_odoo.py`)
and WITHOUT its input files. Before F2 they logged the error and exited 0, and the pipeline went
on with stale files. Only scripts that fail before reaching Odoo or AWS are run here: the ML
stages, the scraping-list builder (it no longer talks to Odoo) and the upload in `--dry-run`
(which must not need the secrets code at all). `filter_new_products.py` was removed with the
partial mode (single run, 2026-11-18).

The dry run is also run on the frozen F1 report (skipped only if `data/f1_before_2026-11-15/` is
missing): one record per report row, nothing written to Odoo, no previous-IDs file.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pandas as pd
import pytest

from tests.e2e import _helpers as h

pytestmark = pytest.mark.e2e

NO_INPUT = [
    ("ml_model/1a_preprocess_data.py",),
    ("ml_model/1b_preprocess_data.py", "--run_mode", "full"),
    ("ml_model/1b_preprocess_data.py", "--run_mode", "partial"),
    ("ml_model/2_train_models.py",),
    ("ml_model/3_predicted_prices.py",),
    ("scraper/odoo_api_connection_products.py", "--input_odoo_products_file", "ml_model/wayakit_products.csv",
     "--output_analysis_file", "scraper/analysis-odoo.csv"),
    ("ml_model/odoo_api_price_suggestion.py", "--dry-run", "--run_mode", "full"),
]


def _env() -> dict:
    env = h._subprocess_env()
    for k in [k for k in env if k.startswith("AWS_")]:
        env.pop(k)
    env.update(AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
               AWS_EC2_METADATA_DISABLED="true", http_proxy="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9")
    return env


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([h.python_executable(), *args], cwd=repo, env=_env(), capture_output=True,
                          text=True, timeout=300)


@pytest.fixture(scope="module")
def code_copy(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("c03") / "repo"
    h.materialize_worktree(repo)
    assert not (repo / "pi_secrets.py").exists(), "the copy must not carry the secrets module"
    return repo


def _strip_inputs(repo: Path) -> None:
    """Removes every data file the copy could have brought (only code stays)."""
    for p in list(repo.rglob("*.csv")) + list(repo.rglob("*.joblib")):
        p.unlink()


@pytest.mark.parametrize("args", NO_INPUT, ids=[" ".join(a)[:60] for a in NO_INPUT])
def test_script_without_its_input_exits_nonzero(code_copy, args):
    _strip_inputs(code_copy)
    r = _run(code_copy, *args)
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"{args[0]} exited 0 without its input\n{out[-3000:]}"
    # It must fail because of the missing input, not because the copy cannot import something.
    for wrong in ("ModuleNotFoundError", "ImportError", "NoCredentialsError", "SyntaxError"):
        assert wrong not in out, f"{args[0]} failed for the wrong reason ({wrong})\n{out[-2000:]}"


@pytest.fixture(scope="module")
def dry_run(code_copy, tmp_path_factory):
    reason = h.data_missing_reason()
    if reason:
        pytest.skip(reason)
    report = h.DATA_DIR / "ml_model" / "wayakit_prediction_report.csv"
    if not report.is_file():
        pytest.skip(f"{report.name} is missing from the frozen data")
    work = tmp_path_factory.mktemp("dry")
    out, ids = work / "dry.csv", work / "logs" / "ids.json"
    r = _run(code_copy, "ml_model/odoo_api_price_suggestion.py", "--dry-run", "--run_mode", "full",
             "--input", str(report), "--dry-run-output", str(out), "--ids-file", str(ids))
    return r, report, out, ids


def test_dry_run_on_the_frozen_report_builds_one_record_per_row_with_a_size(dry_run):
    """One record per report row, except products without a size ("0 units"), which are not
    uploaded since F7a (26 in the frozen report)."""
    r, report, out, ids = dry_run
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    df_report, df_out = pd.read_csv(report), pd.read_csv(out)
    no_size = df_report["volume"].astype(str).str.startswith("0 ")
    assert no_size.sum() == 26
    df_report = df_report[~no_size]
    assert len(df_out) == len(df_report) > 0
    assert df_out["product_id_str"].astype(str).tolist() == df_report["Product_ID"].astype(str).tolist()
    assert df_out["suggested_price"].tolist() == pytest.approx(df_report["predicted_price"].tolist())
    assert set(df_out.columns) == {
        "product_id_str", "volume_units", "product_type", "generic_product_type", "subindustry", "industry",
        "product_name", "production_cost", "suggested_price", "profit", "predicted_price_per_unit",
        "model_confidence", "market_min_found", "market_max_found", "competitors_count"}
    assert not ids.exists(), "a dry run never saves previous IDs"
    assert "Odoo not contacted" in r.stdout + r.stderr
