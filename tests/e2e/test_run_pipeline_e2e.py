"""E2E of `run_pipeline.sh` (stages 1 and 2) in a temporary copy with fake commands (shims).

See `tests/e2e/_pipeline_sandbox.py`: the real script never runs in its place, `python`, `timeout`,
`git`, `conda`, `aws`, `sudo` and `shutdown` are shims that only record, the `PATH` holds nothing
but those shims and a few network-free utilities, and the run refuses to start if `sudo` or
`shutdown` (the `trap EXIT` does `sudo shutdown -h +1`) do not resolve to their shim. There are no
calls to AWS, Odoo, Apify or Bedrock, and no real shutdown. It does not need the F1 data.

What it checks:

- The order of the steps of each stage, that stage 1 aborts if a step fails and that the
  shutdown always goes through the shim.
- Stage 2 does not run `git pull` (F2: it conflicted with `analysis-odoo.csv`, rewritten by
  stage 1; code reaches the instance by a manual pull via SSM until F4).
- C01 (`docs/reliability-checklist.md`): if ANY step of stage 2 fails (scraping, its `timeout`,
  an Odoo read, preprocessing, training, prediction), the script stops right there, never calls
  `odoo_api_price_suggestion.py` and exits with a nonzero code (run of 13-15 Nov 2026: scraping
  hit the 30 h timeout and the upload still happened). A failing upload also exits nonzero.
- C02: stage 2 does not delete `scraper/competitors_complete.csv` before scraping (`scraper/main.py`
  writes to a temporary file and replaces the CSV only on success), so a failed run leaves the
  previous CSV in place.
- An unknown run mode exits with 1 without running anything.
- Log folder (Pending 17, resolved 2026-11-18): `pipeline_execution.log`, `run_mode.txt` and
  `last_full_run.txt` live in ONE folder outside the code: `PI_LOG_DIR` if set and non-empty,
  else `<project>/../logs` (here `<sandbox root>/logs`). Nothing is written under the code folder,
  stage 2 reads `run_mode.txt` from that same folder, and the folder is exported to the Python
  steps (`PI_LOG_DIR`) so the upload's IDs file lands next to the other state files.

Note: the harness property `SandboxRun.pipeline_log` still reads the old `<project>/logs` path
(the harness is a check and is not edited here); these tests read the log through `_pipeline_log`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.e2e import _pipeline_sandbox as sb

pytestmark = pytest.mark.e2e

UPLOAD = "ml_model/odoo_api_price_suggestion.py"
SCRAPER = "scraper/main.py"
COMPETITORS = "scraper/competitors_complete.csv"
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
# Every stage-2 step before the upload, per mode: (mode, script) pairs for the parametrization.
STEPS_BEFORE_UPLOAD = [(mode, script) for mode, steps in STAGE2.items() for script in steps[:-1]]
OLD_CSV = "date,product\n2026-11-14,previous run row\n"


@pytest.fixture
def sandbox(tmp_path):
    return sb.Sandbox(tmp_path / "pi-run-pipeline")


def _default_log_dir(project: Path) -> Path:
    """The default log folder of run_pipeline.sh: <project>/../logs (= <sandbox root>/logs)."""
    return project.parent / "logs"


def _write_state(sandbox: sb.Sandbox, name: str, text: str, log_dir: Path | None = None) -> Path:
    """Writes a state file in the log folder (outside the code copy, so not via `sandbox.write`)."""
    p = (log_dir or _default_log_dir(sandbox.project)) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _pipeline_log(run: sb.SandboxRun, log_dir: Path | None = None) -> str:
    """pipeline_execution.log from the log folder (the harness's `run.pipeline_log` reads the old path)."""
    p = (log_dir or _default_log_dir(run.project)) / "pipeline_execution.log"
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def _stage2_state(sandbox: sb.Sandbox, mode: str, log_dir: Path | None = None) -> None:
    """State that stage 1 leaves in production: mode, Odoo products and (partial) new products."""
    _write_state(sandbox, "run_mode.txt", f"{mode}\n", log_dir)
    sandbox.write("ml_model/wayakit_products.csv", "Product_ID\nP1\n")
    if mode == "partial":
        sandbox.write("ml_model/wayakit_new_products_temp.csv", "Product_ID\nP2\nP3\n")


def _no_real_effects(run: sb.SandboxRun, log_dir: Path | None = None) -> None:
    """Everything that could leave the machine went through a shim and nothing was left unresolved."""
    assert not run.of("aws"), run.trace()
    assert [c.args for c in run.of("sudo")] == ["shutdown -h +1"], run.trace()
    assert not run.of("shutdown"), "the sudo shim executes nothing: shutdown must not be called directly"
    assert all(c.cwd == str(run.project) for c in run.of("python")), run.trace()
    output = run.stdout + run.stderr + _pipeline_log(run, log_dir)
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
    """The shim's failure control works (otherwise the C01 tests would prove nothing)."""
    _stage2_state(sandbox, "full")
    run = sandbox.run(stage=2, fail={SCRAPER: TIMEOUT_EXIT})
    assert f"shim python: {SCRAPER} -> exit {TIMEOUT_EXIT}" in run.stdout, run.trace()
    # run_command logs a failed step at ERROR level; only the level token is checked, not the wording.
    assert re.search(r"\bERROR\b", _pipeline_log(run)), run.trace()
    _no_real_effects(run)


# ---------------------------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------------------------
def test_stage1_runs_its_steps_and_shuts_down_through_the_shim(sandbox):
    run = sandbox.run(stage=1)
    assert run.returncode == 0, run.trace()
    assert run.python_scripts == STAGE1, run.trace()
    assert [c.args for c in run.of("conda")][:2] == ["info --base", "activate wayakit_env"], run.trace()
    assert (_default_log_dir(run.project) / "run_mode.txt").read_text().strip() == "full"
    assert (_default_log_dir(run.project) / "last_full_run.txt").is_file()
    assert not run.of("git"), "stage 1 does not do git pull"
    _no_real_effects(run)


def test_stage1_aborts_if_the_odoo_read_fails_and_still_shuts_down(sandbox):
    run = sandbox.run(stage=1, fail={STAGE1[0]: 1})
    assert run.returncode == 1, run.trace()
    assert run.python_scripts == STAGE1[:1], run.trace()
    assert not (_default_log_dir(run.project) / "run_mode.txt").exists()
    _no_real_effects(run)


@pytest.mark.parametrize("mode", ["full", "partial"])
def test_stage2_without_failures_runs_every_step_in_order(sandbox, mode):
    _stage2_state(sandbox, mode)
    run = sandbox.run(stage=2)
    assert run.returncode == 0, run.trace()
    assert run.python_scripts == STAGE2[mode], run.trace()
    timeouts = [c.args for c in run.of("timeout")]
    assert f"{SCRAPER_TIMEOUT[mode]} python {SCRAPER}" in [t.split(" --")[0] for t in timeouts], run.trace()
    upload = [c.args for c in run.of("python") if c.script == UPLOAD]
    assert upload == [f"{UPLOAD} --run_mode {mode}"], "the upload only reached the shim"
    _no_real_effects(run)


@pytest.mark.parametrize("mode", ["full", "partial"])
def test_stage2_does_not_run_git_pull(sandbox, mode):
    """F2: no `git pull` in stage 2 (it conflicted with analysis-odoo.csv rewritten by stage 1)."""
    _stage2_state(sandbox, mode)
    run = sandbox.run(stage=2)
    assert run.returncode == 0, run.trace()
    assert not run.of("git"), f"stage 2 called git: {[c.args for c in run.of('git')]}\n{run.trace()}"
    _no_real_effects(run)


def test_stage2_without_run_mode_exits_with_1_and_shuts_down(sandbox):
    run = sandbox.run(stage=2)
    assert run.returncode == 1, run.trace()
    assert run.python_scripts == [] and not run.of("git"), run.trace()
    _no_real_effects(run)


def test_stage2_with_unknown_run_mode_exits_with_1_without_running_anything(sandbox):
    _stage2_state(sandbox, "full")
    _write_state(sandbox, "run_mode.txt", "weekly\n")
    run = sandbox.run(stage=2)
    assert run.returncode == 1, run.trace()
    assert run.python_scripts == [], run.trace()
    _no_real_effects(run)


def test_stage2_partial_without_new_products_runs_nothing_and_exits_0(sandbox):
    _stage2_state(sandbox, "partial")
    sandbox.write("ml_model/wayakit_new_products_temp.csv", "Product_ID\n")
    run = sandbox.run(stage=2)
    assert run.returncode == 0, run.trace()
    assert run.python_scripts == [], run.trace()
    _no_real_effects(run)


# ---------------------------------------------------------------------------------------------
# C01: a failing step stops stage 2 before the Odoo upload
# ---------------------------------------------------------------------------------------------
class BrokenPrecondition(RuntimeError):
    """The failure was not injected: the test would prove nothing."""


def _failed_step(sandbox: sb.Sandbox, mode: str, script: str, code: int) -> sb.SandboxRun:
    _stage2_state(sandbox, mode)
    sandbox.write(COMPETITORS, OLD_CSV)
    run = sandbox.run(stage=2, fail={script: code})
    if f"shim python: {script} -> exit {code}" not in run.stdout:
        raise BrokenPrecondition(f"{script} did not fail as requested:\n" + run.trace())
    print(run.trace())
    return run


@pytest.mark.parametrize("mode", ["full", "partial"])
@pytest.mark.parametrize("code", [TIMEOUT_EXIT, 1], ids=["timeout", "error"])
def test_failed_scraping_does_not_call_the_odoo_upload(sandbox, mode, code):
    run = _failed_step(sandbox, mode, SCRAPER, code)
    assert UPLOAD not in run.python_scripts, (
        f"with failed scraping (exit {code}) {UPLOAD} was called\n{run.trace()}")
    _no_real_effects(run)


@pytest.mark.parametrize("mode", ["full", "partial"])
def test_expired_scraping_makes_it_exit_with_nonzero_code(sandbox, mode):
    run = _failed_step(sandbox, mode, SCRAPER, TIMEOUT_EXIT)
    assert run.returncode != 0, f"stage 2 exited with 0 after a scraping timeout\n{run.trace()}"


@pytest.mark.parametrize("mode, script", STEPS_BEFORE_UPLOAD,
                         ids=[f"{m}-{s.split('/')[-1]}" for m, s in STEPS_BEFORE_UPLOAD])
def test_any_failing_step_stops_stage2_right_there_and_before_the_upload(sandbox, mode, script):
    """C01 for every step, not only scraping (e.g. `2_train_models.py` failing in the middle)."""
    run = _failed_step(sandbox, mode, script, 1)
    steps = STAGE2[mode]
    assert run.python_scripts == steps[: steps.index(script) + 1], (
        f"after {script} failed, the pipeline kept running\n{run.trace()}")
    assert UPLOAD not in run.python_scripts, run.trace()
    assert run.returncode != 0, run.trace()
    _no_real_effects(run)


@pytest.mark.parametrize("mode", ["full", "partial"])
def test_failed_upload_makes_it_exit_with_nonzero_code(sandbox, mode):
    run = _failed_step(sandbox, mode, UPLOAD, 1)
    assert run.python_scripts == STAGE2[mode], run.trace()
    assert run.returncode != 0, f"stage 2 exited with 0 after the upload failed\n{run.trace()}"
    _no_real_effects(run)


# ---------------------------------------------------------------------------------------------
# C02: stage 2 never deletes the previous competitors CSV
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["full", "partial"])
@pytest.mark.parametrize("fail", [None, SCRAPER], ids=["ok", "scraping-fails"])
def test_stage2_keeps_the_previous_competitors_csv(sandbox, mode, fail):
    """The shim writes nothing, so the CSV can only disappear if run_pipeline.sh deletes it."""
    _stage2_state(sandbox, mode)
    csv = sandbox.write(COMPETITORS, OLD_CSV)
    run = sandbox.run(stage=2, fail={fail: 1} if fail else None)
    assert csv.is_file(), f"stage 2 deleted {COMPETITORS}\n{run.trace()}"
    assert csv.read_text(encoding="utf-8") == OLD_CSV, run.trace()
    _no_real_effects(run)


