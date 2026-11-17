"""Examples for ``ml_model/1a_preprocess_data.py``, including ``main()`` with synthetic CSVs.

Everything runs in ``tmp_path`` (``monkeypatch.chdir``): the scripts read and write relative paths.
"""
import pandas as pd
import pytest

from tests.conftest import load_module

preprocessing = load_module("ml_model/1a_preprocess_data.py")

pytestmark = [
    pytest.mark.unit,
    pytest.mark.filterwarnings("ignore::pandas.errors.SettingWithCopyWarning"),
]

SCRAPER_COLUMNS = [
    "date", "industry", "subindustry", "type_of_product", "generic_product_type", "product",
    "price_sar", "company", "source", "url", "unit_of_measurement", "total_quantity", "channel",
]
ODOO_COLUMNS = [
    "Date", "Channel", "Type of product", "Product category", "Sub industry", "Industry",
    "Generic product type", "Total quantity", "Unit of measurement [mL,g,units]",
    "Price per unit SAR", "Company",
]


# competitor.product (Odoo) row in mL; always included so the concat in main() does not
# receive an empty DataFrame (pandas warns about a future dtype change in that case).
ODOO_ROW = {
    "Date": "10/11/2022", "Channel": "B2B", "Type of product": "A1-Cleaner", "Product category": "x",
    "Sub industry": "Aviation", "Industry": "Transportation", "Generic product type": "Cleaner",
    "Total quantity": 500.0, "Unit of measurement [mL,g,units]": "mL",
    "Price per unit SAR": "SAR 1,000.50", "Company": "Callington",
}


def _scraper_row(product, price, unit, quantity):
    return {
        "date": "2026-11-13", "industry": "Home care", "subindustry": "Home", "type_of_product": "H1-Cleaner",
        "generic_product_type": "Cleaner", "product": product, "price_sar": price, "company": "Brand",
        "source": "Amazon", "url": "https://example.invalid/p", "unit_of_measurement": unit,
        "total_quantity": quantity, "channel": "B2C",
    }


def _run_main(tmp_path, monkeypatch, scraper_rows, odoo_rows=(ODOO_ROW,)):
    (tmp_path / "scraper").mkdir()
    (tmp_path / "ml_model").mkdir()
    pd.DataFrame(list(scraper_rows), columns=SCRAPER_COLUMNS).to_csv(tmp_path / "scraper/competitors_complete.csv", index=False)
    pd.DataFrame(list(odoo_rows), columns=ODOO_COLUMNS).to_csv(tmp_path / "ml_model/competitor_products_from_odoo.csv", index=False)
    monkeypatch.chdir(tmp_path)
    preprocessing.main()
    volume = pd.read_csv("ml_model/competitor_volumetric_processed.csv")
    units = pd.read_csv("ml_model/competitor_unit_processed.csv")
    return volume, units


def test_main_splits_volume_and_pieces(tmp_path, monkeypatch):
    volume, units = _run_main(
        tmp_path,
        monkeypatch,
        [
            _scraper_row("Cleaner 500 ml", 10.0, "ml", 500.0),
            _scraper_row("Cleaner 2 L", 20.0, "L", 2.0),
            _scraper_row("Cleaner 32 fl oz", 30.0, "fl oz", 32.0),
            _scraper_row("Powder 500 g", 5.0, "g", 500.0),
            _scraper_row("Wipes 80", 8.0, "units", 80.0),
        ],
        [ODOO_ROW],
    )
    assert sorted(volume["unit_of_measurement"]) == ["fl oz", "l", "ml", "ml"]
    by_unit = dict(zip(volume["unit_of_measurement"] + "|" + volume["company"], volume["price_per_liter"]))
    assert by_unit["ml|Brand"] == pytest.approx(20.0)
    assert by_unit["l|Brand"] == pytest.approx(10.0)
    assert by_unit["fl oz|Brand"] == pytest.approx(30.0 / (32 * 0.0295735))
    assert by_unit["ml|Callington"] == pytest.approx(1000.50 / 0.5)  # strips "SAR" and the comma
    assert units["price_per_item"].tolist() == [pytest.approx(0.1)]


@pytest.mark.xfail(
    strict=True,
    reason="BUG: parse_volume_string returns unit='kg' (and 'litre', because of the other bug) and 1a only "
    "recognizes ml/l/fl oz/g as volume; those rows fall into the per-piece model as if they were "
    "pieces. Counterexample: 'Powder 2 kg', 30 SAR -> competitor_unit_processed.csv with "
    "total_quantity=2 and price_per_item=15 (finding for pi-dev)",
)
@pytest.mark.parametrize("unit", ["kg", "litre"])
def test_main_does_not_treat_mass_or_volume_as_pieces(tmp_path, monkeypatch, unit):
    _, units = _run_main(tmp_path, monkeypatch, [_scraper_row("Powder 2 " + unit, 30.0, unit, 2.0)])
    assert units.empty


def test_process_unit_data_drops_invalid_quantities(tmp_path, monkeypatch):
    (tmp_path / "ml_model").mkdir()
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame({"total_quantity": ["10", "0", "-2", "abc", None], "price_sar": [5.0, 5.0, 5.0, 5.0, 5.0]})
    preprocessing.process_unit_data(df)
    output = pd.read_csv("ml_model/competitor_unit_processed.csv")
    assert output["total_quantity"].tolist() == [10.0]
    assert output["price_per_item"].tolist() == [0.5]


def test_process_volumetric_data_without_valid_rows_does_not_fail(tmp_path, monkeypatch):
    (tmp_path / "ml_model").mkdir()
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame({"unit_of_measurement": ["g"], "total_quantity": [100.0], "price_sar": [5.0]})
    preprocessing.process_volumetric_data(df)
    assert pd.read_csv("ml_model/competitor_volumetric_processed.csv").empty
