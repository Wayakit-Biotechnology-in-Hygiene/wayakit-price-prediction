"""E2E of `run_pipeline.sh` (one run, no stages) in a temporary copy with fake commands (shims).

See `tests/e2e/_pipeline_sandbox.py`: the real script never runs in its place, `python`, `timeout`,
`git`, `conda`, `aws`, `sudo` and `shutdown` are shims that only record, the `PATH` holds nothing
but those shims and a few network-free utilities, and the run refuses to start if `sudo` or
`shutdown` (the `trap EXIT` does `sudo shutdown -h +1`) do not resolve to their shim. There are no
calls to AWS, Odoo, Apify or Bedrock, and no real shutdown. It does not need the F1 data.

What it checks (single run, Mafer 2026-11-18):

- The exact steps, in order and with their exact arguments and `timeout`s: Odoo products ->
  scraping list -> scraping (`timeout 30h`) -> Odoo quotations and competitor products ->
  (re-fetch products only if `ml_model/wayakit_products.csv` is missing) -> 1a -> 1b
  `--run_mode full` -> 2 -> 3 -> upload `--run_mode full` (each ML step and the upload under
  `timeout 1h`). No `git` call ever (F2).
- Arguments: the script takes none. Any argument (an old crontab line with `--stage 2`, a
  `--monthly` from an old unit file, an empty string) exits 1 having run no step, and still shuts
  down (and alerts if the topic is set).
- C01 (`docs/reliability-checklist.md`): if ANY step fails (including the scraping `timeout`,
  exit 124) the script stops right there with exit 1, never calls `odoo_api_price_suggestion.py`,
  and the shutdown still happens exactly once. A failing upload also exits 1.
- C02: the previous `scraper/competitors_complete.csv` is never deleted (`scraper/main.py` writes
  to a temporary file and replaces the CSV only on success).
- Log folder (Pending 17): the only file the script writes is `pipeline_execution.log`, in
  `PI_LOG_DIR` if set and non-empty, else `<project>/../logs` (here `<sandbox root>/logs`); it is
  exported (absolute) to the Python steps. No state files (`run_mode.txt`, `last_full_run.txt`)
  and nothing under the code folder.
- SNS failure alert in the EXIT trap: only when the exit code is not 0 AND `PI_ALERTS_TOPIC_ARN`
  is set, exactly once, before the shutdown, through `timeout 60s`; subject and body name the
  failed step, never a stage; the last 20 log lines with secret values redacted; the topic ARN
  (it holds the account ID) is never printed or logged; a failed publish never blocks the
  shutdown or changes the exit code.

Removed with the stages (2026-11-18): stage 1/stage 2 order, `run_mode.txt` (full/partial,
unknown mode), the partial mode (`filter_new_products.py`, `wayakit_new_products_temp.csv`, the
`4h` scraping), `--monthly` chaining and the `(stage X, exit N)` alert subject.
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.e2e import _pipeline_sandbox as sb

pytestmark = pytest.mark.e2e

PRODUCTS = "ml_model/odoo_api_products.py"
LIST = "scraper/odoo_api_connection_products.py"
SCRAPER = "scraper/main.py"
QUOTES = "ml_model/odoo_api_cotizations.py"
COMPETITOR_PRODUCTS = "ml_model/odoo_api_competitor_products.py"
PRE_1A = "ml_model/1a_preprocess_data.py"
PRE_1B = "ml_model/1b_preprocess_data.py"
TRAIN = "ml_model/2_train_models.py"
PREDICT = "ml_model/3_predicted_prices.py"
UPLOAD = "ml_model/odoo_api_price_suggestion.py"
STEPS = [PRODUCTS, LIST, SCRAPER, QUOTES, COMPETITOR_PRODUCTS, PRE_1A, PRE_1B, TRAIN, PREDICT, UPLOAD]
REFETCH_AT = STEPS.index(PRE_1A)  # the optional re-fetch of products goes right before 1a
DESCRIPTIONS = {
    PRODUCTS: "1. Fetch ALL products from Odoo",
    LIST: "2. Generate the scraping list",
    SCRAPER: "3. Run scraping",
    QUOTES: "4a. Fetch quotations from Odoo",
    COMPETITOR_PRODUCTS: "4b. Fetch competitor products from Odoo",
    PRE_1A: "6. Preprocess competitor data",
    PRE_1B: "7. Prepare Wayakit data",
    TRAIN: "8. Train ML models",
    PREDICT: "9. Generate predictions",
    UPLOAD: "10. Upload suggestions to Odoo",
}
REFETCH_DESCRIPTION = "5. (Re)Fetch products from Odoo"
PRODUCTS_CSV = "ml_model/wayakit_products.csv"
COMPETITORS = "scraper/competitors_complete.csv"
TIMEOUT_EXIT = 124  # what `timeout` returns when it expires
OLD_CSV = "date,product\n2026-11-14,previous run row\n"
REMOVED_STATE_FILES = ("run_mode.txt", "last_full_run.txt")
ARGS_ERROR = "ERROR: run_pipeline.sh takes no arguments"


def _python_args(project: Path) -> list[str]:
    """Exact argv (after `python`) of each step, as the shim records it (`$*`)."""
    p = str(project)
    return [
        PRODUCTS,
        f"{LIST} --input_odoo_products_file {p}/{PRODUCTS_CSV} --output_analysis_file {p}/scraper/analysis-odoo.csv",
        f"{SCRAPER} --analysis_file {p}/scraper/analysis-odoo.csv --output_mode overwrite "
        f"--output_file {p}/{COMPETITORS}",
        QUOTES,
        COMPETITOR_PRODUCTS,
        PRE_1A,
        f"{PRE_1B} --run_mode full",
        TRAIN,
        PREDICT,
        f"{UPLOAD} --run_mode full",
    ]


def _timeout_args(project: Path) -> list[str]:
    args = _python_args(project)
    return [f"30h python {args[2]}"] + [f"1h python {a}" for a in args[REFETCH_AT:]]


@pytest.fixture
def sandbox(tmp_path):
    box = sb.Sandbox(tmp_path / "pi-run-pipeline")
    # The products read leaves this file in production; the shim writes nothing, so without it
    # the optional re-fetch step would run. Tests of that step delete it.
    box.write(PRODUCTS_CSV, "Product_ID\nP1\n")
    return box


def _default_log_dir(project: Path) -> Path:
    """The default log folder of run_pipeline.sh: <project>/../logs (= <sandbox root>/logs)."""
    return project.parent / "logs"


def _write_log(sandbox: sb.Sandbox, text: str, log_dir: Path | None = None) -> Path:
    """Writes pipeline_execution.log in the log folder (outside the code copy)."""
    p = (log_dir or _default_log_dir(sandbox.project)) / "pipeline_execution.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _pipeline_log(run: sb.SandboxRun, log_dir: Path | None = None) -> str:
    """pipeline_execution.log from the log folder (the harness's `run.pipeline_log` also reads
    the old `<project>/logs` path)."""
    p = (log_dir or _default_log_dir(run.project)) / "pipeline_execution.log"
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def _no_real_effects(run: sb.SandboxRun, log_dir: Path | None = None) -> None:
    """Everything that could leave the machine went through a shim and nothing was left unresolved."""
    assert not run.of("aws"), run.trace()
    assert not run.of("git"), f"run_pipeline.sh called git: {[c.args for c in run.of('git')]}\n{run.trace()}"
    assert [c.args for c in run.of("sudo")] == ["shutdown -h +1"], run.trace()
    assert not run.of("shutdown"), "the sudo shim executes nothing: shutdown must not be called directly"
    assert all(c.cwd == str(run.project) for c in run.of("python")), run.trace()
    output = run.stdout + run.stderr + _pipeline_log(run, log_dir)
    assert "command not found" not in output and "No such file" not in output, run.trace()
    assert not any(run.project.rglob("*.py")), "the copy must not contain code"


def _no_state_files(sandbox: sb.Sandbox, run: sb.SandboxRun) -> None:
    """No run_mode.txt / last_full_run.txt anywhere in the sandbox; nothing in <project>/logs."""
    left = sorted(str(p.relative_to(sandbox.root)) for p in sandbox.root.rglob("*")
                  if p.name in REMOVED_STATE_FILES)
    assert left == [], f"state files written: {left}\n{run.trace()}"
    # The harness pre-creates <project>/logs; it must stay empty.
    assert not any((sandbox.project / "logs").glob("*")), run.trace()


def _log_dir_holds_only_the_log(log_dir: Path, run: sb.SandboxRun) -> None:
    assert sorted(p.name for p in log_dir.iterdir()) == ["pipeline_execution.log"], run.trace()


def _shutdown_once_and_last(run: sb.SandboxRun) -> None:
    assert [c.args for c in run.of("sudo")] == ["shutdown -h +1"], run.trace()
    assert not run.of("shutdown"), run.trace()
    real = [c.command for c in run.calls if c.command in sb.SHIMS]
    assert real[-1] == "sudo", f"the shutdown must be the last call\n{run.trace()}"


def _run_args(sandbox: sb.Sandbox, args: list[str], fail: dict[str, int] | None = None,
              extra_env: dict[str, str] | None = None) -> sb.SandboxRun:
    """Like `Sandbox.run()` (same lock: environment from `sandbox.env()`, `check_shims()` on that
    exact environment, no code in the copy) but with any argv and extra environment."""
    env = sandbox.env(fail)
    env.update(extra_env or {})
    sandbox.check_shims(env)
    sandbox.log.unlink(missing_ok=True)
    p = subprocess.run([sb.BASH, "--noprofile", "--norc", str(sandbox.project / sb.SCRIPT), *args],
                       cwd=sandbox.project, env=env, capture_output=True, text=True,
                       timeout=sb.RUN_TIMEOUT_S)
    calls = []
    if sandbox.log.is_file():
        for line in sandbox.log.read_text(encoding="utf-8").splitlines():
            command, cwd, args_ = (line.split("\t", 2) + ["", ""])[:3]
            calls.append(sb.Call(command, cwd, args_))
    return sb.SandboxRun(p.returncode, p.stdout, p.stderr, calls, sandbox.project)


def _patch_shim(sandbox: sb.Sandbox, name: str, prelude: str) -> None:
    """Inserts `prelude` right after the shebang of a shim in the sandbox. The shim stays a file
    in the shims folder (the lock still sees it), still records its call and does nothing real."""
    shim = sandbox.shims / name
    shebang, rest = shim.read_text().split("\n", 1)
    shim.write_text(f"{shebang}\n{prelude}{rest}")


def _fail_on_call(sandbox: sb.Sandbox, script: str, nth: int, code: int = 1) -> None:
    """Makes the python shim fail `script` only from its `nth` call on (the products read runs
    twice when the re-fetch step runs; this fails only the re-fetch)."""
    counter = sandbox.root / "count.txt"
    _patch_shim(sandbox, "python", (
        f'if [ "$1" = "{script}" ]; then c=0; [ -f \'{counter}\' ] && read c < \'{counter}\'; '
        f'c=$((c + 1)); echo "$c" > \'{counter}\'; '
        f'[ "$c" -ge {nth} ] && PI_SHIM_FAIL="{script}={code}"; fi\n'))


def _failed_step(sandbox: sb.Sandbox, script: str, code: int, **kwargs) -> sb.SandboxRun:
    run = _run_args(sandbox, [], fail={script: code}, **kwargs)
    if f"shim python: {script} -> exit {code}" not in run.stdout:
        raise BrokenPrecondition(f"{script} did not fail as requested:\n" + run.trace())
    return run


class BrokenPrecondition(RuntimeError):
    """The failure was not injected: the test would prove nothing."""


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
        sandbox.run()
    assert not marker.exists() and not sandbox.log.exists(), "nothing should have run"


def test_refuses_to_run_if_a_shim_is_missing(sandbox):
    (sandbox.shims / "shutdown").unlink()
    with pytest.raises(sb.ShimNotProtecting, match="shutdown"):
        sandbox.run()


def test_python_shim_can_fail_a_step(sandbox):
    """The shim's failure control works (otherwise the C01 tests would prove nothing)."""
    run = sandbox.run(fail={SCRAPER: TIMEOUT_EXIT})
    assert f"shim python: {SCRAPER} -> exit {TIMEOUT_EXIT}" in run.stdout, run.trace()
    # run_command logs a failed step at ERROR level; only the level token is checked, not the wording.
    assert re.search(r"\bERROR\b", _pipeline_log(run)), run.trace()
    _no_real_effects(run)


