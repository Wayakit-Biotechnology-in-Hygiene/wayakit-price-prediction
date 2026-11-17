"""E2E fuera de línea del flujo de ML: 1a → 1b (full) → 2 → 3 con los datos congelados de F1.

Corre los scripts reales como subprocesos en una copia temporal del código (ver
`tests/e2e/_helpers.py`) y revisa las salidas del flujo, no solo los códigos de salida:
los scripts tragan excepciones y salen con 0 aunque fallen.

No corre `odoo_api_*` ni scrapers: nada de AWS, Odoo, Apify ni Bedrock.
Se salta si falta `data/f1_antes_2026-11-15/` (carpeta ignorada por git).
"""
from __future__ import annotations

import math

import joblib
import numpy as np
import pandas as pd
import pytest

from tests.e2e import _helpers as h

pytestmark = pytest.mark.e2e

# Columnas de precio del reporte (`ml_model/3_predicted_prices.py:139-152`).
PRICE_COLUMNS = ["cost_per_unit", "predicted_price", "predicted_price_per_unit",
                 "porcentaje_de_ganancia", "market_min_found", "market_max_found", "model_confidence"]
FLOOR_FACTOR = 1.30  # `3_predicted_prices.py:135`
MIN_COMPETITORS_FOR_CLAMP = 3  # `3_predicted_prices.py:129`
# El reporte redondea a 2 decimales y pasa por CSV; 0.01 cubre el redondeo de ambos lados.
ROUND_TOL = 0.01


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    reason = h.data_missing_reason()
    if reason:
        pytest.skip(reason)
    workdir = tmp_path_factory.mktemp("e2e")
    result = h.prepare_and_run(workdir, label="E2E árbol de trabajo")
    # Queda en "Captured stdout" si alguna revisión falla después.
    print(result.trace())
    return result


@pytest.fixture(scope="module")
def report(run):
    return pd.read_csv(run.path(h.REPORT))


@pytest.fixture(scope="module")
def to_predict(run):
    return pd.read_csv(run.path(h.TO_PREDICT))


def _fail_rows(df: pd.DataFrame, mask: pd.Series, cols: list[str], titulo: str) -> str:
    bad = df.loc[mask, cols]
    return f"{titulo}: {int(mask.sum())} fila(s). Primeras:\n{bad.head(20).to_string()}"


def test_etapas_salen_bien_y_dejan_sus_salidas(run):
    assert run.ok, run.trace()
    for stage in h.STAGES:
        for rel in stage.outputs:
            assert run.path(rel).stat().st_size > 0, f"{rel} quedó vacío\n{run.trace()}"
    # log_config.py escribe en ../logs relativo a la copia: debe quedar dentro de tmp.
    assert (run.repo_dir.parent / "logs").is_dir(), "los logs no quedaron en <tmp>/logs"


def test_competidores_procesados_son_coherentes(run):
    vol = pd.read_csv(run.path(h.VOL_PROCESSED))
    unit = pd.read_csv(run.path(h.UNIT_PROCESSED))
    assert len(vol) > 0 and len(unit) > 0
    assert not vol["unit_of_measurement"].isin(["g"]).any(), "el volumétrico no debe traer gramos"
    assert set(vol["unit_of_measurement"]) <= {"ml", "l", "fl oz"}
    for df, col in ((vol, "price_per_liter"), (unit, "price_per_item")):
        values = df[col].to_numpy(dtype=float)
        assert np.isfinite(values).all(), f"{col} trae NaN o infinito"
    assert (vol["volume_liters"] > 0).all()
    assert (unit["total_quantity"] > 0).all()


def test_lista_a_predecir_sigue_la_regla_de_1b(run, to_predict):
    """1b deja los productos del catálogo cuyo `Type_of_product` (sin espacios) existe en la competencia."""
    vol = pd.read_csv(run.path(h.VOL_PROCESSED))
    unit = pd.read_csv(run.path(h.UNIT_PROCESSED))
    known = set(vol["type_of_product"].unique()) | set(unit["type_of_product"].unique())
    catalog = pd.read_csv(run.path("ml_model/wayakit_products.csv"), encoding="utf-8-sig")
    expected = catalog[catalog["Type_of_product"].str.strip().isin(known)]
    assert len(to_predict) > 0, "no quedó ningún producto predecible"
    assert to_predict["Product_ID"].astype(str).tolist() == expected["Product_ID"].astype(str).tolist()


