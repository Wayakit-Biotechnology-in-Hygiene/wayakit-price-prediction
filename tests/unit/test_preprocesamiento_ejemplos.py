"""Ejemplos de ``ml_model/1a_preprocess_data.py``, incluido ``main()`` con CSV sintéticos.

Todo corre en ``tmp_path`` (``monkeypatch.chdir``): los scripts leen y escriben rutas relativas.
"""
import pandas as pd
import pytest

from tests.conftest import cargar_modulo

preproceso = cargar_modulo("ml_model/1a_preprocess_data.py")

pytestmark = [
    pytest.mark.unit,
    pytest.mark.filterwarnings("ignore::pandas.errors.SettingWithCopyWarning"),
]

COLUMNAS_SCRAPER = [
    "date", "industry", "subindustry", "type_of_product", "generic_product_type", "product",
    "price_sar", "company", "source", "url", "unit_of_measurement", "total_quantity", "channel",
]
COLUMNAS_ODOO = [
    "Date", "Channel", "Type of product", "Product category", "Sub industry", "Industry",
    "Generic product type", "Total quantity", "Unit of measurement [mL,g,units]",
    "Price per unit SAR", "Company",
]


# Fila de competitor.product (Odoo) en mL; se incluye siempre para que el concat de main() no
# reciba un DataFrame vacío (pandas avisa de un cambio futuro de dtypes en ese caso).
FILA_ODOO = {
    "Date": "10/11/2022", "Channel": "B2B", "Type of product": "A1-Cleaner", "Product category": "x",
    "Sub industry": "Aviation", "Industry": "Transportation", "Generic product type": "Cleaner",
    "Total quantity": 500.0, "Unit of measurement [mL,g,units]": "mL",
    "Price per unit SAR": "SAR 1,000.50", "Company": "Callington",
}


def _fila_scraper(producto, precio, unidad, cantidad):
    return {
        "date": "2026-11-13", "industry": "Home care", "subindustry": "Home", "type_of_product": "H1-Cleaner",
        "generic_product_type": "Cleaner", "product": producto, "price_sar": precio, "company": "Marca",
        "source": "Amazon", "url": "https://example.invalid/p", "unit_of_measurement": unidad,
        "total_quantity": cantidad, "channel": "B2C",
    }


def _correr_main(tmp_path, monkeypatch, filas_scraper, filas_odoo=(FILA_ODOO,)):
    (tmp_path / "scraper").mkdir()
    (tmp_path / "ml_model").mkdir()
    pd.DataFrame(list(filas_scraper), columns=COLUMNAS_SCRAPER).to_csv(tmp_path / "scraper/competitors_complete.csv", index=False)
    pd.DataFrame(list(filas_odoo), columns=COLUMNAS_ODOO).to_csv(tmp_path / "ml_model/competitor_products_from_odoo.csv", index=False)
    monkeypatch.chdir(tmp_path)
    preproceso.main()
    volumen = pd.read_csv("ml_model/competitor_volumetric_processed.csv")
    unidades = pd.read_csv("ml_model/competitor_unit_processed.csv")
    return volumen, unidades


def test_main_separa_volumen_y_piezas(tmp_path, monkeypatch):
    volumen, unidades = _correr_main(
        tmp_path,
        monkeypatch,
        [
            _fila_scraper("Cleaner 500 ml", 10.0, "ml", 500.0),
            _fila_scraper("Cleaner 2 L", 20.0, "L", 2.0),
            _fila_scraper("Cleaner 32 fl oz", 30.0, "fl oz", 32.0),
            _fila_scraper("Powder 500 g", 5.0, "g", 500.0),
            _fila_scraper("Wipes 80", 8.0, "units", 80.0),
        ],
        [FILA_ODOO],
    )
    assert sorted(volumen["unit_of_measurement"]) == ["fl oz", "l", "ml", "ml"]
    por_unidad = dict(zip(volumen["unit_of_measurement"] + "|" + volumen["company"], volumen["price_per_liter"]))
    assert por_unidad["ml|Marca"] == pytest.approx(20.0)
    assert por_unidad["l|Marca"] == pytest.approx(10.0)
    assert por_unidad["fl oz|Marca"] == pytest.approx(30.0 / (32 * 0.0295735))
    assert por_unidad["ml|Callington"] == pytest.approx(1000.50 / 0.5)  # limpia "SAR" y la coma
    assert unidades["price_per_item"].tolist() == [pytest.approx(0.1)]


@pytest.mark.xfail(
    strict=True,
    reason="BUG: parse_volume_string devuelve unit='kg' (y 'litre', por el otro bug) y 1a solo "
    "reconoce ml/l/fl oz/g como volumen; esas filas caen al modelo por pieza como si fueran "
    "piezas. Contraejemplo: 'Powder 2 kg', 30 SAR -> competitor_unit_processed.csv con "
    "total_quantity=2 y price_per_item=15 (hallazgo para pi-dev)",
)
@pytest.mark.parametrize("unidad", ["kg", "litre"])
def test_main_no_trata_masa_o_volumen_como_piezas(tmp_path, monkeypatch, unidad):
    _, unidades = _correr_main(tmp_path, monkeypatch, [_fila_scraper("Powder 2 " + unidad, 30.0, unidad, 2.0)])
    assert unidades.empty


def test_process_unit_data_descarta_cantidades_invalidas(tmp_path, monkeypatch):
    (tmp_path / "ml_model").mkdir()
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame({"total_quantity": ["10", "0", "-2", "abc", None], "price_sar": [5.0, 5.0, 5.0, 5.0, 5.0]})
    preproceso.process_unit_data(df)
    salida = pd.read_csv("ml_model/competitor_unit_processed.csv")
    assert salida["total_quantity"].tolist() == [10.0]
    assert salida["price_per_item"].tolist() == [0.5]


def test_process_volumetric_data_sin_filas_validas_no_falla(tmp_path, monkeypatch):
    (tmp_path / "ml_model").mkdir()
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame({"unit_of_measurement": ["g"], "total_quantity": [100.0], "price_sar": [5.0]})
    preproceso.process_volumetric_data(df)
    assert pd.read_csv("ml_model/competitor_volumetric_processed.csv").empty