def test_stage2_partial_without_the_new_products_file_exits_with_1(sandbox):
    _stage2_state(sandbox, "partial")
    (sandbox.project / "ml_model" / "wayakit_new_products_temp.csv").unlink()
    run = sandbox.run(stage=2)
    assert run.returncode == 1, run.trace()
    assert run.python_scripts == [], run.trace()
    assert [c.args for c in run.of("sudo")] == ["shutdown -h +1"], run.trace()
    assert not run.of("aws"), run.trace()


@pytest.mark.parametrize("mode", ["full", "partial"])
def test_missing_products_file_is_refetched_and_its_failure_also_aborts(sandbox, mode):
    """Step 4 only runs when wayakit_products.csv is missing; it is `|| exit 1` too."""
    _stage2_state(sandbox, mode)
    (sandbox.project / "ml_model" / "wayakit_products.csv").unlink()
    refetch = "ml_model/odoo_api_products.py"
    steps = STAGE2[mode]
    expected = steps[: steps.index("ml_model/odoo_api_competitor_products.py") + 1] + [refetch]
    ok = sandbox.run(stage=2)
    assert ok.returncode == 0, ok.trace()
    assert ok.python_scripts == expected + steps[len(expected) - 1:], ok.trace()
    failed = sandbox.run(stage=2, fail={refetch: 1})
    assert failed.returncode != 0 and failed.python_scripts == expected, failed.trace()
    _no_real_effects(failed)


