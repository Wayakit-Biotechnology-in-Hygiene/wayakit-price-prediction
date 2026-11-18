"""Unit tests of the log folder rule in `log_config.py` (Pending 17, resolved 2026-11-18).

Rule: `LOG_DIR` is `PI_LOG_DIR` if set and non-empty (made absolute), else `<repo>/../logs`
(normalized, outside the code). `ml_model/odoo_api_price_suggestion.py` (`log_dir()`) and
`run_pipeline.sh` apply the same rule.

The real `log_config.py` creates `LOG_DIR` and `wayakit_app.log` when it is imported, so it is
NEVER imported in the pytest process: either a COPY of it runs in a subprocess with everything under
`tmp_path`, or only its `LOG_DIR` assignment is evaluated (taken from the source with `ast`, with a
fake `__file__`), which writes nothing.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.conftest import REPO_ROOT, load_module

pytestmark = pytest.mark.unit

SOURCE = REPO_ROOT / "log_config.py"
up = load_module("ml_model/odoo_api_price_suggestion.py")

_PROBE = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import log_config\n"
    "log_config.logger.info('probe line from the test')\n"
    "print(json.dumps({'LOG_DIR': log_config.LOG_DIR, 'LOG_FILENAME': log_config.LOG_FILENAME}))\n"
)


def log_dir_expression(environ: dict, file: str) -> str:
    """Evaluates ONLY the `LOG_DIR = ...` statement of log_config.py, with `__file__` = `file` and
    `os.environ` = `environ`. Nothing is created (setup_logging is not run)."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    [assign] = [n for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "LOG_DIR" for t in n.targets)]
    code = compile(ast.Module(body=[assign], type_ignores=[]), str(SOURCE), "exec")

    class _Os:  # os with a replaced environ; everything else is the real module (path functions)
        def __getattr__(self, name):
            return getattr(os, name)
    fake_os = _Os()
    fake_os.environ = environ
    namespace = {"os": fake_os, "__file__": file}
    exec(code, namespace)  # noqa: S102 - our own source, one assignment
    return namespace["LOG_DIR"]


def _run_copy(tmp_path, pi_log_dir: str | None):
    """Copies log_config.py to <tmp>/apps/repo and imports it in a subprocess launched from <tmp>/cwd."""
    code_dir = tmp_path / "apps" / "repo"
    code_dir.mkdir(parents=True)
    shutil.copy2(SOURCE, code_dir / "log_config.py")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "cwd"), "LANG": "en_US.UTF-8"}
    if pi_log_dir is not None:
        env["PI_LOG_DIR"] = pi_log_dir
    # -I: no PYTHONPATH, user site or cwd on sys.path; -B: no __pycache__ next to the copy.
    p = subprocess.run([sys.executable, "-I", "-B", "-c", _PROBE, str(code_dir)], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return code_dir, cwd, json.loads(p.stdout.strip().splitlines()[-1])


def _files_under(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_default_is_the_parent_of_the_code_plus_logs_and_the_code_folder_gets_nothing(tmp_path):
    code_dir, _, got = _run_copy(tmp_path, None)
    expected = tmp_path / "apps" / "logs"
    assert got["LOG_DIR"] == str(expected), "normalized, no '..' left"
    assert got["LOG_FILENAME"] == str(expected / "wayakit_app.log")
    assert "probe line from the test" in (expected / "wayakit_app.log").read_text(encoding="utf-8")
    assert _files_under(tmp_path) == ["apps/logs/wayakit_app.log", "apps/repo/log_config.py"]
    assert not (code_dir / "logs").exists()


def test_empty_pi_log_dir_is_the_default(tmp_path):
    _, _, got = _run_copy(tmp_path, "")
    assert got["LOG_DIR"] == str(tmp_path / "apps" / "logs")


def test_pi_log_dir_absolute_is_used_and_the_default_is_not_created(tmp_path):
    custom = tmp_path / "shared state" / "pi-logs"
    _, _, got = _run_copy(tmp_path, str(custom))
    assert got["LOG_DIR"] == str(custom)
    assert "probe line from the test" in (custom / "wayakit_app.log").read_text(encoding="utf-8")
    assert not (tmp_path / "apps" / "logs").exists()
    assert _files_under(tmp_path) == ["apps/repo/log_config.py", "shared state/pi-logs/wayakit_app.log"]


def test_pi_log_dir_relative_is_made_absolute_against_the_current_folder(tmp_path):
    _, cwd, got = _run_copy(tmp_path, "rel/../state")
    assert got["LOG_DIR"] == str(cwd / "state")
    assert (cwd / "state" / "wayakit_app.log").is_file()


def test_the_real_repo_default_is_outside_the_repo_computed_without_writing(monkeypatch):
    """The path the real module would use, from its source; nothing is created."""
    monkeypatch.delenv("PI_LOG_DIR", raising=False)
    got = log_dir_expression({}, str(SOURCE))
    assert got == os.path.join(os.path.dirname(str(REPO_ROOT)), "logs")
    assert not got.startswith(str(REPO_ROOT) + os.sep)


@pytest.mark.parametrize("environ", [{}, {"PI_LOG_DIR": ""}, {"PI_LOG_DIR": "/srv/pi/logs"},
                                     {"PI_LOG_DIR": "/srv/pi/x/../logs/"}],
                         ids=["unset", "empty", "absolute", "unnormalized"])
def test_log_config_and_the_upload_ids_folder_agree_on_the_real_repo(environ):
    assert log_dir_expression(environ, str(SOURCE)) == up.log_dir(environ, up.PROJECT_DIR)
