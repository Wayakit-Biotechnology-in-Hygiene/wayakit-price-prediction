"""Shared helpers for all Price Intelligence tests.

What it provides:
- ``REPO_ROOT`` and ``F1_DATA``: paths to the repo root and to the frozen F1 data.
- ``load_module("ml_model/2_train_models.py")``: imports a script by its path, even if the
  name starts with a digit. It does not run ``main()`` (the scripts guard it with
  ``if __name__ == "__main__"``). The module is cached: loading it twice returns the same one.
- ``in_directory(path)``: context manager that changes the cwd and restores it (the scripts in
  ``ml_model/`` use relative paths such as ``'ml_model/...'``; always run them in a tmp dir).
- Fixtures ``repo_root`` and ``f1_data`` (the latter skips the test with a clear reason if
  ``data/f1_before_2026-11-15/`` does not exist, as happens in CI).
- Hypothesis profiles: ``pi`` (default: 200 examples per test, deterministic) and
  ``exploration`` (2000 examples, random). Selected with the ``HYPOTHESIS_PROFILE`` variable.
  Tests do not set their own ``max_examples`` (the profile would override it); if one needs fewer
  examples because of cost, the justification goes in the test itself.

Locks that apply to all tests:
- Every test must carry one of the markers ``unit``, ``property``, ``e2e`` or
  ``differential``; if one is missing, pytest stops with ``UsageError`` (a test without a marker
  would never run in ``scripts/verify.sh``, which selects by marker).
- No network: inside the pytest process, ``socket.connect`` to a non-local address (anything
  other than loopback or a Unix socket) raises ``RuntimeError``. Subprocesses (E2E) are not
  covered by this lock; the one in ``scripts/verify.sh`` applies there.

Rules: no test calls AWS, Odoo, Apify or Bedrock, and none writes to the repo
(scripts that write CSVs are run in ``tmp_path``).

Importing from a test: ``from tests.conftest import load_module, REPO_ROOT``.
"""
import contextlib
import importlib.util
import ipaddress
import logging
import os
import socket
import sys
import types
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

REPO_ROOT = Path(__file__).resolve().parent.parent
F1_DATA = REPO_ROOT / "data" / "f1_before_2026-11-15"

# --- Hypothesis -------------------------------------------------------------------------------
# deadline=None: the first call into pandas takes longer than the 200 ms limit because it imports
# and prepares internal structures; that is not a defect in the code and made the tests flaky.
# pi: derandomize=True, every run uses the same examples, so a failure always reproduces
# and verify.sh does not change color between runs (the .hypothesis/ database is not written either).
# The cost: "pi" never tries new inputs; that is what "exploration" is for.
settings.register_profile(
    "pi",
    max_examples=200,
    deadline=None,
    derandomize=True,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
# To hunt for new bugs (separate run, not in the gate; takes several minutes):
#   HYPOTHESIS_PROFILE=exploration python -m pytest -m property -q
settings.register_profile(
    "exploration",
    max_examples=2000,
    deadline=None,
    derandomize=False,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "pi"))


# --- Mandatory markers ------------------------------------------------------------------------
SUITE_MARKERS = {"unit", "property", "e2e", "differential"}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Stops the run if any collected test has no suite marker.

    Runs before ``-m`` deselects anything, so it checks every collected test.
    """
    unmarked = [
        item.nodeid
        for item in items
        if not SUITE_MARKERS & {mark.name for mark in item.iter_markers()}
    ]
    if unmarked:
        raise pytest.UsageError(
            "These tests have none of the markers "
            f"{sorted(SUITE_MARKERS)} and would never run in scripts/verify.sh: "
            + ", ".join(unmarked)
            + ". Add pytestmark or @pytest.mark.<marker>."
        )


# --- Network lock -----------------------------------------------------------------------------
def _is_local_address(family, address):
    if family == getattr(socket, "AF_UNIX", object()):
        return True
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False  # a host name other than localhost: it would be resolved over the network


def _block(original):
    def connect(self, address, *args, **kwargs):
        if not _is_local_address(self.family, address):
            raise RuntimeError(
                f"Network blocked in tests: attempted connection to {address!r}. No test "
                "may call AWS, Odoo, Apify, Bedrock or any other external service."
            )
        return original(self, address, *args, **kwargs)

    return connect


@pytest.fixture(scope="session", autouse=True)
def no_network():
    """Blocks connections to non-local addresses for the whole pytest session.

    It is session-scoped (it does not use ``monkeypatch``) so Hypothesis does not treat it as a
    function-scoped fixture. It covers everything that runs inside tests and fixtures; it does not
    cover collection (module imports of the test files, which do not open connections).
    """
    originals = {name: getattr(socket.socket, name) for name in ("connect", "connect_ex")}
    for name, original in originals.items():
        setattr(socket.socket, name, _block(original))
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(socket.socket, name, original)


# --- Production logger ------------------------------------------------------------------------
def _install_test_log_config():
    """Replaces ``log_config`` with a version that writes no files.

    The real ``log_config.py`` creates ``../logs/wayakit_app.log`` outside the repo when imported.
    Tests must not write there (that log can get mixed with the production one), so
    scripts loaded with ``load_module`` get a logger without file handlers.
    """
    if "log_config" in sys.modules:
        return
    module = types.ModuleType("log_config")
    logger = logging.getLogger("wayakit_price_prediction.tests")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    module.logger = logger
    module.get_logger = lambda: logger
    module.setup_logging = lambda *args, **kwargs: logger
    sys.modules["log_config"] = module


_MODULE_CACHE = {}


def load_module(relative_path):
    """Imports a .py file from the repo by its path relative to the root and returns it."""
    if relative_path in _MODULE_CACHE:
        return _MODULE_CACHE[relative_path]
    _install_test_log_config()
    path = REPO_ROOT / relative_path
    name = "pi_test_" + relative_path.replace("/", "_").replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _MODULE_CACHE[relative_path] = module
    return module


@contextlib.contextmanager
def in_directory(path):
    """Changes the working directory and restores it on exit (Python 3.10 does not ship
    ``contextlib.chdir``). Usable inside Hypothesis tests, where ``monkeypatch`` (function
    scoped) cannot be used."""
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield Path(path)
    finally:
        os.chdir(previous)


@pytest.fixture(scope="session")
def repo_root():
    return REPO_ROOT


@pytest.fixture(scope="session")
def f1_data():
    """Folder with the frozen F1 data; skips the test if it does not exist."""
    if not F1_DATA.is_dir():
        pytest.skip(
            f"{F1_DATA.relative_to(REPO_ROOT)} does not exist (real frozen F1 data, outside "
            "git). The e2e and differential tests need it; CI will use synthetic "
            "fixtures (F4)."
        )
    return F1_DATA
