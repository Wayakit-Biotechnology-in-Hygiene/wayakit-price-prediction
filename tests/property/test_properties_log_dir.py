"""Property tests of the single log folder rule (Pending 17, resolved 2026-11-18).

`log_dir(environ, project_dir)` in `ml_model/odoo_api_price_suggestion.py` and `LOG_DIR` in
`log_config.py` (only its assignment is evaluated, from the source, so nothing is written):

- invariant: the result is always absolute and normalized;
- an empty `PI_LOG_DIR` is the same as an unset one;
- any non-empty value gives `abspath(value)`;
- the default is `<parent of the project>/logs` and never lies inside the code folder;
- idempotence: exporting the result as `PI_LOG_DIR` (what run_pipeline.sh does for its Python
  steps) gives the same folder, whatever the project folder the step sees (F4: `current` symlink
  vs `releases/<sha>`);
- equivalence: log_config.py and the upload script give the same folder for the same layout.
"""
from __future__ import annotations

import ast
import os
import string

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from tests.conftest import REPO_ROOT, load_module

pytestmark = pytest.mark.property

up = load_module("ml_model/odoo_api_price_suggestion.py")
SOURCE = REPO_ROOT / "log_config.py"


def _log_config_log_dir(environ: dict, file: str) -> str:
    """Evaluates ONLY `LOG_DIR = ...` from log_config.py with a fake `__file__` and `os.environ`."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    [assign] = [n for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "LOG_DIR" for t in n.targets)]
    code = compile(ast.Module(body=[assign], type_ignores=[]), str(SOURCE), "exec")

    class _Os:
        def __getattr__(self, name):
            return getattr(os, name)
    fake_os = _Os()
    fake_os.environ = environ
    namespace = {"os": fake_os, "__file__": file}
    exec(code, namespace)  # noqa: S102 - our own source, one assignment
    return namespace["LOG_DIR"]


SEGMENT = st.text(alphabet=string.ascii_letters + string.digits + "-_ .", min_size=1, max_size=8) \
    .filter(lambda s: s not in {".", ".."})
PROJECT = st.builds(lambda segs, slash: "/" + "/".join(segs) + ("/" if slash else ""),
                    st.lists(SEGMENT, min_size=1, max_size=4), st.booleans())
ANY_SEGMENT = st.one_of(SEGMENT, st.just("."), st.just(".."))
ABSOLUTE = st.builds(lambda segs: "/" + "/".join(segs), st.lists(ANY_SEGMENT, max_size=5))
RELATIVE = st.builds(lambda segs: "/".join(segs), st.lists(ANY_SEGMENT, min_size=1, max_size=4))
VALUE = st.one_of(ABSOLUTE, RELATIVE, st.text(min_size=1, max_size=12).filter(lambda s: "\x00" not in s))
ENVIRON = st.one_of(st.just({}), st.just({"PI_LOG_DIR": ""}), VALUE.map(lambda v: {"PI_LOG_DIR": v}))


@given(ENVIRON, PROJECT)
@example({"PI_LOG_DIR": "/srv/pi/x/../logs/"}, "/srv/pi/current")
@example({}, "/srv/pi/releases/abc123/")
def test_result_is_always_absolute_and_normalized(environ, project):
    got = up.log_dir(environ, project)
    assert os.path.isabs(got) and got == os.path.normpath(got)


@given(PROJECT)
def test_empty_is_the_same_as_unset(project):
    assert up.log_dir({"PI_LOG_DIR": ""}, project) == up.log_dir({}, project)


@given(VALUE, PROJECT)
def test_any_non_empty_value_wins_and_is_made_absolute(value, project):
    assert up.log_dir({"PI_LOG_DIR": value}, project) == os.path.abspath(value)


@given(PROJECT)
@example("/home/ec2-user/wayakit-price-prediction")
def test_default_is_parent_plus_logs_and_never_inside_the_code(project):
    real = os.path.normpath(project)
    # A code folder literally named "logs" would BE its own default; not a layout PI uses.
    assume(os.path.basename(real) != "logs")
    got = up.log_dir({}, project)
    assert got == os.path.join(os.path.dirname(real), "logs")
    assert got != real and not got.startswith(real + os.sep)


@given(ENVIRON, PROJECT, PROJECT)
@example({}, "/srv/pi/current", "/srv/pi/releases/abc123")
def test_exporting_the_result_is_idempotent_whatever_folder_the_step_sees(environ, project, other):
    first = up.log_dir(environ, project)
    assert up.log_dir({"PI_LOG_DIR": first}, other) == first


@given(ENVIRON, PROJECT)
@example({}, str(REPO_ROOT))
def test_log_config_and_the_upload_script_give_the_same_folder(environ, project):
    """log_config.py sits at the repo root; the upload script derives the root from ml_model/."""
    file = os.path.join(project, "log_config.py")
    assert _log_config_log_dir(environ, file) == up.log_dir(environ, project)
