"""Unit tests of the default timestamp file of `ml_model/filter_new_products.py` (Pending 17).

Contract (pi-dev, 2026-11-18): `DEFAULT_TIMESTAMP_FILE` is `last_full_run.txt` inside the single log
and state folder, with the same rule as `log_config.LOG_DIR` and `odoo_api_price_suggestion.log_dir()`:
`PI_LOG_DIR` if set and non-empty (made absolute), else `<repo>/../logs`. It is always absolute, never
inside the code folder and never relative to the current folder. `--timestamp` still overrides it.

Importing the module runs `log_config` (which creates its folder and `wayakit_app.log`), so the
module is never imported in the pytest process: a COPY of `log_config.py` and the script runs in a
subprocess with everything under `tmp_path`, launched from a different current folder.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pandas as pd
import pytest

from tests.conftest import REPO_ROOT, load_module

pytestmark = pytest.mark.unit

up = load_module("ml_model/odoo_api_price_suggestion.py")
SCRIPT = "ml_model/filter_new_products.py"

_PROBE = (
    "import importlib.util, json, sys\n"
    "repo = sys.argv[1]\n"
    "spec = importlib.util.spec_from_file_location('fnp', repo + '/ml_model/filter_new_products.py')\n"
    "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
    "import log_config\n"
    "print(json.dumps({'DEFAULT_TIMESTAMP_FILE': m.DEFAULT_TIMESTAMP_FILE,"
    " 'LOG_DIR': log_config.LOG_DIR}))\n"
)


def _layout(tmp_path):
    """<tmp>/apps/repo holds a copy of the code; <tmp>/cwd is where the process is launched."""
    code_dir = tmp_path / "apps" / "repo"
    (code_dir / "ml_model").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "log_config.py", code_dir / "log_config.py")
    shutil.copy2(REPO_ROOT / SCRIPT, code_dir / SCRIPT)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    return code_dir, cwd


def _env(tmp_path, pi_log_dir: str | None) -> dict:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "cwd"), "LANG": "en_US.UTF-8"}
    if pi_log_dir is not None:
        env["PI_LOG_DIR"] = pi_log_dir
    return env


def _probe(tmp_path, pi_log_dir: str | None):
    code_dir, cwd = _layout(tmp_path)
    # -I: no PYTHONPATH, user site or cwd on sys.path; -B: no __pycache__ next to the copy.
    p = subprocess.run([sys.executable, "-I", "-B", "-c", _PROBE, str(code_dir)], cwd=cwd,
                       env=_env(tmp_path, pi_log_dir), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return code_dir, cwd, json.loads(p.stdout.strip().splitlines()[-1])


def test_default_is_in_the_parent_of_the_code_and_agrees_with_log_config_and_the_upload(tmp_path):
    code_dir, _, got = _probe(tmp_path, None)
    expected = tmp_path / "apps" / "logs" / "last_full_run.txt"
    assert got["DEFAULT_TIMESTAMP_FILE"] == str(expected), "absolute and normalized, no '..' left"
    assert got["DEFAULT_TIMESTAMP_FILE"] == os.path.join(got["LOG_DIR"], "last_full_run.txt")
    assert got["DEFAULT_TIMESTAMP_FILE"] == os.path.join(up.log_dir({}, str(code_dir)), "last_full_run.txt")
    assert not got["DEFAULT_TIMESTAMP_FILE"].startswith(str(code_dir) + os.sep)


def test_empty_pi_log_dir_is_the_default(tmp_path):
    _, _, got = _probe(tmp_path, "")
    assert got["DEFAULT_TIMESTAMP_FILE"] == str(tmp_path / "apps" / "logs" / "last_full_run.txt")


def test_pi_log_dir_absolute_is_used(tmp_path):
    custom = tmp_path / "shared state" / "pi-logs"
    code_dir, _, got = _probe(tmp_path, str(custom / "x" / ".." ))
    assert got["DEFAULT_TIMESTAMP_FILE"] == str(custom / "last_full_run.txt")
    assert got["DEFAULT_TIMESTAMP_FILE"] == os.path.join(got["LOG_DIR"], "last_full_run.txt")
    assert got["DEFAULT_TIMESTAMP_FILE"] == os.path.join(
        up.log_dir({"PI_LOG_DIR": str(custom / "x" / "..")}, str(code_dir)), "last_full_run.txt")


def test_pi_log_dir_relative_is_made_absolute_against_the_current_folder(tmp_path):
    _, cwd, got = _probe(tmp_path, "rel/../state")
    assert got["DEFAULT_TIMESTAMP_FILE"] == str(cwd / "state" / "last_full_run.txt")


def _products_csv(path):
    pd.DataFrame({
        "default_code": ["OLD-1", "MID-1", "NEW-1"],
        "create_date": ["2026-01-01 00:00:00", "2026-06-01 00:00:00", "2026-11-01 00:00:00"],
    }).to_csv(path, index=False)


def test_without_timestamp_flag_the_script_reads_the_log_folder_not_the_current_folder(tmp_path):
    """Before the fix the default was `logs/last_full_run.txt` relative to the cwd: a decoy there with
    an older timestamp would have let MID-1 through."""
    code_dir, cwd = _layout(tmp_path)
    log_dir = tmp_path / "apps" / "logs"
    log_dir.mkdir()
    (log_dir / "last_full_run.txt").write_text("2026-07-01 00:00:00\n", encoding="utf-8")
    (cwd / "logs").mkdir()
    (cwd / "logs" / "last_full_run.txt").write_text("2025-01-01 00:00:00\n", encoding="utf-8")
    _products_csv(cwd / "products.csv")
    p = subprocess.run([sys.executable, "-I", "-B", str(code_dir / SCRIPT),
                        "--input", "products.csv", "--output", "new.csv"],
                       cwd=cwd, env=_env(tmp_path, None), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert pd.read_csv(cwd / "new.csv")["default_code"].tolist() == ["NEW-1"]
    assert not (code_dir / "logs").exists()


def test_timestamp_flag_still_overrides_the_default(tmp_path):
    code_dir, cwd = _layout(tmp_path)
    log_dir = tmp_path / "apps" / "logs"
    log_dir.mkdir()
    (log_dir / "last_full_run.txt").write_text("2026-12-01 00:00:00\n", encoding="utf-8")
    (cwd / "ts.txt").write_text("2026-03-01 00:00:00\n", encoding="utf-8")
    _products_csv(cwd / "products.csv")
    p = subprocess.run([sys.executable, "-I", "-B", str(code_dir / SCRIPT), "--input", "products.csv",
                        "--timestamp", "ts.txt", "--output", "new.csv"],
                       cwd=cwd, env=_env(tmp_path, None), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert pd.read_csv(cwd / "new.csv")["default_code"].tolist() == ["MID-1", "NEW-1"]


def test_missing_default_timestamp_file_exits_1(tmp_path):
    """The moved default must not turn a missing file into a silent success."""
    code_dir, cwd = _layout(tmp_path)
    _products_csv(cwd / "products.csv")
    p = subprocess.run([sys.executable, "-I", "-B", str(code_dir / SCRIPT),
                        "--input", "products.csv", "--output", "new.csv"],
                       cwd=cwd, env=_env(tmp_path, None), capture_output=True, text=True, timeout=60)
    assert p.returncode == 1
    assert not (cwd / "new.csv").exists()