# ---------------------------------------------------------------------------------------------
# One log folder outside the code (Pending 17, resolved 2026-11-18)
# ---------------------------------------------------------------------------------------------
STATE_FILES = ("pipeline_execution.log", "run_mode.txt", "last_full_run.txt")


def _with_log_dir(sandbox: sb.Sandbox, value: str) -> None:
    """Adds PI_LOG_DIR to the environment the sandbox builds from scratch (keeps `fail`/`path`)."""
    build = sandbox.env
    def env(fail=None, path=None):
        e = build(fail, path)
        e["PI_LOG_DIR"] = value
        return e
    sandbox.env = env  # type: ignore[method-assign]


def _record_python_log_dir(sandbox: sb.Sandbox) -> Path:
    """Makes the python shim (in the temp copy) also record the PI_LOG_DIR it receives.

    The extra line only appends to a file in the sandbox; the shim still does nothing else.
    """
    out = sandbox.root / "python_pi_log_dir.txt"
    shim = sandbox.shims / "python"
    shebang, rest = shim.read_text().split("\n", 1)
    shim.write_text(f"{shebang}\nprintf '%s\\n' \"${{PI_LOG_DIR-<unset>}}\" >> '{out}'\n{rest}")
    return out


def _files_named_like_state_in(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.name in STATE_FILES)