def test_patched_shim_fails_only_from_the_nth_call(sandbox):
    """Precondition of the re-fetch tests: `_fail_on_call` fails the second products read only."""
    (sandbox.project / PRODUCTS_CSV).unlink()
    _fail_on_call(sandbox, PRODUCTS, 2)
    run = sandbox.run()
    assert f"shim python: {PRODUCTS} -> exit 0" in run.stdout, run.trace()
    assert f"shim python: {PRODUCTS} -> exit 1" in run.stdout, run.trace()
    assert run.python_scripts.count(PRODUCTS) == 2, run.trace()


# ---------------------------------------------------------------------------------------------
# Happy path: one run, every step, in order
# ---------------------------------------------------------------------------------------------
def test_single_run_calls_every_step_in_order_with_exact_arguments(sandbox):
    run = sandbox.run()
    assert run.returncode == 0, run.trace()
    assert run.python_scripts == STEPS, run.trace()
    assert [c.args for c in run.of("python")] == _python_args(run.project), run.trace()
    assert [c.args for c in run.of("timeout")] == _timeout_args(run.project), run.trace()
    assert [c.args for c in run.of("conda")] == ["info --base", "activate wayakit_env", "deactivate"], \
        run.trace()
    _no_real_effects(run)
    _shutdown_once_and_last(run)


