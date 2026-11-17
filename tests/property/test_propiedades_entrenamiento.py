"""Propiedades de ``ml_model/2_train_models.py`` (filtro p10–p90 y datos de cotizaciones).

Solo se importan funciones; ``main()`` no corre (está protegido por ``__name__``) y el logger
de producción se reemplaza por uno sin archivos (ver ``tests/conftest.py``).
"""
import numpy as np
import pandas as pd
import pytest
from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st

from tests.conftest import cargar_modulo

entrenamiento = cargar_modulo("ml_model/2_train_models.py")
filtrar = entrenamiento.filter_outliers_with_percentiles

pytestmark = pytest.mark.property

METRICA = "price_per_liter"

precios = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)
filas = st.lists(
    st.tuples(st.sampled_from(["A", "B", "C", "D"]), precios),
    max_size=60,
)


def _df(filas_generadas):
    df = pd.DataFrame(filas_generadas, columns=["type_of_product", METRICA])
    # Con 0 filas pandas deja la columna como object; en producción viene de un CSV y es float.
    df[METRICA] = df[METRICA].astype(float)
    df["fila"] = range(len(df))  # identificador estable para comparar filas
    return df


def _cuantil(valores, p):
    return float(np.quantile(np.asarray(valores, dtype=float), p))


@given(filas, st.sampled_from([(0.10, 0.90), (0.0, 1.0), (0.25, 0.75), (0.05, 0.95)]))
def test_salida_es_subconjunto_exacto_de_la_entrada(filas_generadas, percentiles):
    df = _df(filas_generadas)
    salida = filtrar(df, METRICA, *percentiles)
    assert set(salida.index) <= set(df.index)
    columnas = ["type_of_product", METRICA, "fila"]
    pd.testing.assert_frame_equal(salida[columnas], df.loc[salida.index, columnas])
    assert (salida["approved_quote_price"] == 0.0).all()


@given(filas)
def test_no_modifica_la_entrada(filas_generadas):
    df = _df(filas_generadas)
    copia = df.copy()
    filtrar(df, METRICA)
    pd.testing.assert_frame_equal(df, copia)


@given(filas, st.sampled_from([(0.10, 0.90), (0.25, 0.75), (0.05, 0.95)]))
def test_equivale_a_filtrar_por_cuantiles_de_cada_grupo(filas_generadas, percentiles):
    """Equivalencia con una implementación de referencia (numpy, interpolación lineal).

    Las filas a menos de 1e-9 (relativo) de un límite se omiten: pandas y numpy interpolan con
    fórmulas distintas y pueden diferir en el último bit justo en el borde.
    """
    p_bajo, p_alto = percentiles
    df = _df(filas_generadas)
    conservadas = set(filtrar(df, METRICA, p_bajo, p_alto)["fila"])
    for _, grupo in df.groupby("type_of_product"):
        bajo = _cuantil(grupo[METRICA], p_bajo)
        alto = _cuantil(grupo[METRICA], p_alto)
        for fila, valor in zip(grupo["fila"], grupo[METRICA]):
            tolerancia = 1e-9 * max(1.0, abs(bajo), abs(alto))
            if bajo + tolerancia < valor < alto - tolerancia:
                assert fila in conservadas, (fila, valor, bajo, alto)
            elif valor < bajo - tolerancia or valor > alto + tolerancia:
                assert fila not in conservadas, (fila, valor, bajo, alto)


@given(filas)
def test_percentiles_0_y_1_conservan_todo(filas_generadas):
    df = _df(filas_generadas)
    assert len(filtrar(df, METRICA, 0.0, 1.0)) == len(df)


@given(filas)
def test_filtrar_dos_veces_solo_puede_quitar_mas(filas_generadas):
    """No es idempotente por diseño: el p10 del conjunto ya filtrado es mayor (ver el ejemplo en
    tests/unit/test_entrenamiento_ejemplos.py). La propiedad que sí vale: f(f(x)) ⊆ f(x)."""
    una_vez = filtrar(_df(filas_generadas), METRICA)
    dos_veces = filtrar(una_vez.drop(columns="approved_quote_price"), METRICA)
    assert set(dos_veces["fila"]) <= set(una_vez["fila"])


@given(filas)
def test_grupos_con_un_solo_valor_distinto_se_conservan_completos(filas_generadas):
    df = _df(filas_generadas)
    salida = filtrar(df, METRICA)
    for tipo, grupo in df.groupby("type_of_product"):
        if grupo[METRICA].nunique() == 1:
            assert (salida["type_of_product"] == tipo).sum() == len(grupo)