def test_default_log_folder_is_outside_the_code_and_holds_every_state_file(sandbox):
    stage1 = sandbox.run(stage=1)
    assert stage1.returncode == 0, stage1.trace()
    logs = _default_log_dir(sandbox.project)
    assert logs == sandbox.root / "logs"
    for name in STATE_FILES:
        assert (logs / name).is_file(), f"{name} not in {logs}\n{stage1.trace()}"
    assert "Starting Wayakit Pipeline (STAGE 1)" in _pipeline_log(stage1)
    # Nothing in the code folder: <project>/logs (the harness pre-creates it) stays empty or absent.
    assert not any((sandbox.project / "logs").glob("*")), stage1.trace()
    assert _files_named_like_state_in(sandbox.project) == [], stage1.trace()
    _no_real_effects(stage1)
    # Stage 2 picks up the mode stage 1 left in that folder and also logs there.
    sandbox.write("ml_model/wayakit_products.csv", "Product_ID\nP1\n")
    stage2 = sandbox.run(stage=2)
    assert stage2.returncode == 0, stage2.trace()
    assert stage2.python_scripts == STAGE2["full"], stage2.trace()
    assert "Starting Wayakit Pipeline (STAGE 2)" in _pipeline_log(stage2)
    assert not any((sandbox.project / "logs").glob("*")), stage2.trace()
    assert _files_named_like_state_in(sandbox.project) == [], stage2.trace()
    _no_real_effects(stage2)