def test_single_run_logs_the_banners_and_every_step_in_order_and_no_stage(sandbox):
    run = sandbox.run()
    assert run.returncode == 0, run.trace()
    log = _pipeline_log(run)
    marks = (["Starting Wayakit Pipeline"]
             + [f"Starting: {DESCRIPTIONS[s]}" for s in STEPS]
             + ["Wayakit Pipeline completed.", "Pipeline finished (code=0)"])
    positions = [log.find(m) for m in marks]
    assert -1 not in positions and positions == sorted(positions), (list(zip(marks, positions)), log)
    for s in STEPS:
        assert f"Success: {DESCRIPTIONS[s]} completed." in log
    assert REFETCH_DESCRIPTION not in log, "the products file exists: no re-fetch"
    assert "stage" not in log.lower(), log
    assert log.count("Pipeline finished") == 1, "one EXIT trap, one shutdown"


def test_missing_products_file_is_refetched_before_the_ml_steps(sandbox):
    """Step 5 only runs when wayakit_products.csv is missing (the shim never writes it)."""
    (sandbox.project / PRODUCTS_CSV).unlink()
    run = sandbox.run()
    assert run.returncode == 0, run.trace()
    assert run.python_scripts == STEPS[:REFETCH_AT] + [PRODUCTS] + STEPS[REFETCH_AT:], run.trace()
    assert f"Starting: {REFETCH_DESCRIPTION}" in _pipeline_log(run)
    _no_real_effects(run)


