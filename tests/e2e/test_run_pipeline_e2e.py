"""E2E of `run_pipeline.sh` (stages 1 and 2) in a temporary copy with fake commands (shims).

See `tests/e2e/_pipeline_sandbox.py`: the real script never runs in its place, `python`, `timeout`,
`git`, `conda`, `aws`, `sudo` and `shutdown` are shims that only record, the `PATH` holds nothing
but those shims and a few network-free utilities, and the run refuses to start if `sudo` or
`shutdown` (the `trap EXIT` does `sudo shutdown -h +1`) do not resolve to their shim. There are no
calls to AWS, Odoo, Apify or Bedrock, and no real shutdown. It does not need the F1 data.

What it documents:

- Today (passing): the order of the steps of each stage, that stage 1 aborts if a step fails and
  that the shutdown always goes through the shim.
- The desired behavior (strict xfail, bug C01 of `docs/reliability-checklist.md`): if scraping
  fails or the `timeout` expires, stage 2 must NOT call `odoo_api_price_suggestion.py` and
  must exit with a nonzero code. Today it does call it and exits with 0 (run of 13–15 Nov 2026).
  When `pi-dev` fixes it, the xfail will turn into XPASS and `xfail_strict` will make the suite
  fail: that is when the mark is removed.
"""
from __future__ import annotations

import re

import pytest

from tests.e2e import _pipeline_sandbox as sb

pytestmark = pytest.mark.e2e

UPLOAD = "ml_model/odoo_api_price_suggestion.py"
SCRAPER = "scraper/main.py"
TIMEOUT_EXIT = 124  # what `timeout` returns when it expires

STAGE1 = ["ml_model/odoo_api_products.py", "scraper/odoo_api_connection_products.py"]
STAGE2 = {
    "full": [SCRAPER, "ml_model/odoo_api_cotizations.py", "ml_model/odoo_api_competitor_products.py",
             "ml_model/1a_preprocess_data.py", "ml_model/1b_preprocess_data.py",
             "ml_model/2_train_models.py", "ml_model/3_predicted_prices.py", UPLOAD],
    "partial": ["scraper/odoo_api_connection_products.py", SCRAPER, "ml_model/odoo_api_cotizations.py",
                "ml_model/odoo_api_competitor_products.py", "ml_model/1a_preprocess_data.py",
                "ml_model/1b_preprocess_data.py", "ml_model/2_train_models.py",
                "ml_model/3_predicted_prices.py", UPLOAD],
}
SCRAPER_TIMEOUT = {"full": "30h", "partial": "4h"}


@pytest.fixture
def sandbox(tmp_path):
    return sb.Sandbox(tmp_path / "pi-run-pipeline")


def _stage2_state(sandbox: sb.Sandbox, mode: str) -> None:
    """State that stage 1 leaves in production: mode, Odoo products and (partial) new products."""
    sandbox.write("logs/run_mode.txt", f"{mode}\n")
    sandbox.write("ml_model/wayakit_products.csv", "Product_ID\nP1\n")
    if mode == "partial":
        sandbox.write("ml_model/wayakit_new_products_temp.csv", "Product_ID\nP2\nP3\n")


def _no_real_effects(run: sb.SandboxRun) -> None:
    """Everything that could leave the machine went through a shim and nothing was left unresolved."""
    assert not run.of("aws"), run.trace()
    assert [c.args for c in run.of("sudo")] == ["shutdown -h +1"], run.trace()
    assert not run.of("shutdown"), "the sudo shim executes nothing: shutdown must not be called directly"
    assert all(c.cwd == str(run.project) for c in run.of("python")), run.trace()
    output = run.stdout + run.stderr + run.pipeline_log
    assert "command not found" not in output and "No such file" not in output, run.trace()
    assert not any(run.project.rglob("*.py")), "the copy must not contain code"


# ---------------------------------------------------------------------------------------------
# Shim lock
# ---------------------------------------------------------------------------------------------
def test_shims_resolve_first_and_sudo_shutdown_are_present(sandbox):
    sandbox.check_shims(sandbox.env())  # does not raise


def test_refuses_to_run_if_sudo_is_not_the_shim(sandbox):
    # With the system PATH first, sudo/shutdown/git resolve to the real ones: it must not run.
    env = sandbox.env(path=f"{sb.SYSTEM_PATH}:/sbin:/usr/sbin:{sandbox.shims}")
    with pytest.raises(sb.ShimNotProtecting, match="sudo"):
        sandbox.check_shims(env)
    # In case the lock failed: here the copy is a harmless script that leaves a marker.
    marker = sandbox.root / "ran.txt"
    (sandbox.project / sb.SCRIPT).write_text(f"#!/bin/bash\necho yes > '{marker}'\n")
    sandbox.env = lambda fail=None, path=None: env  # type: ignore[method-assign]
    with pytest.raises(sb.ShimNotProtecting):
        sandbox.run(stage=1)
    assert not marker.exists() and not sandbox.log.exists(), "nothing should have run"