@st.composite
def filas_sin_par_de_precios_distintos(draw):
    """Como ``filas``, pero ningún grupo tiene exactamente 2 filas con precios distintos.

    Ese es el único caso en que un grupo desaparece (con n >= 3 filas siempre hay un índice entero
    entre las posiciones de p10 y p90, y su valor queda dentro de los límites). Se excluye aquí
    para que la propiedad general siga corriendo; el bug tiene su propia prueba mínima abajo.
    """
    grupo = st.one_of(
        st.lists(precios, min_size=1, max_size=1),
        precios.map(lambda p: [p, p]),
        st.lists(precios, min_size=3, max_size=15),
    )
    por_tipo = draw(st.dictionaries(st.sampled_from(["A", "B", "C", "D"]), grupo, max_size=4))
    resultado = [(tipo, valor) for tipo, valores in por_tipo.items() for valor in valores]
    return draw(st.permutations(resultado))


@given(filas_sin_par_de_precios_distintos())
def test_ningun_grupo_desaparece(filas_generadas):
    df = _df(filas_generadas)
    salida = filtrar(df, METRICA)
    assert set(salida["type_of_product"]) == set(df["type_of_product"])


@pytest.mark.xfail(
    strict=True,
    reason="BUG: un type_of_product con exactamente 2 filas de precios distintos pierde las 2 "
    "(p10 > mínimo y p90 < máximo). Contraejemplo: grupo 'A' con [1.0, 2.0] -> límites 1.1 y "
    "1.9 -> 0 filas. En los datos de F1, 14 de 151 tipos volumétricos y 1 de 15 unitarios se "
    "quedan sin competidores (hallazgo para pi-dev)",
)
@settings(phases=[Phase.explicit])
@given(filas)
@example([("A", 1.0), ("A", 2.0)])
def test_bug_grupo_de_dos_precios_distintos_desaparece(filas_generadas):
    df = _df(filas_generadas)
    salida = filtrar(df, METRICA)
    assert set(salida["type_of_product"]) == set(df["type_of_product"])


@given(filas, st.lists(st.sampled_from([np.nan]), min_size=1, max_size=5))
def test_filas_sin_precio_nunca_pasan(filas_generadas, nans):
    df = _df(filas_generadas + [("A", v) for v in nans])
    salida = filtrar(df, METRICA)
    assert not salida[METRICA].isna().any()


# --- prepare_wayakit_training_data --------------------------------------------------------------

SUBINDUSTRIAS = ["Home", "Automotive", "Pets", "Hotels", "Aviation", "Airports", "Restaurants"]


@st.composite
def cotizaciones_y_productos(draw):
    ids = draw(st.lists(st.sampled_from([f"FP-{i:03d}" for i in range(8)]), unique=True, max_size=8))
    productos = pd.DataFrame(
        {
            "Product_ID": ids,
            "Industry": ["Transportation"] * len(ids),
            "SubIndustry": [draw(st.sampled_from(SUBINDUSTRIAS)) for _ in ids],
            "Generic product type": ["Cleaner"] * len(ids),
            "Type_of_product": [f"T-{i}" for i in ids],
            "Volume_Liters": [1.0] * len(ids),
        }
    )
    n = draw(st.integers(0, 20))
    cotizaciones = pd.DataFrame(
        {
            "Product_ID": [draw(st.sampled_from([f"FP-{i:03d}" for i in range(10)])) for _ in range(n)],
            "Description": ["x"] * n,
            "approved_quote_price": [draw(st.floats(0, 1e5)) for _ in range(n)],
        }
    )
    return cotizaciones, productos


@given(cotizaciones_y_productos())
def test_preparar_cotizaciones_reglas_de_canal_y_compania(datos):
    cotizaciones, productos = datos
    esperadas = int(cotizaciones["Product_ID"].isin(productos["Product_ID"]).sum())
    base = entrenamiento.prepare_wayakit_training_data(cotizaciones.copy(), productos.copy())
    # Con SKU únicos, cada cotización con producto aparece exactamente una vez.
    assert len(base) == esperadas
    assert (base["company"] == "Wayakit").all()
    es_b2c = base["subindustry"].isin(["Home", "Automotive", "Pets"])
    assert (base.loc[es_b2c, "channel"] == "B2C").all()
    assert (base.loc[~es_b2c, "channel"] == "B2B").all()
    assert {"industry", "subindustry", "generic_product_type", "type_of_product"} <= set(base.columns)
