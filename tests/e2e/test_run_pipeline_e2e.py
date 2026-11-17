"""E2E de `run_pipeline.sh` (etapas 1 y 2) en una copia temporal con comandos falsos (shims).

Ver `tests/e2e/_pipeline_sandbox.py`: el script real nunca corre en su lugar, `python`, `timeout`,
`git`, `conda`, `aws`, `sudo` y `shutdown` son shims que solo registran, el `PATH` no trae nada
más que esos shims y unas utilidades sin red, y la corrida se niega a empezar si `sudo` o
`shutdown` (el `trap EXIT` hace `sudo shutdown -h +1`) no resuelven a su shim. No hay llamadas a
AWS, Odoo, Apify ni Bedrock, ni apagado real. No necesita los datos de F1.

Qué documenta:

- Hoy (pasan): el orden de los pasos de cada etapa, que la etapa 1 aborta si falla un paso y que
  el apagado siempre pasa por el shim.
- Lo deseado (xfail estricto, bug C01 de `docs/checklist-confiabilidad.md`): si el scraping
  falla o se vence el `timeout`, la etapa 2 NO debe llamar a `odoo_api_price_suggestion.py` y
  debe salir con código distinto de 0. Hoy sí la llama y sale con 0 (corrida del 13–15 nov 2026).
  Cuando `pi-dev` lo arregle, el xfail pasará a XPASS y `xfail_strict` hará fallar la suite: ahí
  se quita la marca.
"""
from __future__ import annotations

import pytest

from tests.e2e import _pipeline_sandbox as sb

pytestmark = pytest.mark.e2e

SUBIDA = "ml_model/odoo_api_price_suggestion.py"
SCRAPER = "scraper/main.py"
TIMEOUT_EXIT = 124  # lo que devuelve `timeout` cuando se vence

ETAPA1 = ["ml_model/odoo_api_products.py", "scraper/odoo_api_connection_products.py"]
ETAPA2 = {
    "full": [SCRAPER, "ml_model/odoo_api_cotizations.py", "ml_model/odoo_api_competitor_products.py",
             "ml_model/1a_preprocess_data.py", "ml_model/1b_preprocess_data.py",
             "ml_model/2_train_models.py", "ml_model/3_predicted_prices.py", SUBIDA],
    "partial": ["scraper/odoo_api_connection_products.py", SCRAPER, "ml_model/odoo_api_cotizations.py",
                "ml_model/odoo_api_competitor_products.py", "ml_model/1a_preprocess_data.py",
                "ml_model/1b_preprocess_data.py", "ml_model/2_train_models.py",
                "ml_model/3_predicted_prices.py", SUBIDA],
}
SCRAPER_TIMEOUT = {"full": "30h", "partial": "4h"}


@pytest.fixture
def sandbox(tmp_path):
    return sb.Sandbox(tmp_path / "pi-run-pipeline")


def _etapa2(sandbox: sb.Sandbox, modo: str) -> None:
    """Estado que deja la etapa 1 en producción: modo, productos de Odoo y (parcial) productos nuevos."""
    sandbox.write("logs/run_mode.txt", f"{modo}\n")
    sandbox.write("ml_model/wayakit_products.csv", "Product_ID\nP1\n")
    if modo == "partial":
        sandbox.write("ml_model/wayakit_new_products_temp.csv", "Product_ID\nP2\nP3\n")


def _sin_efectos_reales(run: sb.Corrida) -> None:
    """Todo lo que podría salir de la máquina pasó por un shim y nada quedó sin resolver."""
    assert not run.de("aws"), run.traza()
    assert [c.args for c in run.de("sudo")] == ["shutdown -h +1"], run.traza()
    assert not run.de("shutdown"), "el shim de sudo no ejecuta nada: shutdown no debe llamarse directo"
    assert all(c.cwd == str(run.project) for c in run.de("python")), run.traza()
    salida = run.stdout + run.stderr + run.pipeline_log
    assert "command not found" not in salida and "No such file" not in salida, run.traza()
    assert not any(run.project.rglob("*.py")), "la copia no debe traer código"


# ---------------------------------------------------------------------------------------------
# Candado de los shims
# ---------------------------------------------------------------------------------------------
def test_shims_resuelven_primero_y_sudo_shutdown_estan(sandbox):
    sandbox.check_shims(sandbox.env())  # no lanza


def test_se_niega_a_correr_si_sudo_no_es_el_shim(sandbox):
    # Con el PATH del sistema primero, sudo/shutdown/git resuelven a los reales: no debe correr.
    env = sandbox.env(path=f"{sb.SISTEMA_PATH}:/sbin:/usr/sbin:{sandbox.shims}")
    with pytest.raises(sb.ShimNoProtege, match="sudo"):
        sandbox.check_shims(env)
    # Por si el candado fallara: en este caso la copia es un script inofensivo que deja una marca.
    marca = sandbox.root / "corrio.txt"
    (sandbox.project / sb.SCRIPT).write_text(f"#!/bin/bash\necho si > '{marca}'\n")
    sandbox.env = lambda fallar=None, path=None: env  # type: ignore[method-assign]
    with pytest.raises(sb.ShimNoProtege):
        sandbox.run(stage=1)
    assert not marca.exists() and not sandbox.log.exists(), "no debió ejecutarse nada"


