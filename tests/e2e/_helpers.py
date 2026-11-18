"""Runs the PI ML flow offline, in a temporary copy of the code.

Used by the E2E test (`tests/e2e/test_ml_pipeline_e2e.py`) and the differential
harness (`tests/differential/_harness.py`).

What it does:

1. Copies the code (`scraper/`, `ml_model/`, `log_config.py`) to `<dest>/repo`,
   either from the current working tree (HEAD + uncommitted changes) or from a git
   ref (`git archive`). It never writes inside the real repo.
2. Copies only the frozen INPUT CSVs from `data/f1_before_2026-11-15/` to the
   same relative paths. The outputs (`competitor_*_processed.csv`,
   `wayakit_products_to_predict_odoo.csv`, `wayakit_prediction_report.csv`,
   models) are not copied: if they show up at the end, this run generated them.
3. Runs, as subprocesses with `cwd=<dest>/repo`, the stages
   1a → 1b (`--run_mode full`) → 2 → 3. `log_config.py` writes to `../logs`,
   which ends up in `<dest>/logs`.

It never runs `odoo_api_*` or the scrapers: no calls to AWS, Odoo, Apify or
Bedrock.

The ML scripts swallow exceptions and exit with 0 (see CLAUDE.md, "Silent
failures"), so a stage is considered good only if it exits with 0, writes no
ERROR-level lines and leaves its output files.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "f1_before_2026-11-15"
CODE_PATHS = ["scraper", "ml_model", "log_config.py"]

# ML flow inputs (relative to the repo root and to DATA_DIR).
INPUT_FILES = [
    "scraper/competitors_complete.csv",
    "ml_model/competitor_products_from_odoo.csv",
    "ml_model/wayakit_products.csv",
    "ml_model/wayakit_cotizations.csv",
]

MODEL_DIR = "ml_model/trained_models"
MODEL_FILES = [
    "volumetric_model.joblib",
    "volumetric_model_columns.joblib",
    "unit_model.joblib",
    "unit_model_columns.joblib",
    "vol_market_stats.joblib",
    "unit_market_stats.joblib",
]

REPORT = "ml_model/wayakit_prediction_report.csv"
TO_PREDICT = "ml_model/wayakit_products_to_predict_odoo.csv"
VOL_PROCESSED = "ml_model/competitor_volumetric_processed.csv"
UNIT_PROCESSED = "ml_model/competitor_unit_processed.csv"


@dataclass(frozen=True)
class Stage:
    name: str
    args: tuple[str, ...]
    outputs: tuple[str, ...]


STAGES = [
    Stage("1a_preprocess", ("ml_model/1a_preprocess_data.py",), (VOL_PROCESSED, UNIT_PROCESSED)),
    Stage("1b_preprocess", ("ml_model/1b_preprocess_data.py", "--run_mode", "full"), (TO_PREDICT,)),
    Stage("2_train", ("ml_model/2_train_models.py",), tuple(f"{MODEL_DIR}/{f}" for f in MODEL_FILES)),
    Stage("3_predict", ("ml_model/3_predicted_prices.py",), (REPORT,)),
]

STEP_TIMEOUT_S = 900
TAIL_LINES = 30
ERROR_MARK = " - ERROR - "  # log_config.py format


def python_executable() -> str:
    """Python from the `wayakit-price-prediction` conda environment (the one running pytest), or PI_PYTHON."""
    return os.environ.get("PI_PYTHON", sys.executable)


def data_missing_reason() -> str | None:
    """Reason to skip the test if the frozen data is missing; None if it is present."""
    shown = DATA_DIR.relative_to(REPO_ROOT) if REPO_ROOT in DATA_DIR.parents else DATA_DIR
    if not DATA_DIR.is_dir():
        return (f"the frozen data is missing in {shown}/ "
                "(folder ignored by git; CI will use synthetic fixtures, F4)")
    missing = [f for f in INPUT_FILES if not (DATA_DIR / f).is_file()]
    if missing:
        return f"frozen inputs missing in {shown}/: {', '.join(missing)}"
    return None


def _check_outside_repo(dest: Path) -> None:
    dest = dest.resolve()
    if dest == REPO_ROOT or REPO_ROOT in dest.parents:
        raise ValueError(f"The destination {dest} is inside the repo; use a temporary directory.")


def materialize_worktree(dest_repo: Path) -> Path:
    """Copies the current code (HEAD + uncommitted changes + new non-ignored files)."""
    _check_outside_repo(dest_repo)
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *CODE_PATHS],
        check=True, capture_output=True,
    ).stdout.decode()
    files = sorted({f for f in out.split("\0") if f})
    copied = 0
    for rel in files:
        src = REPO_ROOT / rel
        if not src.is_file():  # deleted in the working tree
            continue
        dst = dest_repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    if copied == 0:
        raise RuntimeError("No code file was copied from the working tree.")
    return dest_repo


def resolve_ref(ref: str) -> str | None:
    """Commit SHA of `ref`, or None if it does not exist locally."""
    r = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def materialize_ref(ref: str, dest_repo: Path) -> Path:
    """Extracts the code of a ref with `git archive <ref> scraper ml_model log_config.py | tar -x`."""
    _check_outside_repo(dest_repo)
    if resolve_ref(ref) is None:
        raise RuntimeError(f"The ref '{ref}' does not exist locally (missing `git fetch origin`?).")
    paths = [p for p in CODE_PATHS
             if subprocess.run(["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{ref}:{p}"],
                               capture_output=True).returncode == 0]
    dest_repo.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", ref, *paths],
                             check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest_repo)], input=archive, check=True, capture_output=True)
    return dest_repo


def copy_inputs(dest_repo: Path) -> None:
    """Copies the frozen input CSVs and deletes outputs that may have come with the code."""
    for rel in INPUT_FILES:
        dst = dest_repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DATA_DIR / rel, dst)
    for stage in STAGES:
        for rel in stage.outputs:
            (dest_repo / rel).unlink(missing_ok=True)


@dataclass
class StepResult:
    stage: Stage
    cmd: list[str]
    returncode: int | None
    seconds: float
    stdout: str
    stderr: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _tail(text: str, n: int = TAIL_LINES) -> str:
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-n:]) if lines else "(empty)"


@dataclass
class PipelineRun:
    repo_dir: Path
    label: str
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.steps) == len(STAGES) and all(s.ok for s in self.steps)

    @property
    def seconds(self) -> float:
        return sum(s.seconds for s in self.steps)

    def path(self, rel: str) -> Path:
        return self.repo_dir / rel

    def trace(self) -> str:
        """Trace per stage; for the one that failed, the last 30 lines of stdout and stderr."""
        out = [f"ML flow trace ({self.label}) in {self.repo_dir}:"]
        for s in self.steps:
            status = "OK" if s.ok else "FAIL"
            out.append(f"  [{status}] {s.stage.name}: exit={s.returncode}, {s.seconds:.1f} s, cmd={' '.join(s.cmd[1:])}")
            if not s.ok:
                out.extend(f"      - {p}" for p in s.problems)
                out.append(f"    --- stdout (last {TAIL_LINES} lines) ---")
                out.append(_tail(s.stdout))
                out.append(f"    --- stderr (last {TAIL_LINES} lines) ---")
                out.append(_tail(s.stderr))
        for stage in STAGES[len(self.steps):]:
            out.append(f"  [DID NOT RUN] {stage.name}")
        return "\n".join(out)


class PipelineError(AssertionError):
    def __init__(self, run: PipelineRun):
        self.run = run
        super().__init__(run.trace())


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    # No PYTHONPATH: `from log_config import ...` must resolve to the temporary copy, not the real repo.
    env.pop("PYTHONPATH", None)
    # No PI_LOG_DIR: E2E subprocesses must never write to a developer's real log folder.
    env.pop("PI_LOG_DIR", None)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONHASHSEED="0",
               PYTHONIOENCODING="utf-8", MPLBACKEND="Agg")
    return env


def run_ml_pipeline(repo_dir: Path, label: str = "", raise_on_error: bool = True) -> PipelineRun:
    """Runs 1a → 1b → 2 → 3 in `repo_dir`; stops at the first stage that fails."""
    _check_outside_repo(repo_dir)
    run = PipelineRun(repo_dir=repo_dir, label=label or str(repo_dir))
    py = python_executable()
    env = _subprocess_env()
    for stage in STAGES:
        cmd = [py, *stage.args]
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, cwd=repo_dir, env=env, capture_output=True, text=True,
                               timeout=STEP_TIMEOUT_S)
            step = StepResult(stage, cmd, p.returncode, time.monotonic() - t0, p.stdout, p.stderr)
        except subprocess.TimeoutExpired as e:
            def _txt(b):
                return b.decode(errors="replace") if isinstance(b, bytes) else (b or "")
            step = StepResult(stage, cmd, None, time.monotonic() - t0, _txt(e.stdout), _txt(e.stderr),
                              [f"exceeded the {STEP_TIMEOUT_S} s limit"])
        if step.returncode not in (0, None):
            step.problems.append(f"exited with code {step.returncode}")
        error_lines = [ln for ln in (step.stdout + step.stderr).splitlines() if ERROR_MARK in ln]
        if error_lines:
            step.problems.append(f"{len(error_lines)} log line(s) at ERROR level (the script swallows "
                                 f"exceptions and exits with 0); first: {error_lines[0].strip()}")
        missing = [rel for rel in stage.outputs if not (repo_dir / rel).is_file()]
        if missing:
            step.problems.append(f"did not generate: {', '.join(missing)}")
        run.steps.append(step)
        if not step.ok:
            break
    if raise_on_error and not run.ok:
        raise PipelineError(run)
    return run


def prepare_and_run(workdir: Path, ref: str | None = None, label: str = "") -> PipelineRun:
    """Builds `<workdir>/repo` (working tree or `ref`), copies the inputs and runs the flow."""
    repo_dir = workdir / "repo"
    if ref is None:
        materialize_worktree(repo_dir)
    else:
        materialize_ref(ref, repo_dir)
    copy_inputs(repo_dir)
    return run_ml_pipeline(repo_dir, label=label or (ref or "working tree"))