def test_a_failing_refetch_also_stops_before_the_ml_steps_and_the_upload(sandbox):
    (sandbox.project / PRODUCTS_CSV).unlink()
    _fail_on_call(sandbox, PRODUCTS, 2)
    run = sandbox.run()
    assert run.returncode == 1, run.trace()
    assert run.python_scripts == STEPS[:REFETCH_AT] + [PRODUCTS], run.trace()
    assert f"ERROR: {REFETCH_DESCRIPTION} failed" in _pipeline_log(run), run.trace()
    _no_real_effects(run)
    _shutdown_once_and_last(run)


# ---------------------------------------------------------------------------------------------
# Arguments: none accepted (an old crontab line or unit file must not start anything)
# ---------------------------------------------------------------------------------------------
BAD_ARGS = [["--stage", "2"], ["--stage", "1"], ["--monthly"], [""], ["--stage"], ["--stage=2"],
            ["-h"], ["--monthly", "--stage", "2"], ["full"], ["--stage", "2", "x"]]


@pytest.mark.parametrize("args", BAD_ARGS, ids=lambda a: repr(" ".join(a)))
def test_any_argument_exits_1_runs_nothing_and_still_shuts_down(sandbox, args):
    run = _run_args(sandbox, args)
    assert run.returncode == 1, run.trace()
    assert run.python_scripts == [] and not run.of("timeout"), run.trace()
    # Only the configuration block's `conda info --base`: no activation, no step.
    assert [c.args for c in run.of("conda")] == ["info --base"], run.trace()
    log = _pipeline_log(run)
    assert f"{ARGS_ERROR} (got '{' '.join(args)}')" in log, log
    assert "Starting Wayakit Pipeline" not in log, log
    assert "Pipeline finished (code=1)" in log
    _no_real_effects(run)
    _shutdown_once_and_last(run)
    _no_state_files(sandbox, run)


def test_the_old_saturday_crontab_line_runs_nothing(sandbox):
    """`Sandbox.run(stage=2)` passes `--stage 2`, what the old `ec2-user` crontab line does."""
    run = sandbox.run(stage=2)
    assert run.returncode == 1, run.trace()
    assert run.python_scripts == [], run.trace()
    assert ARGS_ERROR in _pipeline_log(run)
    _no_real_effects(run)


def test_a_failing_conda_activation_is_a_setup_failure_and_runs_nothing(sandbox):
    _patch_shim(sandbox, "conda", 'if [ "$1" = activate ]; then\n'
                                  + sb._RECORD + "exit 1\nfi\n")
    run = sandbox.run()
    assert run.returncode == 1, run.trace()
    assert run.python_scripts == [], run.trace()
    assert "ERROR: Could not activate the environment wayakit_env" in _pipeline_log(run)
    _no_real_effects(run)
    _shutdown_once_and_last(run)


# ---------------------------------------------------------------------------------------------
# C01: a failing step stops the run right there, before the Odoo upload
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("code", [TIMEOUT_EXIT, 1], ids=["timeout", "error"])
def test_failed_scraping_exits_1_and_never_calls_the_odoo_upload(sandbox, code):
    run = _failed_step(sandbox, SCRAPER, code)
    assert run.returncode == 1, f"exit {run.returncode} after scraping exited {code}\n{run.trace()}"
    assert run.python_scripts == STEPS[: STEPS.index(SCRAPER) + 1], run.trace()
    assert UPLOAD not in run.python_scripts, run.trace()
    _no_real_effects(run)
    _shutdown_once_and_last(run)


@pytest.mark.parametrize("script", STEPS[:-1], ids=lambda s: s.split("/")[-1])
def test_any_failing_step_stops_the_run_right_there_and_before_the_upload(sandbox, script):
    """C01 for every step, not only scraping (e.g. `2_train_models.py` failing in the middle)."""
    run = _failed_step(sandbox, script, 1)
    assert run.returncode == 1, run.trace()
    assert run.python_scripts == STEPS[: STEPS.index(script) + 1], (
        f"after {script} failed, the pipeline kept running\n{run.trace()}")
    assert UPLOAD not in run.python_scripts, run.trace()
    log = _pipeline_log(run)
    assert f"ERROR: {DESCRIPTIONS[script]} failed" in log, log
    assert "Wayakit Pipeline completed." not in log, log
    assert [c.args for c in run.of("conda")] == ["info --base", "activate wayakit_env"], run.trace()
    _no_real_effects(run)
    _shutdown_once_and_last(run)


def test_failed_upload_exits_1(sandbox):
    run = _failed_step(sandbox, UPLOAD, 1)
    assert run.python_scripts == STEPS, run.trace()
    assert run.returncode == 1, f"exited {run.returncode} after the upload failed\n{run.trace()}"
    assert "Wayakit Pipeline completed." not in _pipeline_log(run)
    _no_real_effects(run)
    _shutdown_once_and_last(run)


# ---------------------------------------------------------------------------------------------
# C02: the previous competitors CSV is never deleted
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fail", [None, {SCRAPER: TIMEOUT_EXIT}, {SCRAPER: 1}, {UPLOAD: 1}],
                         ids=["ok", "scraping-timeout", "scraping-fails", "upload-fails"])