def test_refuses_to_run_if_a_shim_is_missing(sandbox):
    (sandbox.shims / "shutdown").unlink()
    with pytest.raises(sb.ShimNotProtecting, match="shutdown"):
        sandbox.run(stage=1)


def test_python_shim_can_fail_a_step(sandbox):
    """The shim's failure control works (otherwise the C01 xfail would prove nothing)."""
    _stage2_state(sandbox, "full")
    run = sandbox.run(stage=2, fail={SCRAPER: TIMEOUT_EXIT})
    assert f"shim python: {SCRAPER} -> exit {TIMEOUT_EXIT}" in run.stdout, run.trace()
    # run_command logs a failed step at ERROR level; only the level token is checked, not the wording.
    assert re.search(r"\bERROR\b", run.pipeline_log), run.trace()
    _no_real_effects(run)


# ---------------------------------------------------------------------------------------------
# Today's behavior (correct)
# ---------------------------------------------------------------------------------------------
def test_stage1_runs_its_steps_and_shuts_down_through_the_shim(sandbox):
    run = sandbox.run(stage=1)
    assert run.returncode == 0, run.trace()
    assert run.python_scripts == STAGE1, run.trace()
    assert [c.args for c in run.of("conda")][:2] == ["info --base", "activate wayakit_env"], run.trace()
    assert (run.project / "logs" / "run_mode.txt").read_text().strip() == "full"
    assert (run.project / "logs" / "last_full_run.txt").is_file()
    assert not run.of("git"), "stage 1 does not do git pull"
    _no_real_effects(run)


def test_stage1_aborts_if_the_odoo_read_fails_and_still_shuts_down(sandbox):
    run = sandbox.run(stage=1, fail={STAGE1[0]: 1})
    assert run.returncode == 1, run.trace()
    assert run.python_scripts == STAGE1[:1], run.trace()
    assert not (run.project / "logs" / "run_mode.txt").exists()
    _no_real_effects(run)


@pytest.mark.parametrize("mode", ["full", "partial"])
def test_stage2_without_failures_runs_every_step_in_order(sandbox, mode):
    _stage2_state(sandbox, mode)
    run = sandbox.run(stage=2)
    assert run.returncode == 0, run.trace()
    assert run.python_scripts == STAGE2[mode], run.trace()
    assert [c.args for c in run.of("git")] == ["pull"], run.trace()
    timeouts = [c.args for c in run.of("timeout")]
    assert f"{SCRAPER_TIMEOUT[mode]} python {SCRAPER}" in [t.split(" --")[0] for t in timeouts], run.trace()
    upload = [c.args for c in run.of("python") if c.script == UPLOAD]
    assert upload == [f"{UPLOAD} --run_mode {mode}"], "the upload only reached the shim"
    _no_real_effects(run)


def test_stage2_without_run_mode_exits_with_1_and_shuts_down(sandbox):
    run = sandbox.run(stage=2)
    assert run.returncode == 1, run.trace()
    assert run.python_scripts == [] and not run.of("git"), run.trace()
    _no_real_effects(run)


# ---------------------------------------------------------------------------------------------
# The desired behavior: bug C01 (strict xfail, finding for pi-dev)
# ---------------------------------------------------------------------------------------------
class BrokenPrecondition(RuntimeError):
    """The failure was not injected: this is not the expected xfail (raises=AssertionError does not cover it)."""


def _failed_scraping(sandbox: sb.Sandbox, mode: str, code: int) -> sb.SandboxRun:
    _stage2_state(sandbox, mode)
    run = sandbox.run(stage=2, fail={SCRAPER: code})
    if f"shim python: {SCRAPER} -> exit {code}" not in run.stdout:
        raise BrokenPrecondition("scraping did not fail as requested:\n" + run.trace())
    print(run.trace())
    return run


C01 = ("BUG C01 (docs/reliability-checklist.md): run_pipeline.sh does not abort when scraping "
       "fails or the timeout expires and still trains and uploads to Odoo (run of 13-15 Nov 2026). "
       "Finding for pi-dev: add `|| exit 1` (or equivalent) to every step of stage 2.")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=C01)
@pytest.mark.parametrize("mode", ["full", "partial"])
@pytest.mark.parametrize("code", [TIMEOUT_EXIT, 1], ids=["timeout", "error"])
def test_failed_scraping_does_not_call_the_odoo_upload(sandbox, mode, code):
    run = _failed_scraping(sandbox, mode, code)
    assert UPLOAD not in run.python_scripts, (
        f"with failed scraping (exit {code}) {UPLOAD} was called\n{run.trace()}")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=C01)
@pytest.mark.parametrize("mode", ["full", "partial"])
def test_expired_scraping_makes_it_exit_with_nonzero_code(sandbox, mode):
    run = _failed_scraping(sandbox, mode, TIMEOUT_EXIT)
    assert run.returncode != 0, f"stage 2 exited with 0 after a scraping timeout\n{run.trace()}"
