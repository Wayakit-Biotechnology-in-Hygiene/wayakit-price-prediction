"""Pruebas diferenciales del flujo de ML: ref base vs árbol de trabajo con los mismos datos.

- `test_base_vs_nueva_sin_diferencias_sin_aprobar` corre 1a → 1b → 2 → 3 en las dos versiones
  (ver `tests/differential/_harness.py`) y falla si hay diferencias que no estén en
  `cambios_aprobados.json`. Necesita `data/f1_antes_2026-11-15/` (si faltan los datos se salta,
  único salto permitido) y la ref base local (si falta, la prueba FALLA: una ref ausente no es
  falta de datos y no debe pasar en silencio).
- Las demás son autopruebas del arnés: demuestran que la comparación SÍ detecta una diferencia
  (si no, una prueba que siempre pasa no prueba nada). No necesitan datos.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from tests.differential import _harness as dh
from tests.e2e import _helpers as h

REPORT = dh.REPORT_FILE


# ---------------------------------------------------------------------------------------------
# Prueba diferencial real
# ---------------------------------------------------------------------------------------------
@pytest.mark.differential
def test_base_vs_nueva_sin_diferencias_sin_aprobar(tmp_path):
    reason = h.data_missing_reason()
    if reason:
        pytest.skip(reason)
    ref = dh.base_ref()
    if h.resolve_ref(ref) is None:
        pytest.fail(f"la ref base '{ref}' no existe localmente: correr `git fetch origin` o definir "
                    "PI_BASE_REF. Solo la falta de datos permite saltar esta prueba.", pytrace=False)
    result = dh.run_differential(tmp_path, ref=ref)  # CoberturaIncorrecta también hace fallar
    summary = result.summary()
    print(summary)
    print(result.base_run.trace())
    print(result.new_run.trace())
    assert not result.pending, summary


# ---------------------------------------------------------------------------------------------
# Autopruebas del arnés (sin datos)
# ---------------------------------------------------------------------------------------------
def _report() -> pd.DataFrame:
    return pd.DataFrame({
        "Product_ID": ["A", "B", "B", "C"],
        "product_name": ["a", "b1", "b2", "c"],
        "predicted_price": [10.0, 20.5, 20.5, 7.25],
        "competitors_count": [3, 0, 0, 5],
        "clamped_to_market": [False, False, False, True],
    })


def _ok(**kw) -> dict:
    """Aprobación válida mínima del reporte; `kw` cambia o agrega campos."""
    e = {"archivo": REPORT, "tipo": "valor_cambiado", "columna": "predicted_price",
         "celdas_esperadas": 1, "motivo": "prueba", "fecha": "2026-11-17", "aprobado_por": "Mafer"}
    e.update(kw)
    return e


def _without(e: dict, *fields: str) -> dict:
    return {k: v for k, v in e.items() if k not in fields}



@pytest.mark.unit
@pytest.mark.differential
def test_tablas_iguales_no_dan_diferencias_aunque_cambie_el_orden_de_columnas():
    base = _report()
    new = base[list(reversed(base.columns))]
    assert dh.compare_frames(base, new, REPORT) == []


@pytest.mark.unit
@pytest.mark.differential
def test_detecta_un_valor_cambiado_y_respeta_la_tolerancia():
    base = _report()
    new = base.copy()
    new.loc[3, "predicted_price"] = 7.26
    diffs = dh.compare_frames(base, new, REPORT)
    assert diffs == [dh.Diferencia(REPORT, "valor_cambiado", clave="C#0", columna="predicted_price",
                                   base=7.25, nuevo=7.26)]
    tiny = base.copy()
    tiny.loc[3, "predicted_price"] = 7.25 + 1e-9
    assert dh.compare_frames(base, tiny, REPORT) == []


@pytest.mark.unit
@pytest.mark.differential
def test_ids_repetidos_se_distinguen_por_aparicion():
    base = _report()
    new = base.copy()
    new.loc[2, "product_name"] = "otro"
    diffs = dh.compare_frames(base, new, REPORT)
    assert [(d.clave, d.columna) for d in diffs] == [("B#1", "product_name")]


@pytest.mark.unit
@pytest.mark.differential
def test_detecta_filas_y_columnas_agregadas_o_quitadas():
    base = _report()
    new = pd.concat([base.iloc[:3], pd.DataFrame({"Product_ID": ["D"], "product_name": ["d"],
                                                  "predicted_price": [1.0], "competitors_count": [0],
                                                  "clamped_to_market": [False]})], ignore_index=True)
    new = new.drop(columns=["clamped_to_market"]).assign(origin="local")
    tipos = {(d.tipo, d.clave or d.columna) for d in dh.compare_frames(base, new, REPORT)}
    assert ("fila_agregada", "D#0") in tipos
    assert ("fila_eliminada", "C#0") in tipos
    assert ("columna_agregada", "origin") in tipos
    assert ("columna_eliminada", "clamped_to_market") in tipos


@pytest.mark.unit
@pytest.mark.differential
def test_compare_output_dirs_detecta_un_valor_perturbado_en_el_csv(tmp_path):
    """En una copia en tmp se cambia un precio del reporte y la comparación lo reporta."""
    base_repo, new_repo = tmp_path / "base", tmp_path / "new"
    shape = pd.DataFrame({"type_of_product": ["x", "y"], "price_per_liter": [1.0, 2.0]})
    for repo in (base_repo, new_repo):
        (repo / "ml_model").mkdir(parents=True)
        _report().to_csv(repo / REPORT, index=False)
        for rel in dh.COUNT_FILES:
            shape.to_csv(repo / rel, index=False)
    assert dh.compare_output_dirs(base_repo, new_repo) == []

    perturbed = pd.read_csv(new_repo / REPORT)
    perturbed.loc[0, "predicted_price"] += 0.01
    perturbed.to_csv(new_repo / REPORT, index=False)
    shape.iloc[:1].to_csv(new_repo / h.UNIT_PROCESSED, index=False)

    diffs = dh.compare_output_dirs(base_repo, new_repo)
    assert {(d.archivo, d.tipo, d.clave, d.columna) for d in diffs} == {
        (REPORT, "valor_cambiado", "A#0", "predicted_price"),
        (h.UNIT_PROCESSED, "conteo_filas", None, None),
    }
    pending, approved = dh.split_approved(diffs, [])
    summary = dh.summarize(pending, approved)
    assert "predicted_price: 1" in summary and "A#0" in summary


@pytest.mark.unit
@pytest.mark.differential
def test_aprobaciones_filtran_solo_lo_que_cubren():
    diffs = [
        dh.Diferencia(REPORT, "valor_cambiado", clave="A#0", columna="predicted_price", base=1.0, nuevo=2.0),
        dh.Diferencia(REPORT, "valor_cambiado", clave="A#0", columna="product_name", base="a", nuevo="b"),
    ]
    pending, approved = dh.split_approved(diffs, [_ok()])
    assert [d.columna for d in pending] == ["product_name"]
    assert [d.columna for d in approved] == ["predicted_price"]


@pytest.mark.unit
@pytest.mark.differential
@pytest.mark.parametrize("bad, motivo", [
    (_without(_ok(), "motivo"), "sin 'motivo'"),
    (_ok(fecha="17/11/2026"), "AAAA-MM-DD"),
    (_ok(fecha="2026-02-30"), "no existe"),
    (_without(_ok(), "aprobado_por"), "sin 'aprobado_por'"),
    (_ok(aprobado_por="pi-tester"), "solo aprueba"),
    (_ok(aprobado_por="mafer"), "solo aprueba"),
    (_ok(tipo="otro"), "no válido"),
    # tipo y columna obligatorios: sin ellos la entrada sería un comodín del archivo completo
    (_without(_ok(), "tipo"), "sin 'tipo'"),
    (_without(_ok(), "columna"), "exige 'columna'"),
    (_ok(tipo="columna_agregada", columna=""), "exige 'columna'"),
    (_ok(tipo="fila_agregada"), "no lleva 'columna'"),
    # celdas_esperadas obligatorio, entero ≥ 1
    (_without(_ok(), "celdas_esperadas"), "sin 'celdas_esperadas'"),
    (_ok(celdas_esperadas=0), "entero ≥ 1"),
    (_ok(celdas_esperadas="3"), "entero ≥ 1"),
    (_ok(celdas_esperadas=True), "entero ≥ 1"),
    (_ok(celdas_esperadas=2.0), "entero ≥ 1"),
    # archivo y tipo compatibles; filtros solo donde tienen sentido
    (_ok(archivo="ml_model/otro.csv"), "no se compara"),
    (_ok(tipo="conteo_filas", archivo=REPORT) | {"columna": None}, "no aplica"),
    (_without(_ok(archivo=h.VOL_PROCESSED), "columna") | {"tipo": "valor_cambiado"}, "no aplica"),
    (_ok(tipo="columna_eliminada", subindustria="Pets"), "'subindustria' solo"),
    (_ok(subindustria=""), "'subindustria' solo"),
    (_ok(clave=None), "'clave' solo"),
    # campos desconocidos: un nombre mal escrito ampliaría la aprobación sin avisar
    (_ok(columnas="predicted_price"), "no permitidos"),
])
def test_aprobaciones_invalidas_se_rechazan(bad, motivo):
    with pytest.raises(dh.AprobacionesInvalidas, match=motivo):
        dh.validate_approvals([bad])


@pytest.mark.unit
@pytest.mark.differential
@pytest.mark.parametrize("good", [
    _ok(),
    _ok(clave="A#0", subindustria="Pets", celdas_esperadas=3),
    _without(_ok(tipo="fila_agregada", clave="D#0"), "columna"),
    _without(_ok(tipo="fila_eliminada", subindustria="Pets", celdas_esperadas=40), "columna"),
    _ok(tipo="columna_agregada", columna="origin"),
    _without(_ok(archivo=h.UNIT_PROCESSED, tipo="conteo_filas"), "columna"),
    _ok(archivo=h.TO_PREDICT, tipo="columna_eliminada", columna="x"),
])
def test_aprobaciones_validas_se_aceptan(good):
    dh.validate_approvals([good])


def _diffs_por_subindustria() -> list[dh.Diferencia]:
    """Base vs nueva con precios cambiados en 2 filas de Pets y 1 de Home care."""
    base = _report().assign(subindustry=["Pets", "Pets", "Home care", "Pets"])
    new = base.copy()
    new.loc[[0, 3], "predicted_price"] += 1.0   # A y C (Pets)
    new.loc[2, "predicted_price"] += 1.0        # B#1 (Home care)
    return dh.compare_frames(base, new, REPORT)


@pytest.mark.unit
@pytest.mark.differential
def test_diferencias_del_reporte_traen_su_subindustria():
    diffs = _diffs_por_subindustria()
    assert {(d.clave, d.subindustria) for d in diffs} == {("A#0", "Pets"), ("C#0", "Pets"),
                                                          ("B#1", "Home care")}


@pytest.mark.unit
@pytest.mark.differential
def test_filtro_de_subindustria_no_aprueba_otras_subindustrias():
    """Caso de r7 (C17): aprobar un cambio de Pets no debe tapar un cambio de precio en otra subindustria."""
    diffs = _diffs_por_subindustria()
    pending, approved = dh.split_approved(diffs, [_ok(subindustria="Pets", celdas_esperadas=2)])
    assert {d.clave for d in approved} == {"A#0", "C#0"}
    assert [(d.clave, d.subindustria) for d in pending] == [("B#1", "Home care")]


@pytest.mark.unit
@pytest.mark.differential
@pytest.mark.parametrize("esperadas", [1, 3])
def test_cobertura_distinta_de_celdas_esperadas_falla(esperadas):
    """La entrada cubre 2 celdas de Pets: declarar más o menos hace fallar, no aprueba en silencio."""
    diffs = _diffs_por_subindustria()
    with pytest.raises(dh.CoberturaIncorrecta, match=rf"esperadas {esperadas}, cubre 2"):
        dh.split_approved(diffs, [_ok(subindustria="Pets", celdas_esperadas=esperadas)])


@pytest.mark.unit
@pytest.mark.differential
def test_aprobacion_sin_filtro_que_cubre_de_mas_falla():
    """Sin subindustria la misma entrada cubre 3 celdas; con celdas_esperadas=2 eso es un error."""
    with pytest.raises(dh.CoberturaIncorrecta, match="esperadas 2, cubre 3"):
        dh.split_approved(_diffs_por_subindustria(), [_ok(celdas_esperadas=2)])


@pytest.mark.unit
@pytest.mark.differential
def test_aprobacion_que_ya_no_cubre_nada_falla():
    unused = _without(_ok(archivo=h.VOL_PROCESSED, tipo="conteo_filas"), "columna")
    with pytest.raises(dh.CoberturaIncorrecta, match="esperadas 1, cubre 0"):
        dh.split_approved(_diffs_por_subindustria(), [_ok(subindustria="Pets", celdas_esperadas=2), unused])


@pytest.mark.unit
@pytest.mark.differential
def test_split_approved_valida_las_entradas():
    with pytest.raises(dh.AprobacionesInvalidas, match="solo aprueba"):
        dh.split_approved(_diffs_por_subindustria(), [_ok(aprobado_por="otra persona")])


@pytest.mark.unit
@pytest.mark.differential
def test_archivo_de_aprobaciones_del_repo_es_valido():
    entries = dh.load_approvals()
    raw = json.loads(dh.APPROVALS_FILE.read_text(encoding="utf-8"))
    assert isinstance(raw["cambios"], list) and entries == raw["cambios"]
    dh.validate_approvals([raw["_ejemplo"]])  # el ejemplo documentado cumple las reglas