def test_the_previous_competitors_csv_is_kept(sandbox, fail):
    """The shim writes nothing, so the CSV can only disappear if run_pipeline.sh deletes it."""
    csv = sandbox.write(COMPETITORS, OLD_CSV)
    run = sandbox.run(fail=fail)
    assert run.returncode == (1 if fail else 0), run.trace()
    assert csv.is_file(), f"run_pipeline.sh deleted {COMPETITORS}\n{run.trace()}"
    assert csv.read_text(encoding="utf-8") == OLD_CSV, run.trace()
    _no_real_effects(run)


# ---------------------------------------------------------------------------------------------
# One log folder outside the code; no state files
# ---------------------------------------------------------------------------------------------
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
    _patch_shim(sandbox, "python", f"printf '%s\\n' \"${{PI_LOG_DIR-<unset>}}\" >> '{out}'\n")
    return out


@pytest.mark.parametrize("fail, args", [
    (None, []), ({SCRAPER: TIMEOUT_EXIT}, []), ({UPLOAD: 1}, []), (None, ["--stage", "2"]),
], ids=["ok", "scraping-timeout", "upload-fails", "argument"])
def test_the_only_file_written_is_the_log_in_the_default_folder(sandbox, fail, args):
    run = _run_args(sandbox, args, fail=fail)
    assert run.returncode == (0 if fail is None and not args else 1), run.trace()
    logs = _default_log_dir(sandbox.project)
    assert logs == sandbox.root / "logs"
    _log_dir_holds_only_the_log(logs, run)
    _no_state_files(sandbox, run)
    _no_real_effects(run)


def test_python_steps_receive_the_default_log_folder_as_an_absolute_pi_log_dir(sandbox):
    """Exported, so the upload's IDs file lands in the same folder as the log."""
    seen = _record_python_log_dir(sandbox)
    run = sandbox.run()
    assert run.returncode == 0, run.trace()
    values = seen.read_text(encoding="utf-8").splitlines()
    assert len(values) == len(STEPS), run.trace()
    assert set(values) == {str(_default_log_dir(run.project))}, values
    _no_real_effects(run)


def test_pi_log_dir_moves_the_log_there(sandbox, tmp_path):
    custom = tmp_path / "shared state" / "pi-logs"  # with a space: the script must quote it
    _with_log_dir(sandbox, str(custom))
    seen = _record_python_log_dir(sandbox)
    run = sandbox.run()
    assert run.returncode == 0, run.trace()
    assert run.python_scripts == STEPS, run.trace()
    _log_dir_holds_only_the_log(custom, run)
    assert "Starting Wayakit Pipeline" in _pipeline_log(run, custom)
    # Neither the default folder nor the code folder got anything.
    assert not _default_log_dir(run.project).exists(), run.trace()
    _no_state_files(sandbox, run)
    assert set(seen.read_text(encoding="utf-8").splitlines()) == {str(custom)}
    _no_real_effects(run, custom)


def test_empty_pi_log_dir_means_the_default_folder(sandbox):
    _with_log_dir(sandbox, "")
    seen = _record_python_log_dir(sandbox)
    run = sandbox.run()
    assert run.returncode == 0, run.trace()
    _log_dir_holds_only_the_log(_default_log_dir(run.project), run)
    assert set(seen.read_text(encoding="utf-8").splitlines()) == {str(_default_log_dir(run.project))}
    _no_state_files(sandbox, run)
    _no_real_effects(run)


def test_relative_pi_log_dir_is_made_absolute_against_the_launch_folder(sandbox):
    """The harness launches from the project copy; the steps get the absolute folder."""
    _with_log_dir(sandbox, "rel-state")
    seen = _record_python_log_dir(sandbox)
    run = sandbox.run()
    assert run.returncode == 0, run.trace()
    expected = sandbox.project / "rel-state"
    _log_dir_holds_only_the_log(expected, run)
    assert set(seen.read_text(encoding="utf-8").splitlines()) == {str(expected)}
    _no_real_effects(run, expected)


# ---------------------------------------------------------------------------------------------
# SNS failure alert in the EXIT trap
# ---------------------------------------------------------------------------------------------
TOPIC = "arn:aws:sns:eu-north-1:000000000000:price-intelligence-alerts"
FAKE_HOST = "ip-10-1-2-3.eu-north-1.compute.internal"
SUBJECT = "Price Intelligence pipeline failed (exit 1)"
EXCERPT_HEADER = ("Last 20 lines of the log (values after key/token/secret/password/authorization "
                  "redacted):\n")


def _aws_recorder(sandbox: sb.Sandbox, exit_code: int = 0) -> Path:
    """Replaces the `aws` shim (still a file in the shims folder, still records in calls.tsv) with
    one that also writes its exact argv (NUL-separated) to `<root>/aws-args/aws.<pid>`, and exits
    with `exit_code`. It still does nothing real."""
    out = sandbox.root / "aws-args"
    out.mkdir(exist_ok=True)
    shim = sandbox.shims / "aws"
    shim.write_text("#!/bin/sh\n# run_pipeline.sh test shim: records and does nothing real.\n"
                    + sb._RECORD
                    + f"printf '%s\\0' \"$@\" > '{out}/aws.$$'\n"
                    + f"exit {exit_code}\n")
    return out


