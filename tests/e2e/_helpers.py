"""Corre el flujo de ML de PI fuera de línea, en una copia temporal del código.

Lo usan la prueba E2E (`tests/e2e/test_ml_pipeline_e2e.py`) y el arnés
diferencial (`tests/differential/_harness.py`).

Qué hace:

1. Copia el código (`scraper/`, `ml_model/`, `log_config.py`) a `<destino>/repo`,
   ya sea del árbol de trabajo actual (HEAD + cambios sin commit) o de una ref
   de git (`git archive`). Nunca escribe dentro del repo real.
2. Copia solo los CSV de ENTRADA congelados de `data/f1_antes_2026-11-15/` a las
   mismas rutas relativas. Las salidas (`competitor_*_processed.csv`,
   `wayakit_products_to_predict_odoo.csv`, `wayakit_prediction_report.csv`,
   modelos) no se copian: si aparecen al final, las generó esta corrida.
3. Corre como subprocesos, con `cwd=<destino>/repo`, las etapas
   1a → 1b (`--run_mode full`) → 2 → 3. `log_config.py` escribe en `../logs`,
   que queda en `<destino>/logs`.

Nunca corre `odoo_api_*` ni los scrapers: no hay llamadas a AWS, Odoo, Apify ni
Bedrock.

Los scripts de ML tragan excepciones y salen con 0 (ver CLAUDE.md, "Fallas
silenciosas"), así que una etapa se da por buena solo si sale con 0, no escribe
líneas de nivel ERROR y deja sus archivos de salida.
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
DATA_DIR = REPO_ROOT / "data" / "f1_antes_2026-11-15"
CODE_PATHS = ["scraper", "ml_model", "log_config.py"]

# Entradas del flujo de ML (relativas a la raíz del repo y a DATA_DIR).
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
ERROR_MARK = " - ERROR - "  # formato de log_config.py


def python_executable() -> str:
    """Python del entorno conda `wayakit-price-prediction` (el que corre pytest), o PI_PYTHON."""
    return os.environ.get("PI_PYTHON", sys.executable)


def data_missing_reason() -> str | None:
    """Motivo para saltar la prueba si faltan los datos congelados; None si están."""
    shown = DATA_DIR.relative_to(REPO_ROOT) if REPO_ROOT in DATA_DIR.parents else DATA_DIR
    if not DATA_DIR.is_dir():
        return (f"faltan los datos congelados en {shown}/ "
                "(carpeta ignorada por git; en CI se usarán fixtures sintéticos, F4)")
    missing = [f for f in INPUT_FILES if not (DATA_DIR / f).is_file()]
    if missing:
        return f"faltan entradas congeladas en {shown}/: {', '.join(missing)}"
    return None


def _check_outside_repo(dest: Path) -> None:
    dest = dest.resolve()
    if dest == REPO_ROOT or REPO_ROOT in dest.parents:
        raise ValueError(f"El destino {dest} está dentro del repo; usar un directorio temporal.")


def materialize_worktree(dest_repo: Path) -> Path:
    """Copia el código actual (HEAD + cambios sin commit + archivos nuevos no ignorados)."""
    _check_outside_repo(dest_repo)
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *CODE_PATHS],
        check=True, capture_output=True,
    ).stdout.decode()
    files = sorted({f for f in out.split("\0") if f})
    copied = 0
    for rel in files:
        src = REPO_ROOT / rel
        if not src.is_file():  # borrado en el árbol de trabajo
            continue
        dst = dest_repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    if copied == 0:
        raise RuntimeError("No se copió ningún archivo de código del árbol de trabajo.")
    return dest_repo


def resolve_ref(ref: str) -> str | None:
    """SHA del commit de `ref`, o None si no existe localmente."""
    r = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def materialize_ref(ref: str, dest_repo: Path) -> Path:
    """Extrae el código de una ref con `git archive <ref> scraper ml_model log_config.py | tar -x`."""
    _check_outside_repo(dest_repo)
    if resolve_ref(ref) is None:
        raise RuntimeError(f"La ref '{ref}' no existe localmente (¿falta `git fetch origin`?).")
    paths = [p for p in CODE_PATHS
             if subprocess.run(["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{ref}:{p}"],
                               capture_output=True).returncode == 0]
    dest_repo.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", ref, *paths],
                             check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest_repo)], input=archive, check=True, capture_output=True)
    return dest_repo


def copy_inputs(dest_repo: Path) -> None:
    """Copia los CSV de entrada congelados y borra salidas que hubieran venido con el código."""
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
    return "\n".join(lines[-n:]) if lines else "(vacío)"


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
        """Traza por etapa; para la que falló, las últimas 30 líneas de stdout y stderr."""
        out = [f"Traza del flujo de ML ({self.label}) en {self.repo_dir}:"]
        for s in self.steps:
            estado = "OK" if s.ok else "FALLA"
            out.append(f"  [{estado}] {s.stage.name}: exit={s.returncode}, {s.seconds:.1f} s, cmd={' '.join(s.cmd[1:])}")
            if not s.ok:
                out.extend(f"      - {p}" for p in s.problems)
                out.append(f"    --- stdout (últimas {TAIL_LINES} líneas) ---")
                out.append(_tail(s.stdout))
                out.append(f"    --- stderr (últimas {TAIL_LINES} líneas) ---")
                out.append(_tail(s.stderr))
        for stage in STAGES[len(self.steps):]:
            out.append(f"  [NO CORRIÓ] {stage.name}")
        return "\n".join(out)


class PipelineError(AssertionError):
    def __init__(self, run: PipelineRun):
        self.run = run
        super().__init__(run.trace())


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    # Sin PYTHONPATH: que `from log_config import ...` resuelva a la copia temporal, no al repo real.
    env.pop("PYTHONPATH", None)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONHASHSEED="0",
               PYTHONIOENCODING="utf-8", MPLBACKEND="Agg")
    return env


def run_ml_pipeline(repo_dir: Path, label: str = "", raise_on_error: bool = True) -> PipelineRun:
    """Corre 1a → 1b → 2 → 3 en `repo_dir`; se detiene en la primera etapa que falle."""
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
                              [f"se pasó del límite de {STEP_TIMEOUT_S} s"])
        if step.returncode not in (0, None):
            step.problems.append(f"salió con código {step.returncode}")
        error_lines = [ln for ln in (step.stdout + step.stderr).splitlines() if ERROR_MARK in ln]
        if error_lines:
            step.problems.append(f"{len(error_lines)} línea(s) de log con nivel ERROR (el script traga "
                                 f"excepciones y sale con 0); primera: {error_lines[0].strip()}")
        missing = [rel for rel in stage.outputs if not (repo_dir / rel).is_file()]
        if missing:
            step.problems.append(f"no generó: {', '.join(missing)}")
        run.steps.append(step)
        if not step.ok:
            break
    if raise_on_error and not run.ok:
        raise PipelineError(run)
    return run


def prepare_and_run(workdir: Path, ref: str | None = None, label: str = "") -> PipelineRun:
    """Arma `<workdir>/repo` (árbol de trabajo o `ref`), copia las entradas y corre el flujo."""
    repo_dir = workdir / "repo"
    if ref is None:
        materialize_worktree(repo_dir)
    else:
        materialize_ref(ref, repo_dir)
    copy_inputs(repo_dir)
    return run_ml_pipeline(repo_dir, label=label or (ref or "árbol de trabajo"))
