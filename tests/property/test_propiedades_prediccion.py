"""Propiedades de ``generate_predictions`` en ``ml_model/3_predicted_prices.py``.

Es la regla de negocio más importante del pipeline: el precio sugerido nunca baja del piso de
margen ``costo × 1.30`` y, con 3 o más competidores, se recorta al rango de mercado (el piso
siempre gana). ``generate_predictions`` no hace E/S (solo registra en el log), así que se llama
directo con modelos falsos y estadísticas de mercado generadas por Hypothesis:

- ``ModeloFalso.predict`` devuelve, en orden, el precio por litro o por pieza generado para
  cada producto que pasa por ese modelo (se llama una vez por producto).
- ``estimators_`` es una lista de árboles falsos con ``predict``; solo la usa
  ``calculate_confidence``.

Los productos generados se parecen a ``wayakit_products_to_predict_odoo.csv`` de F1: volumen en
litros o piezas, y productos sin ninguno de los dos con 0 (en F1 hay 26 así; el costo puede ser 0).
"""
import math

import numpy as np
import pandas as pd
import pytest
from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st

from tests.conftest import cargar_modulo

prediccion = cargar_modulo("ml_model/3_predicted_prices.py")

pytestmark = pytest.mark.property

TIPOS = ["T0", "T1", "T2", "T3"]
SUBINDUSTRIAS = ["Home", "Automotive", "Pets", "Hotels", "Aviation"]
COLUMNAS_NUMERICAS = [
    "predicted_price", "predicted_price_per_unit", "porcentaje_de_ganancia", "model_confidence",
    "market_min_found", "market_max_found", "competitors_count",
]
FACTOR_PISO = 1.30


class ArbolFalso:
    def __init__(self, valor):
        self.valor = valor

    def predict(self, X):
        return np.full(len(X), self.valor)


class ModeloFalso:
    def __init__(self, precios, valores_arboles):
        self._precios = list(precios)
        self.estimators_ = [ArbolFalso(v) for v in valores_arboles]

    def predict(self, X):
        assert len(X) == 1, "generate_predictions predice un producto a la vez"
        return np.array([self._precios.pop(0)])


positivos = st.floats(min_value=0.01, max_value=1e4, allow_nan=False, allow_infinity=False)


@st.composite
def productos(draw):
    tipo_de_medida = draw(st.sampled_from(["volumen", "piezas", "ambos", "ninguno"]))
    volumen = draw(st.floats(0.01, 1000)) if tipo_de_medida in ("volumen", "ambos") else draw(
        st.sampled_from([0.0, float("nan")])
    )
    if tipo_de_medida in ("piezas", "ambos"):
        piezas = float(draw(st.integers(1, 1000)))
    elif tipo_de_medida == "volumen":
        piezas = draw(st.sampled_from([0.0, float("nan")]))
    else:
        piezas = 0.0  # sin volumen ni piezas: Odoo manda 0 (el caso NaN tiene su prueba aparte)
    return {
        "Product_ID": f"FP-{draw(st.integers(0, 999)):03d}",
        "Product_Name": "Producto de prueba",
        "type_of_product": draw(st.sampled_from(TIPOS)),
        "subindustry": draw(st.sampled_from(SUBINDUSTRIAS)),
        "industry": "Transportation",
        "generic_product_type": "Cleaner",
        "company": "Wayakit",
        "channel": "B2B",
        "Volume_Liters": volumen,
        "Pack_quantity_Units": piezas,
        # Costo 0 existe en F1 (6 productos); fuera de eso, al menos 0.001 SAR (un subnormal
        # haría que el margen porcentual se desborde a inf, que no es un caso real).
        "Unit_cost_SAR": draw(st.one_of(st.just(0.0), st.floats(0.001, 1e4))),
        "precio_modelo": draw(positivos),  # precio por litro o por pieza que devuelve el modelo
    }


@st.composite
def estadisticas_de_mercado(draw):
    filas = []
    for tipo in draw(st.lists(st.sampled_from(TIPOS), unique=True, max_size=len(TIPOS))):
        minimo = draw(st.floats(0.01, 1e3))
        vol_min = draw(st.floats(0, 100))
        filas.append(
            {
                "type_of_product": tipo,
                "market_min": minimo,
                "market_max": minimo + draw(st.floats(0, 1e3)),
                "competitor_count": draw(st.integers(0, 20)),
                "vol_min": vol_min,
                "vol_max": vol_min + draw(st.floats(0, 1000)),
            }
        )
    columnas = ["type_of_product", "market_min", "market_max", "competitor_count", "vol_min", "vol_max"]
    return pd.DataFrame(filas, columns=columnas)


def _es_volumetrico(p):
    return pd.notna(p["Volume_Liters"]) and p["Volume_Liters"] > 0


def _es_por_piezas(p):
    return pd.notna(p["Pack_quantity_Units"]) and p["Pack_quantity_Units"] > 0