def _aws_argvs(out: Path) -> list[list[str]]:
    return [p.read_bytes().decode("utf-8").split("\0")[:-1] for p in sorted(out.glob("aws.*"))]


def _option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def _alert_env(**extra: str) -> dict[str, str]:
    env = {"PI_ALERTS_TOPIC_ARN": TOPIC, "HOSTNAME": FAKE_HOST, "TZ": "America/Mexico_City"}
    env.update(extra)
    return env


def _no_leaks(run: sb.SandboxRun, log_dir: Path | None = None) -> None:
    """The topic ARN (it holds the account ID) is never printed or logged."""
    text = run.stdout + run.stderr + _pipeline_log(run, log_dir)
    assert TOPIC not in text and "000000000000" not in text, run.trace()
    assert "command not found" not in text and "No such file" not in text, run.trace()


def _alert_message(out: Path, run: sb.SandboxRun) -> str:
    argvs = _aws_argvs(out)
    assert len(argvs) == 1, run.trace()
    return _option(argvs[0], "--message")


def test_successful_run_sends_no_alert_even_with_the_topic_set(sandbox):
    out = _aws_recorder(sandbox)
    run = _run_args(sandbox, [], extra_env=_alert_env())
    assert run.returncode == 0, run.trace()
    assert not run.of("aws") and _aws_argvs(out) == [], run.trace()
    assert not [c for c in run.of("timeout") if c.args.startswith("60s ")], run.trace()
    assert "alert" not in _pipeline_log(run).lower()
    _no_leaks(run)
    _shutdown_once_and_last(run)


@pytest.mark.parametrize("args, fail", [([], {SCRAPER: TIMEOUT_EXIT}), ([], {UPLOAD: 1}),
                                        (["--stage", "2"], None)],
                         ids=["scraping-timeout", "upload", "argument"])
def test_failures_without_the_topic_send_no_alert(sandbox, args, fail):
    out = _aws_recorder(sandbox)
    run = _run_args(sandbox, args, fail=fail, extra_env={"HOSTNAME": FAKE_HOST})
    assert run.returncode == 1, run.trace()
    assert not run.of("aws") and _aws_argvs(out) == [], run.trace()
    assert "alert" not in _pipeline_log(run).lower()
    _no_real_effects(run)


def test_empty_topic_variable_means_no_alert(sandbox):
    out = _aws_recorder(sandbox)
    run = _run_args(sandbox, [], fail={SCRAPER: TIMEOUT_EXIT}, extra_env=_alert_env(PI_ALERTS_TOPIC_ARN=""))
    assert run.returncode == 1, run.trace()
    assert _aws_argvs(out) == [] and not run.of("aws"), run.trace()
    _shutdown_once_and_last(run)


ALERT_CASES = [
    ([], {SCRAPER: TIMEOUT_EXIT}, DESCRIPTIONS[SCRAPER]),
    ([], {PRODUCTS: 1}, DESCRIPTIONS[PRODUCTS]),
    ([], {LIST: 1}, DESCRIPTIONS[LIST]),
    ([], {COMPETITOR_PRODUCTS: 1}, DESCRIPTIONS[COMPETITOR_PRODUCTS]),
    ([], {TRAIN: 1}, DESCRIPTIONS[TRAIN]),
    ([], {UPLOAD: 1}, DESCRIPTIONS[UPLOAD]),
    (["--stage", "2"], None, "arguments"),
    (["--monthly"], None, "arguments"),
]


@pytest.mark.parametrize("args, fail, step", ALERT_CASES,
                         ids=["scraper-timeout", "products", "scraping-list", "competitor-products",
                              "train", "upload", "old-stage2-line", "old-monthly-unit"])
