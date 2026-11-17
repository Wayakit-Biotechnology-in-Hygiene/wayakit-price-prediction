"""Propiedades de ``ml_model/1a_preprocess_data.py`` (precio por litro y por pieza).

``process_volumetric_data`` y ``process_unit_data`` no devuelven nada: escriben
``ml_model/competitor_*_processed.csv`` relativo al cwd. Aquí se corren dentro de un directorio
temporal (``en_directorio``) y se lee el CSV que dejan; nunca se escribe en el repo.

Fila válida (C18 de ``docs/checklist-confiabilidad.md``): unidad de volumen reconocida (o
cantidad numérica en el caso por pieza), cantidad > 0 y precio > 0. Hoy hay dos bugs conocidos
(volumétrico con cantidad negativa y precio 0 en ambos caminos): las propiedades generales usan
estrategias que no generan esos contraejemplos, y cada bug tiene su prueba mínima
``xfail(strict=True)`` que solo corre su ``@example``.
"""
import math

import numpy as np
import pandas as pd
import pytest
from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st

from tests.conftest import cargar_modulo, en_directorio

preproceso = cargar_modulo("ml_model/1a_preprocess_data.py")

pytestmark = [
    pytest.mark.property,
    # El código de producción hace dropna(inplace=True) sobre un slice: pandas avisa en cada
    # llamada. El resultado es correcto (df es local); se silencia para no tapar otros avisos.
    pytest.mark.filterwarnings("ignore::pandas.errors.SettingWithCopyWarning"),
]

LITROS_POR_UNIDAD = {"ml": 1 / 1000, "l": 1.0, "fl oz": 0.0295735}
UNIDADES = ["ml", "ML", " ml ", "l", "L", " L", "fl oz", "FL OZ", "g", "G", "kg", "litre", "oz", "units"]

cantidades = st.one_of(
    st.integers(-100, 100_000).map(float),
    st.floats(min_value=0.001, max_value=1e5),
)
precios = st.one_of(st.floats(min_value=0, max_value=1e5), st.just(float("nan")))
# Sin los contraejemplos conocidos: cantidad negativa (volumétrico) y precio 0 (ambos caminos).
cantidades_no_negativas = st.one_of(
    st.integers(0, 100_000).map(float),
    st.floats(min_value=0.001, max_value=1e5),
)
precios_sin_cero = st.one_of(st.floats(min_value=0.001, max_value=1e5), st.just(float("nan")))


@pytest.fixture(scope="module")
def directorio_tmp(tmp_path_factory):
    ruta = tmp_path_factory.mktemp("preproceso")
    (ruta / "ml_model").mkdir()
    return ruta


def _correr_volumetrico(directorio, filas):
    df = pd.DataFrame(filas, columns=["unit_of_measurement", "total_quantity", "price_sar"])
    df["type_of_product"] = "T"
    df["fila"] = range(len(df))
    with en_directorio(directorio):
        preproceso.process_volumetric_data(df.copy())
        salida = pd.read_csv("ml_model/competitor_volumetric_processed.csv", float_precision="round_trip")
    return df, salida


def _correr_unitario(directorio, filas):
    df = pd.DataFrame(filas, columns=["total_quantity", "price_sar"])
    df["type_of_product"] = "T"
    df["fila"] = range(len(df))
    with en_directorio(directorio):
        preproceso.process_unit_data(df.copy())
        salida = pd.read_csv("ml_model/competitor_unit_processed.csv", float_precision="round_trip")
    return df, salida


filas_volumetricas = st.lists(st.tuples(st.sampled_from(UNIDADES), cantidades, precios), max_size=30)
filas_volumetricas_sin_bugs = st.lists(
    st.tuples(st.sampled_from(UNIDADES), cantidades_no_negativas, precios_sin_cero), max_size=30
)


def _validas_volumetricas(entrada):
    unidad = entrada["unit_of_measurement"].str.lower().str.strip()
    return entrada[
        unidad.isin(LITROS_POR_UNIDAD) & (entrada["price_sar"] > 0) & (entrada["total_quantity"] > 0)
    ]


@given(filas=filas_volumetricas)
def test_volumetrico_conversion_a_litros_y_precio_por_litro(directorio_tmp, filas):
    _, salida = _correr_volumetrico(directorio_tmp, filas)
    assert set(salida["unit_of_measurement"]) <= set(LITROS_POR_UNIDAD), "solo ml, l y fl oz; nunca g"
    for _, fila in salida.iterrows():
        esperado = fila["total_quantity"] * LITROS_POR_UNIDAD[fila["unit_of_measurement"]]
        assert fila["volume_liters"] == pytest.approx(esperado, rel=1e-12)
        assert math.isfinite(fila["price_per_liter"])
        assert fila["price_per_liter"] == pytest.approx(fila["price_sar"] / fila["volume_liters"], rel=1e-12)