def test_se_niega_a_correr_si_falta_un_shim(sandbox):
    (sandbox.shims / "shutdown").unlink()
    with pytest.raises(sb.ShimNoProtege, match="shutdown"):
        sandbox.run(stage=1)


def test_shim_de_python_puede_fallar_un_paso(sandbox):
    """El control de fallas del shim funciona (si no, el xfail de C01 no probaría nada)."""
    _etapa2(sandbox, "full")
    run = sandbox.run(stage=2, fallar={SCRAPER: TIMEOUT_EXIT})
    assert f"shim python: {SCRAPER} -> exit {TIMEOUT_EXIT}" in run.stdout, run.traza()
    assert "ERROR: 2. Ejecutar scraping" in run.pipeline_log, run.traza()
    _sin_efectos_reales(run)


# ---------------------------------------------------------------------------------------------
# Comportamiento de hoy (correcto)
# ---------------------------------------------------------------------------------------------
def test_etapa1_corre_sus_pasos_y_apaga_con_el_shim(sandbox):
    run = sandbox.run(stage=1)
    assert run.returncode == 0, run.traza()
    assert run.scripts_python == ETAPA1, run.traza()
    assert [c.args for c in run.de("conda")][:2] == ["info --base", "activate wayakit_env"], run.traza()
    assert (run.project / "logs" / "run_mode.txt").read_text().strip() == "full"
    assert (run.project / "logs" / "last_full_run.txt").is_file()
    assert not run.de("git"), "la etapa 1 no hace git pull"
    _sin_efectos_reales(run)


def test_etapa1_aborta_si_falla_la_lectura_de_odoo_y_aun_asi_apaga(sandbox):
    run = sandbox.run(stage=1, fallar={ETAPA1[0]: 1})
    assert run.returncode == 1, run.traza()
    assert run.scripts_python == ETAPA1[:1], run.traza()
    assert not (run.project / "logs" / "run_mode.txt").exists()
    _sin_efectos_reales(run)


@pytest.mark.parametrize("modo", ["full", "partial"])
def test_etapa2_sin_fallas_corre_todos_los_pasos_en_orden(sandbox, modo):
    _etapa2(sandbox, modo)
    run = sandbox.run(stage=2)
    assert run.returncode == 0, run.traza()
    assert run.scripts_python == ETAPA2[modo], run.traza()
    assert [c.args for c in run.de("git")] == ["pull"], run.traza()
    timeouts = [c.args for c in run.de("timeout")]
    assert f"{SCRAPER_TIMEOUT[modo]} python {SCRAPER}" in [t.split(" --")[0] for t in timeouts], run.traza()
    subida = [c.args for c in run.de("python") if c.script == SUBIDA]
    assert subida == [f"{SUBIDA} --run_mode {modo}"], "la subida solo llegó al shim"
    _sin_efectos_reales(run)


def test_etapa2_sin_run_mode_sale_con_1_y_apaga(sandbox):
    run = sandbox.run(stage=2)
    assert run.returncode == 1, run.traza()
    assert run.scripts_python == [] and not run.de("git"), run.traza()
    _sin_efectos_reales(run)


# ---------------------------------------------------------------------------------------------
# Lo deseado: bug C01 (xfail estricto, hallazgo para pi-dev)
# ---------------------------------------------------------------------------------------------
class PrecondicionRota(RuntimeError):
    """La falla no se inyectó: no es el xfail esperado (raises=AssertionError no la cubre)."""


def _scraping_fallido(sandbox: sb.Sandbox, modo: str, code: int) -> sb.Corrida:
    _etapa2(sandbox, modo)
    run = sandbox.run(stage=2, fallar={SCRAPER: code})
    if f"shim python: {SCRAPER} -> exit {code}" not in run.stdout:
        raise PrecondicionRota("el scraping no falló como se pidió:\n" + run.traza())
    print(run.traza())
    return run


C01 = ("BUG C01 (docs/checklist-confiabilidad.md): run_pipeline.sh no aborta cuando el scraping "
       "falla o vence el timeout y aun así entrena y sube a Odoo (corrida del 13-15 nov 2026). "
       "Hallazgo para pi-dev: agregar `|| exit 1` (o equivalente) a cada paso de la etapa 2.")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=C01)
@pytest.mark.parametrize("modo", ["full", "partial"])
@pytest.mark.parametrize("code", [TIMEOUT_EXIT, 1], ids=["timeout", "error"])
def test_scraping_fallido_no_llama_a_la_subida_a_odoo(sandbox, modo, code):
    run = _scraping_fallido(sandbox, modo, code)
    assert SUBIDA not in run.scripts_python, (
        f"con el scraping fallido (exit {code}) se llamó a {SUBIDA}\n{run.traza()}")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=C01)
@pytest.mark.parametrize("modo", ["full", "partial"])
def test_scraping_vencido_hace_salir_con_codigo_distinto_de_0(sandbox, modo):
    run = _scraping_fallido(sandbox, modo, TIMEOUT_EXIT)
    assert run.returncode != 0, f"la etapa 2 salió con 0 tras un timeout del scraping\n{run.traza()}"