def test_failure_alert_is_published_once_before_the_shutdown(sandbox, args, fail, step):
    out = _aws_recorder(sandbox)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    run = _run_args(sandbox, args, fail=fail, extra_env=_alert_env())
    after = datetime.now(timezone.utc)
    assert run.returncode == 1, run.trace()
    argvs = _aws_argvs(out)
    assert len(argvs) == 1 and len(run.of("aws")) == 1, run.trace()
    argv = argvs[0]
    assert argv[:2] == ["sns", "publish"]
    assert _option(argv, "--region") == "eu-north-1"
    assert _option(argv, "--topic-arn") == TOPIC
    assert _option(argv, "--subject") == SUBJECT
    assert len(_option(argv, "--subject")) < 100, "SNS email subjects must be under 100 characters"
    assert set(argv[2::2]) == {"--region", "--topic-arn", "--subject", "--message"}, argv
    # Through `timeout 60s` so a hung publish cannot block the shutdown.
    assert [c.args.split(" ")[:4] for c in run.of("timeout") if c.args.startswith("60s ")] == \
        [["60s", "aws", "sns", "publish"]], run.trace()
    message = _option(argv, "--message")
    head = message.split(EXCERPT_HEADER, 1)[0]
    assert EXCERPT_HEADER in message
    assert f"\nFailed step: {step}\n" in head, head
    assert "\nExit code: 1\n" in head
    assert "Host: ip-<ip>.eu-north-1.compute.internal" in head and "10-1-2-3" not in message
    log_file = _default_log_dir(run.project) / "pipeline_execution.log"
    assert f"\nLog: {log_file}\n" in head
    assert "stage" not in head.lower() and not re.search(r"^Run:", message, re.M), head
    stamp = re.search(r"^Time \(UTC\): (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)$", head, re.M)
    assert stamp, head
    sent = datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert before <= sent <= after, (sent, before, after, "the time must be UTC, not TZ")
    log = _pipeline_log(run)
    assert log.count("Failure alert sent (SNS).") == 1
    assert log.index("Failure alert sent (SNS).") < log.index("Pipeline finished (code=1)")
    # The aws call happens before the shutdown, and the shutdown still happens.
    shim_calls = [c.command for c in run.calls if c.command in sb.SHIMS]
    assert shim_calls.index("aws") < shim_calls.index("sudo"), run.trace()
    assert UPLOAD not in run.python_scripts or step == DESCRIPTIONS[UPLOAD], run.trace()
    _shutdown_once_and_last(run)
    _no_leaks(run)


def test_alert_names_the_refetch_step(sandbox):
    (sandbox.project / PRODUCTS_CSV).unlink()
    _fail_on_call(sandbox, PRODUCTS, 2)
    out = _aws_recorder(sandbox)
    run = _run_args(sandbox, [], extra_env=_alert_env())
    assert run.returncode == 1, run.trace()
    assert f"\nFailed step: {REFETCH_DESCRIPTION}\n" in _alert_message(out, run)
    _shutdown_once_and_last(run)


def test_alert_names_a_setup_failure(sandbox):
    _patch_shim(sandbox, "conda", 'if [ "$1" = activate ]; then\n' + sb._RECORD + "exit 1\nfi\n")
    out = _aws_recorder(sandbox)
    run = _run_args(sandbox, [], extra_env=_alert_env())
    assert run.returncode == 1 and run.python_scripts == [], run.trace()
    assert "\nFailed step: setup\n" in _alert_message(out, run)
    _shutdown_once_and_last(run)
    _no_leaks(run)


def test_alert_carries_the_last_20_log_lines(sandbox):
    """The message ends with exactly the last 20 lines the log had when the alert was built."""
    _write_log(sandbox, "".join(f"2026-11-26 21:{i:02d}:00 - earlier line {i:03d}\n" for i in range(50)))
    out = _aws_recorder(sandbox)
    run = _run_args(sandbox, ["--stage", "2"], extra_env=_alert_env())
    assert run.returncode == 1, run.trace()
    message = _alert_message(out, run)
    assert EXCERPT_HEADER in message
    tail = message.split(EXCERPT_HEADER, 1)[1].split("\n")
    log_lines = _pipeline_log(run).splitlines()
    # After the alert the script logs two more lines: the alert result and "Pipeline finished".
    assert "Failure alert sent" in log_lines[-2] and "Pipeline finished" in log_lines[-1]
    assert tail == log_lines[:-2][-20:], (tail, log_lines)
    assert any("earlier line" in line for line in tail) and "earlier line 029" not in message


def test_alert_after_a_failing_step_carries_the_last_20_lines_of_that_run(sandbox):
    out = _aws_recorder(sandbox)
    run = _run_args(sandbox, [], fail={TRAIN: 1}, extra_env=_alert_env())
    assert run.returncode == 1, run.trace()
    tail = _alert_message(out, run).split(EXCERPT_HEADER, 1)[1].split("\n")
    log_lines = _pipeline_log(run).splitlines()
    assert len(log_lines) - 2 > 20, "precondition: the run logs more than 20 lines"
    assert tail == log_lines[:-2][-20:], (tail, log_lines)
    assert f"ERROR: {DESCRIPTIONS[TRAIN]} failed" in tail[-1]


def test_alert_with_a_short_log_sends_all_of_it(sandbox):
    out = _aws_recorder(sandbox)
    run = _run_args(sandbox, [], fail={PRODUCTS: 1}, extra_env=_alert_env())
    assert run.returncode == 1, run.trace()
    tail = _alert_message(out, run).split(EXCERPT_HEADER, 1)[1].split("\n")
    assert tail == _pipeline_log(run).splitlines()[:-2]
    assert len(tail) < 20


SECRET_LINES = [
    "GET https://generativelanguage.googleapis.com/v1/models/x:generate?key=AIzaFAKEFAKE123&alt=json",
    "retrying with KEY=FAKEUPPERCASE0 now",
    "auth token=FAKETOKEN0 refused",
    "Token=FAKETOKEN1;expires=1",
    "api_key=FAKEAPIKEY0",
    "x" * 2000 + " key=FAKEINLONGLINE0",
    '{"api_key": "FAKEJSON0", "model": "x"}',
    "{'token': 'FAKEJSON1'}",
    'body={\\"client_secret\\": \\"FAKEJSON2\\"}',
    "x-api-key: FAKEHEADER0",
    "Authorization: Bearer FAKEBEARER0",
    "password = FAKEPASSWORD0",
    "client_secret=FAKESECRET0 next",
    'key=x?key="FAKEGLUED0"',
    ' key="FAKEGLUED1"FAKEGLUED2',
]