def _correr(lista_productos, stats_vol, stats_unit, arboles):
    precios_vol = [p["precio_modelo"] for p in lista_productos if _es_volumetrico(p)]
    precios_unit = [p["precio_modelo"] for p in lista_productos if not _es_volumetrico(p) and _es_por_piezas(p)]
    df = pd.DataFrame(lista_productos).drop(columns="precio_modelo")
    reporte = prediccion.generate_predictions(
        df,
        ModeloFalso(precios_vol, arboles),
        ModeloFalso(precios_unit, arboles),
        ["volume_liters"],
        ["total_quantity"],
        stats_vol,
        stats_unit,
    )
    return reporte


def _mercado_esperado(producto, stats_vol, stats_unit):
    """(cantidad, competidores, mínimo total, máximo total) como los calcula producción."""
    if _es_volumetrico(producto):
        cantidad, stats = producto["Volume_Liters"], stats_vol
    elif _es_por_piezas(producto):
        cantidad, stats = producto["Pack_quantity_Units"], stats_unit
    else:
        return None, 0, 0.0, 0.0
    fila = stats[stats["type_of_product"] == producto["type_of_product"]]
    if fila.empty:
        return cantidad, 0, 0.0, 0.0
    fila = fila.iloc[0]
    return cantidad, int(fila["competitor_count"]), fila["market_min"] * cantidad, fila["market_max"] * cantidad


@given(
    lista_productos=st.lists(productos(), min_size=1, max_size=6),
    stats_vol=estadisticas_de_mercado(),
    stats_unit=estadisticas_de_mercado(),
    arboles=st.lists(positivos, min_size=1, max_size=5),
)
def test_piso_de_margen_y_recorte_al_mercado(lista_productos, stats_vol, stats_unit, arboles):
    reporte = _correr(lista_productos, stats_vol, stats_unit, arboles)

    assert len(reporte) == len(lista_productos)
    for columna in COLUMNAS_NUMERICAS:
        assert np.isfinite(reporte[columna].astype(float)).all(), f"NaN o infinito en {columna}"

    for producto, (_, fila) in zip(lista_productos, reporte.iterrows()):
        costo = producto["Unit_cost_SAR"]
        piso = costo * FACTOR_PISO
        precio = fila["predicted_price"]
        assert math.isfinite(precio)
        # Piso de margen (C17): nunca por debajo de round(costo × 1.30, 2).
        assert precio >= round(piso, 2) - 0.005, (producto, fila.to_dict())

        cantidad, competidores, minimo, maximo = _mercado_esperado(producto, stats_vol, stats_unit)
        assert fila["competitors_count"] == competidores
        tolerancia = 0.005 + 1e-9 * max(1.0, abs(minimo), abs(maximo), abs(piso))
        if competidores >= 3:
            # Recorte al mercado; el piso gana si queda por encima del máximo.
            assert minimo - tolerancia <= precio <= max(maximo, piso) + tolerancia, (producto, fila.to_dict())
        else:
            assert not fila["clamped_to_market"], (producto, fila.to_dict())
            # Sin recorte: max(precio del modelo, piso) al centavo. Tolerancia de medio centavo
            # porque producción redondea un float64 de numpy (0.015 -> 0.02) y Python redondea
            # su float con la representación exacta (0.015 -> 0.01).
            precio_modelo = producto["precio_modelo"] * cantidad if cantidad is not None else 0
            assert abs(precio - max(precio_modelo, piso)) <= tolerancia, (producto, fila.to_dict())


@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason="BUG menor: un producto sin volumen ni piezas con Pack_quantity_Units vacío (NaN) rompe "
    "generate_predictions con ValueError en int(NaN) al armar la columna 'volume'. En F1 no pasa "
    "(Odoo manda 0 en los 26 productos sin medida), pero un CSV con la celda vacía tumbaría el "
    "paso 3 completo (hallazgo para pi-dev)",
)
@settings(phases=[Phase.explicit])
@given(lista_productos=st.lists(productos(), min_size=1, max_size=1))
@example(
    lista_productos=[
        {
            "Product_ID": "FP-001", "Product_Name": "Producto de prueba", "type_of_product": "T0",
            "subindustry": "Home", "industry": "Transportation", "generic_product_type": "Cleaner",
            "company": "Wayakit", "channel": "B2C", "Volume_Liters": 0.0,
            "Pack_quantity_Units": float("nan"), "Unit_cost_SAR": 10.0, "precio_modelo": 1.0,
        }
    ]
)
def test_bug_producto_sin_medida_con_piezas_vacias(lista_productos):
    vacias = pd.DataFrame(
        columns=["type_of_product", "market_min", "market_max", "competitor_count", "vol_min", "vol_max"]
    )
    reporte = _correr(lista_productos, vacias, vacias, [1.0])
    assert reporte["predicted_price"].iloc[0] == round(10.0 * FACTOR_PISO, 2)
