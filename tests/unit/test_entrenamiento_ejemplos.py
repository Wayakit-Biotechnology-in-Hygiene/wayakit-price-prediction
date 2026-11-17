"""Ejemplos concretos de ``ml_model/2_train_models.py``: filtro p10–p90 y cotizaciones."""
import numpy as np
import pandas as pd
import pytest

from tests.conftest import cargar_modulo

entrenamiento = cargar_modulo("ml_model/2_train_models.py")
filtrar = entrenamiento.filter_outliers_with_percentiles

pytestmark = pytest.mark.unit


def test_filtro_quita_extremos_por_grupo():
    df = pd.DataFrame(
        {
            "type_of_product": ["A"] * 11 + ["B"],
            "price_per_liter": [float(v) for v in range(11)] + [99.0],
        }
    )
    salida = filtrar(df, "price_per_liter")
    # Grupo A (0..10): p10 = 1 y p90 = 9 -> quedan 1..9. Grupo B (1 fila) se conserva.
    assert salida.loc[salida["type_of_product"] == "A", "price_per_liter"].tolist() == [float(v) for v in range(1, 10)]
    assert salida.loc[salida["type_of_product"] == "B", "price_per_liter"].tolist() == [99.0]
    assert "approved_quote_price" in salida and (salida["approved_quote_price"] == 0.0).all()


def test_filtro_no_es_idempotente_por_diseno():
    """Documenta el comportamiento: aplicar el filtro dos veces recorta otra vez los extremos."""
    df = pd.DataFrame({"type_of_product": ["A"] * 20, "price_per_liter": [float(v) for v in range(1, 21)]})
    una_vez = filtrar(df, "price_per_liter")
    dos_veces = filtrar(una_vez.drop(columns="approved_quote_price"), "price_per_liter")
    assert len(dos_veces) < len(una_vez)


def test_filtro_descarta_filas_sin_tipo_de_producto():
    """Documenta el comportamiento: groupby ignora type_of_product vacío, sus límites quedan NaN
    y esas filas se pierden sin aviso (en los datos de F1 no hay ninguna)."""
    df = pd.DataFrame({"type_of_product": ["A", None], "price_per_liter": [5.0, 5.0]})
    salida = filtrar(df, "price_per_liter")
    assert salida["type_of_product"].tolist() == ["A"]


@pytest.mark.xfail(
    strict=True,
    reason="BUG: un type_of_product con exactamente 2 filas de precios distintos se queda sin filas. Contraejemplo: "
    "'A' con [1.0, 2.0] -> límites 1.1 y 1.9 -> 0 filas (hallazgo para pi-dev)",
)
def test_grupo_de_dos_precios_no_desaparece():
    df = pd.DataFrame({"type_of_product": ["A", "A"], "price_per_liter": [1.0, 2.0]})
    assert len(filtrar(df, "price_per_liter")) > 0


def _productos(ids, subindustrias):
    return pd.DataFrame(
        {
            "Product_ID": ids,
            "Industry": ["Transportation"] * len(ids),
            "SubIndustry": subindustrias,
            "Generic product type": ["Cleaner"] * len(ids),
            "Type_of_product": ["T"] * len(ids),
        }
    )


def test_preparar_cotizaciones_canal_y_columnas():
    cotizaciones = pd.DataFrame(
        {"Product_ID ": ["P1", "P2", "P3", "PX"], "approved_quote_price": [10.0, 20.0, 30.0, 40.0]}
    )
    productos = _productos(["P1", "P2", "P3"], ["Home", "Hotels", "Pets"])
    base = entrenamiento.prepare_wayakit_training_data(cotizaciones, productos)
    assert base.sort_values("Product_ID")["channel"].tolist() == ["B2C", "B2B", "B2C"]
    assert (base["company"] == "Wayakit").all()
    assert len(base) == 3  # PX no tiene producto: el join es inner


def test_preparar_cotizaciones_canal_distingue_mayusculas():
    """Documenta el comportamiento: 'home' en minúsculas cae en B2B (misma trampa que
    'Land transportation' en el scraper). En los datos de F1 todas vienen capitalizadas."""
    cotizaciones = pd.DataFrame({"Product_ID": ["P1"], "approved_quote_price": [10.0]})
    base = entrenamiento.prepare_wayakit_training_data(cotizaciones, _productos(["P1"], ["home"]))
    assert base["channel"].tolist() == ["B2B"]


@pytest.mark.xfail(
    strict=True,
    reason="BUG: si un SKU se repite en wayakit_products_to_predict_odoo.csv, el merge duplica sus "
    "cotizaciones y pesan doble en el entrenamiento. Contraejemplo: 1 cotización de 'P1' y 'P1' "
    "dos veces en productos -> 2 filas. En los datos de F1 hay 4 SKU repetidos y 162 cotizaciones "
    "dan 163 filas (hallazgo para pi-dev)",
)
def test_cada_cotizacion_aparece_una_sola_vez():
    cotizaciones = pd.DataFrame({"Product_ID": ["P1"], "approved_quote_price": [10.0]})
    productos = _productos(["P1", "P1"], ["Home", "Home"])
    assert len(entrenamiento.prepare_wayakit_training_data(cotizaciones, productos)) == 1


def test_train_and_evaluate_es_determinista():
    """random_state=42: mismos datos -> mismos árboles (base de las pruebas diferenciales).

    Ojo: con n_jobs=-1, ``predict`` suma los árboles en hilos y el orden de la suma cambia entre
    llamadas, así que la predicción puede diferir en el último bit (~1e-15) aun con el mismo
    modelo. Las comparaciones de predicciones deben usar tolerancia (o n_jobs=1).
    """
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"volume_liters": rng.uniform(0.1, 20, 60), "t_B": rng.integers(0, 2, 60)})
    y = pd.Series(3 + 2 * X["t_B"] + rng.normal(0, 0.1, 60))
    a = entrenamiento.train_and_evaluate(X, y, "prueba")
    b = entrenamiento.train_and_evaluate(X, y, "prueba")
    for arbol_a, arbol_b in zip(a.estimators_, b.estimators_):
        np.testing.assert_array_equal(arbol_a.tree_.threshold, arbol_b.tree_.threshold)
        np.testing.assert_array_equal(arbol_a.tree_.value, arbol_b.tree_.value)
    np.testing.assert_allclose(a.predict(X), b.predict(X), rtol=1e-12, atol=0)
    a.set_params(n_jobs=1)
    b.set_params(n_jobs=1)
    np.testing.assert_array_equal(a.predict(X), b.predict(X))