def test_alert_redacts_secret_values_in_every_form_and_caps_line_length(sandbox):
    _write_log(sandbox, "\n".join(SECRET_LINES) + "\n")
    out = _aws_recorder(sandbox)
    run = _run_args(sandbox, ["--stage", "2"], extra_env=_alert_env())
    assert run.returncode == 1, run.trace()
    message = _alert_message(out, run)
    for secret in ("AIzaFAKEFAKE123", "FAKEUPPERCASE0", "FAKETOKEN0", "FAKETOKEN1", "FAKEAPIKEY0",
                   "FAKEINLONGLINE0", "FAKEJSON0", "FAKEJSON1", "FAKEJSON2", "FAKEHEADER0",
                   "FAKEBEARER0", "FAKEPASSWORD0", "FAKESECRET0", "FAKEGLUED0", "FAKEGLUED1",
                   "FAKEGLUED2"):
        assert secret not in message, secret
    lines = message.split("\n")
    assert "GET https://generativelanguage.googleapis.com/v1/models/x:generate?key=<redacted>&alt=json" \
        in lines, "only the value of a plain name=value is redacted"
    assert "auth token=<redacted> refused" in lines
    assert "x-api-key: <redacted>" in lines and "Authorization: <redacted>" in lines
    assert '{"api_key": <redacted>' in lines and "{'token': <redacted>" in lines
    assert "password = <redacted>" in lines and "client_secret=<redacted> next" in lines
    assert max(len(line) for line in lines) <= 500
    # The log on disk is not rewritten (redaction is only for the email).
    log = _pipeline_log(run)
    assert "AIzaFAKEFAKE123" in log and "FAKEBEARER0" in log


@pytest.mark.parametrize("line, secrets, kept", [
    ('key="FAKEQUOTED"', ["FAKEQUOTED"], 'key=<redacted>'),
    ("token='FAKESQ'", ["FAKESQ"], "token=<redacted>"),
    ('API_KEY="FAKE WITH SPACES" next', ["FAKE WITH SPACES", "WITH SPACES"], "API_KEY=<redacted> next"),
    ("auth Token='FAKEOPEN never closed", ["FAKEOPEN", "never closed"], "auth Token=<redacted>"),
    ('a key=FAKEA1&token="FAKEB 2" c', ["FAKEA1", "FAKEB 2"], "a key=<redacted>&token=<redacted> c"),
    ("k=1 key=FAKE1&token=\"FAKE2\" API_KEY='FAKE3'", ["FAKE1", "FAKE2", "FAKE3"],
     "k=1 key=<redacted>&token=<redacted> API_KEY=<redacted>"),
], ids=["double", "single", "spaces", "unterminated", "several", "three"])
def test_alert_redacts_quoted_key_values(sandbox, line, secrets, kept):
    _write_log(sandbox, line + "\n")
    out = _aws_recorder(sandbox)
    run = _run_args(sandbox, ["--stage", "2"], extra_env=_alert_env())
    assert run.returncode == 1, run.trace()
    message = _alert_message(out, run)
    for secret in secrets:
        assert secret not in message, secret
    assert kept in message.split("\n"), "only the value is replaced; the key name keeps its case"


@pytest.mark.parametrize("aws_exit", [1, 255, 124], ids=["error", "cli-error", "timeout"])
def test_failed_publish_never_blocks_the_shutdown_or_changes_the_exit_code(sandbox, aws_exit):
    out = _aws_recorder(sandbox, exit_code=aws_exit)
    run = _run_args(sandbox, [], fail={SCRAPER: TIMEOUT_EXIT}, extra_env=_alert_env())
    assert run.returncode == 1, run.trace()
    assert len(_aws_argvs(out)) == 1
    log = _pipeline_log(run)
    assert "WARNING: could not send the failure alert (SNS). Shutting down anyway." in log
    assert "Failure alert sent" not in log
    assert "Pipeline finished (code=1)" in log
    _shutdown_once_and_last(run)
    _no_leaks(run)


@pytest.mark.parametrize("topic, region", [
    ("arn:aws:sns:eu-west-1:000000000000:price-intelligence-alerts", "eu-west-1"),
    ("not-an-arn", ""),
], ids=["other-region", "malformed"])
def test_alert_region_comes_from_the_arn_and_a_bad_arn_still_shuts_down(sandbox, topic, region):
    out = _aws_recorder(sandbox, exit_code=0 if region else 255)
    run = _run_args(sandbox, [], fail={TRAIN: 1}, extra_env=_alert_env(PI_ALERTS_TOPIC_ARN=topic))
    assert run.returncode == 1, run.trace()
    argv = _aws_argvs(out)[0]
    assert _option(argv, "--region") == region and _option(argv, "--topic-arn") == topic
    assert topic not in run.stdout + run.stderr + _pipeline_log(run)
    _shutdown_once_and_last(run)