def test_reporte_trae_una_fila_por_producto_predecible(report, to_predict):
    assert len(report) == len(to_predict)
    assert report["Product_ID"].astype(str).tolist() == to_predict["Product_ID"].astype(str).tolist()


def test_reporte_sin_nan_en_precios(report):
    missing = [c for c in PRICE_COLUMNS if c not in report.columns]
    assert not missing, f"faltan columnas en el reporte: {missing}"
    nan_mask = report[PRICE_COLUMNS].isna().any(axis=1)
    assert not nan_mask.any(), _fail_rows(report, nan_mask, ["Product_ID", *PRICE_COLUMNS], "precios con NaN")
    assert np.isfinite(report[PRICE_COLUMNS].to_numpy(dtype=float)).all()


def test_precio_final_respeta_el_piso_de_margen(report):
    # predicted_price = round(max(modelo, costo × 1.30), 2), así que nunca baja más de medio centavo.
    floor = report["cost_per_unit"] * FLOOR_FACTOR
    bad = report["predicted_price"] < floor - 0.005 - 1e-9
    assert not bad.any(), _fail_rows(report, bad, ["Product_ID", "cost_per_unit", "predicted_price"],
                                     f"precio final < costo × {FLOOR_FACTOR}")


def test_recorte_al_rango_de_mercado(report):
    assert (report["market_min_found"] <= report["market_max_found"] + 1e-9).all()
    few = report["competitors_count"] < MIN_COMPETITORS_FOR_CLAMP
    bad_flag = few & report["clamped_to_market"]
    assert not bad_flag.any(), _fail_rows(report, bad_flag, ["Product_ID", "competitors_count"],
                                          "recortado con menos de 3 competidores")

    # Con >= 3 competidores y precio del modelo > 0, el precio queda en [mín, máx] del mercado,
    # salvo que el piso de margen lo suba por encima del máximo (el piso siempre gana).
    clamped_rows = ~few & (report["predicted_price_per_unit"] > 0)
    assert clamped_rows.any(), "ningún producto entró al recorte; la revisión no probaría nada"
    floor = report["cost_per_unit"] * FLOOR_FACTOR
    upper = np.maximum(report["market_max_found"], floor)
    below = clamped_rows & (report["predicted_price"] < report["market_min_found"] - ROUND_TOL)
    above = clamped_rows & (report["predicted_price"] > upper + ROUND_TOL)
    cols = ["Product_ID", "predicted_price", "market_min_found", "market_max_found", "cost_per_unit"]
    assert not below.any(), _fail_rows(report, below, cols, "precio por debajo del mínimo de mercado")
    assert not above.any(), _fail_rows(report, above, cols, "precio por encima del máximo de mercado y del piso")


def test_confianza_y_conteos_en_rango(report):
    assert report["model_confidence"].between(0, 100).all()
    assert (report["competitors_count"] >= 0).all()
    assert report["clamped_to_market"].dtype == bool
    assert report["volume_outside_market"].dtype == bool


def test_modelos_guardados_y_cargables(run):
    model_dir = run.path(h.MODEL_DIR)
    vol_model = joblib.load(model_dir / "volumetric_model.joblib")
    vol_cols = joblib.load(model_dir / "volumetric_model_columns.joblib")
    unit_model = joblib.load(model_dir / "unit_model.joblib")
    unit_cols = joblib.load(model_dir / "unit_model_columns.joblib")
    assert type(vol_model).__name__ == "RandomForestRegressor"
    assert vol_model.n_features_in_ == len(vol_cols)
    assert unit_model.n_features_in_ == len(unit_cols)
    for name in ("vol_market_stats.joblib", "unit_market_stats.joblib"):
        stats = joblib.load(model_dir / name)
        assert {"type_of_product", "market_min", "market_max", "competitor_count"} <= set(stats.columns)
        assert len(stats) > 0 and not math.isnan(float(stats["market_min"].min()))