def test_python_steps_receive_the_default_log_folder_as_an_absolute_pi_log_dir(sandbox):
    """Exported, so the upload's IDs file lands in the same folder as run_mode.txt."""
    seen = _record_python_log_dir(sandbox)
    _stage2_state(sandbox, "full")
    run = sandbox.run(stage=2)
    assert run.returncode == 0, run.trace()
    values = seen.read_text(encoding="utf-8").splitlines()
    assert len(values) == len(STAGE2["full"]), run.trace()
    assert set(values) == {str(_default_log_dir(run.project))}, values
    _no_real_effects(run)


@pytest.mark.parametrize("stage", [1, 2])
def test_pi_log_dir_moves_every_state_file_and_the_log_there(sandbox, tmp_path, stage):
    custom = tmp_path / "shared state" / "pi-logs"  # with a space: the script must quote it
    _with_log_dir(sandbox, str(custom))
    seen = _record_python_log_dir(sandbox)
    if stage == 2:
        _stage2_state(sandbox, "full", log_dir=custom)
    run = sandbox.run(stage=stage)
    assert run.returncode == 0, run.trace()
    assert run.python_scripts == (STAGE1 if stage == 1 else STAGE2["full"]), run.trace()
    assert (custom / "run_mode.txt").read_text().strip() == "full"
    if stage == 1:
        assert (custom / "last_full_run.txt").is_file()
    assert f"Starting Wayakit Pipeline (STAGE {stage})" in _pipeline_log(run, custom)
    # Neither the default folder nor the code folder got anything.
    assert not _default_log_dir(run.project).exists(), run.trace()
    assert _files_named_like_state_in(sandbox.project) == [], run.trace()
    assert set(seen.read_text(encoding="utf-8").splitlines()) == {str(custom)}
    _no_real_effects(run, custom)


def test_stage2_reads_run_mode_only_from_pi_log_dir(sandbox, tmp_path):
    """A run_mode.txt left in the default folder is ignored when PI_LOG_DIR points elsewhere."""
    custom = tmp_path / "pi-logs"
    _with_log_dir(sandbox, str(custom))
    _stage2_state(sandbox, "full")  # run_mode.txt in the DEFAULT folder only
    run = sandbox.run(stage=2)
    assert run.returncode == 1, run.trace()
    assert run.python_scripts == [], run.trace()
    assert str(custom / "run_mode.txt") in _pipeline_log(run, custom), run.trace()
    _no_real_effects(run, custom)


def test_empty_pi_log_dir_means_the_default_folder(sandbox):
    _with_log_dir(sandbox, "")
    run = sandbox.run(stage=1)
    assert run.returncode == 0, run.trace()
    assert (_default_log_dir(run.project) / "run_mode.txt").read_text().strip() == "full"
    assert _files_named_like_state_in(sandbox.project) == [], run.trace()
    _no_real_effects(run)


def test_relative_pi_log_dir_is_made_absolute_against_the_launch_folder(sandbox):
    """The harness launches from the project copy; the steps get the absolute folder."""
    _with_log_dir(sandbox, "rel-state")
    seen = _record_python_log_dir(sandbox)
    run = sandbox.run(stage=1)
    assert run.returncode == 0, run.trace()
    expected = sandbox.project / "rel-state"
    assert (expected / "run_mode.txt").read_text().strip() == "full"
    assert set(seen.read_text(encoding="utf-8").splitlines()) == {str(expected)}
    _no_real_effects(run, expected)