@given(filas=filas_volumetricas_sin_bugs)
def test_volumetrico_conserva_exactamente_las_filas_validas(directorio_tmp, filas):
    """La salida tiene exactamente las filas válidas (unidad de volumen, cantidad > 0, precio > 0)."""
    entrada, salida = _correr_volumetrico(directorio_tmp, filas)
    assert sorted(salida["fila"]) == sorted(_validas_volumetricas(entrada)["fila"])


@given(filas=filas_volumetricas_sin_bugs)
def test_volumetrico_volumen_y_precio_por_litro_positivos(directorio_tmp, filas):
    _, salida = _correr_volumetrico(directorio_tmp, filas)
    assert (salida["volume_liters"] > 0).all()
    assert (salida["price_per_liter"] > 0).all()


@pytest.mark.xfail(
    strict=True,
    reason="BUG: process_volumetric_data no descarta cantidades <= 0 (process_unit_data sí). "
    "Contraejemplo: ml, total_quantity=-500, price_sar=1 -> volume_liters=-0.5 y "
    "price_per_liter=-2.0 entran al entrenamiento (hallazgo para pi-dev)",
)
@settings(phases=[Phase.explicit])
@given(filas=filas_volumetricas)
@example(filas=[("ml", -500.0, 1.0)])
def test_bug_volumetrico_cantidad_negativa(directorio_tmp, filas):
    entrada, salida = _correr_volumetrico(directorio_tmp, filas)
    assert sorted(salida["fila"]) == sorted(_validas_volumetricas(entrada)["fila"])


@pytest.mark.xfail(
    strict=True,
    reason="BUG: process_volumetric_data no descarta precio 0 (C18). Contraejemplo: ml, "
    "total_quantity=500, price_sar=0 -> price_per_liter=0.0 entra al entrenamiento y baja el "
    "p10 del tipo de producto (hallazgo para pi-dev)",
)
@settings(phases=[Phase.explicit])
@given(filas=filas_volumetricas)
@example(filas=[("ml", 500.0, 0.0)])
def test_bug_volumetrico_precio_cero(directorio_tmp, filas):
    entrada, salida = _correr_volumetrico(directorio_tmp, filas)
    assert sorted(salida["fila"]) == sorted(_validas_volumetricas(entrada)["fila"])


cantidades_unitarias = st.one_of(
    cantidades,
    st.sampled_from(["12", "0", "-3", "abc", "", "1e3", " 7 "]),
)
filas_unitarias = st.lists(st.tuples(cantidades_unitarias, precios), max_size=30)
filas_unitarias_sin_bugs = st.lists(st.tuples(cantidades_unitarias, precios_sin_cero), max_size=30)


def _validas_unitarias(entrada):
    cantidad = pd.to_numeric(entrada["total_quantity"], errors="coerce")
    return entrada[(cantidad > 0) & (entrada["price_sar"] > 0)]


@given(filas=filas_unitarias)
def test_unitario_cantidad_positiva_y_precio_por_pieza(directorio_tmp, filas):
    _, salida = _correr_unitario(directorio_tmp, filas)
    # Un CSV vacío se lee con columnas object; con filas, ya son float.
    cantidad = salida["total_quantity"].astype(float)
    precio_por_pieza = salida["price_per_item"].astype(float)
    assert (cantidad > 0).all()
    assert np.isfinite(precio_por_pieza).all()
    np.testing.assert_allclose(precio_por_pieza, salida["price_sar"].astype(float) / cantidad, rtol=1e-12)


@given(filas=filas_unitarias_sin_bugs)
def test_unitario_conserva_exactamente_las_filas_validas(directorio_tmp, filas):
    """La salida tiene exactamente las filas válidas (cantidad numérica > 0, precio > 0)."""
    entrada, salida = _correr_unitario(directorio_tmp, filas)
    assert sorted(salida["fila"]) == sorted(_validas_unitarias(entrada)["fila"])


@pytest.mark.xfail(
    strict=True,
    reason="BUG: process_unit_data no descarta precio 0 (C18). Contraejemplo: total_quantity=12, "
    "price_sar=0 -> price_per_item=0.0 entra al entrenamiento (hallazgo para pi-dev)",
)
@settings(phases=[Phase.explicit])
@given(filas=filas_unitarias)
@example(filas=[(12.0, 0.0)])
def test_bug_unitario_precio_cero(directorio_tmp, filas):
    entrada, salida = _correr_unitario(directorio_tmp, filas)
    assert sorted(salida["fila"]) == sorted(_validas_unitarias(entrada)["fila"])
