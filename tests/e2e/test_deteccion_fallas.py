"""Autopruebas de la detección de fallas de `tests/e2e/_helpers.py` (sin datos, sin red).

Los scripts de `ml_model/` tragan excepciones y salen con 0, así que `run_ml_pipeline` da una
etapa por buena solo si sale con 0, no escribe líneas ` - ERROR - ` y deja sus salidas. Estas
pruebas demuestran que esa detección funciona: en `tmp_path` se ponen scripts FALSOS con los
nombres de las etapas reales y se exige `run.ok is False` en cada forma de falla silenciosa.
Hay un control positivo (todo bien → `run.ok is True`) para que no pasen por un detector que
siempre dice "falla".
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tests.e2e import _helpers as h

pytestmark = pytest.mark.unit

_FAKE = textwrap.dedent('''\
    import sys
    from pathlib import Path
    outputs = {outputs!r}
    for rel in outputs[:{write}]:
        Path(rel).parent.mkdir(parents=True, exist_ok=True)
        Path(rel).write_text("x\\n1\\n")
    {extra}
    sys.exit({code})
''')


def _fake_repo(tmp_path: Path, bad_stage: str | None = None, *, write_all: bool = True,
               extra: str = "", code: int = 0) -> Path:
    """Repo falso: cada etapa escribe sus salidas y sale con 0, salvo `bad_stage`."""
    repo = tmp_path / "repo"
    for stage in h.STAGES:
        script = repo / stage.args[0]
        script.parent.mkdir(parents=True, exist_ok=True)
        bad = stage.name == bad_stage
        outputs = list(stage.outputs)
        script.write_text(_FAKE.format(
            outputs=outputs,
            write=len(outputs) if (not bad or write_all) else 0,
            extra=extra if bad else "",
            code=code if bad else 0,
        ))
    return repo


def test_control_positivo_scripts_que_cumplen_dan_ok(tmp_path):
    run = h.run_ml_pipeline(_fake_repo(tmp_path), raise_on_error=False)
    assert run.ok, run.trace()
    assert [s.stage.name for s in run.steps] == [s.name for s in h.STAGES]


@pytest.mark.parametrize("stage", [s.name for s in h.STAGES])
def test_sale_con_0_sin_escribir_su_salida_es_falla(tmp_path, stage):
    run = h.run_ml_pipeline(_fake_repo(tmp_path, stage, write_all=False), raise_on_error=False)
    assert run.ok is False, run.trace()
    failed = run.steps[-1]
    assert failed.stage.name == stage and failed.returncode == 0
    assert any(p.startswith("no generó:") for p in failed.problems), failed.problems
    # Se detiene en la etapa que falló: las siguientes no corren.
    assert len(run.steps) == [s.name for s in h.STAGES].index(stage) + 1


@pytest.mark.parametrize("where", ["stdout", "stderr"])
def test_linea_error_en_el_log_es_falla_aunque_salga_con_0(tmp_path, where):
    msg = "2026-11-17 21:50:00,000 - wayakit - ERROR - Error al preprocesar: KeyError('price_sar')"
    extra = f"print({msg!r}, file=sys.{where})"
    run = h.run_ml_pipeline(_fake_repo(tmp_path, "1a_preprocess", extra=extra), raise_on_error=False)
    assert run.ok is False, run.trace()
    failed = run.steps[-1]
    assert failed.returncode == 0 and failed.stage.name == "1a_preprocess"
    assert any("nivel ERROR" in p for p in failed.problems), failed.problems
    assert "[FALLA] 1a_preprocess" in run.trace() and "[NO CORRIÓ] 1b_preprocess" in run.trace()


def test_codigo_distinto_de_0_es_falla(tmp_path):
    run = h.run_ml_pipeline(_fake_repo(tmp_path, "2_train", code=3), raise_on_error=False)
    assert run.ok is False
    assert "salió con código 3" in run.steps[-1].problems


def test_raise_on_error_lanza_pipeline_error_con_la_traza(tmp_path):
    with pytest.raises(h.PipelineError, match=r"\[FALLA\] 3_predict"):
        h.run_ml_pipeline(_fake_repo(tmp_path, "3_predict", write_all=False))


def test_salida_vieja_borrada_por_copy_inputs_no_cuenta_como_generada(tmp_path, monkeypatch):
    """Una salida que venía con el código (de una corrida anterior) se borra antes de correr."""
    data = tmp_path / "datos"
    for rel in h.INPUT_FILES:
        (data / rel).parent.mkdir(parents=True, exist_ok=True)
        (data / rel).write_text("a\n1\n")
    monkeypatch.setattr(h, "DATA_DIR", data)
    repo = _fake_repo(tmp_path, "3_predict", write_all=False)
    (repo / h.REPORT).write_text("vieja\n")
    h.copy_inputs(repo)
    assert not (repo / h.REPORT).exists()
    run = h.run_ml_pipeline(repo, raise_on_error=False)
    assert run.ok is False and "no generó: " + h.REPORT in run.steps[-1].problems


def test_no_corre_dentro_del_repo_real():
    with pytest.raises(ValueError, match="dentro del repo"):
        h.run_ml_pipeline(h.REPO_ROOT / "ml_model", raise_on_error=False)
