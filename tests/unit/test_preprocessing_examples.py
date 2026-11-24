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


def _run_main(tmp_path, monkeypatch, scraper_rows, odoo_rows=(ODOO_ROW,), scraper_columns=SCRAPER_COLUMNS,
              odoo_columns=ODOO_COLUMNS):
    (tmp_path / "scraper").mkdir()
    (tmp_path / "ml_model").mkdir()
    pd.DataFrame(list(scraper_rows), columns=scraper_columns).to_csv(tmp_path / "scraper/competitors_complete.csv", index=False)
    pd.DataFrame(list(odoo_rows), columns=odoo_columns).to_csv(tmp_path / "ml_model/competitor_products_from_odoo.csv", index=False)
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


@pytest.mark.parametrize("unit", ["kg", "KG", "g", "kilograms"])
def test_main_drops_mass_units_and_never_treats_them_as_pieces(tmp_path, monkeypatch, unit):
    """Bug 5 (fixed in F7a): 'Powder 2 kg' used to reach competitor_unit_processed.csv as 2 pieces."""
    volume, units = _run_main(tmp_path, monkeypatch, [_scraper_row("Powder 2 " + unit, 30.0, unit, 2.0)])
    assert units.empty
    assert volume["company"].tolist() == ["Callington"]  # only the Odoo mL row


@pytest.mark.parametrize("unit", ["litre", "Litres", "liter", "liters"])
def test_main_reads_litre_spellings_as_liters(tmp_path, monkeypatch, unit):
    """Bug 5 (fixed in F7a): 'litre' fell into the per-piece model; now it is 1 L, written as 'l'."""
    volume, units = _run_main(tmp_path, monkeypatch, [_scraper_row("Cleaner 2 " + unit, 30.0, unit, 2.0)])
    assert units.empty
    row = volume[volume["company"] == "Brand"].iloc[0]
    assert row["unit_of_measurement"] == "l"
    assert row["volume_liters"] == pytest.approx(2.0)
    assert row["price_per_liter"] == pytest.approx(15.0)


def test_main_drops_zero_and_negative_prices_c18(tmp_path, monkeypatch):
    volume, units = _run_main(
        tmp_path,
        monkeypatch,
        [
            _scraper_row("Free 500 ml", 0.0, "ml", 500.0),
            _scraper_row("Refund 500 ml", -5.0, "ml", 500.0),
            _scraper_row("Free wipes 80", 0.0, "units", 80.0),
            _scraper_row("Cleaner 500 ml", 10.0, "ml", 500.0),
        ],
    )
    assert sorted(volume["price_sar"].tolist()) == [10.0, 1000.50]
    assert units.empty


def test_main_flags_multipacks_from_the_title_only(tmp_path, monkeypatch):
    """F7a: a scraper row whose title states a pack of the same product is flagged (it stays in
    training, but not in the market range); Odoo rows have no title and are never flagged."""
    volume, units = _run_main(
        tmp_path,
        monkeypatch,
        [
            _scraper_row("Cleaner 500 ml x 2", 20.0, "ml", 1000.0),
            _scraper_row("Cleaner Twin Pack 1L", 30.0, "L", 2.0),
            _scraper_row("Cleaner 750 ml", 10.0, "ml", 750.0),
            _scraper_row("Wipes Pack of 6, 480 count", 60.0, "units", 480.0),
            _scraper_row("Wipes 80", 8.0, "units", 80.0),
            _scraper_row("Wet Wipes Pack of 72", 12.0, "units", 72.0),  # one pack of 72 pieces
        ],
    )
    flags = dict(zip(volume["price_sar"], volume["is_multipack"]))
    assert flags == {20.0: True, 30.0: True, 10.0: False, 1000.50: False}
    assert dict(zip(units["price_sar"], units["is_multipack"])) == {60.0: True, 8.0: False, 12.0: False}


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


def test_f6_seller_and_origin_columns_are_read_c08(tmp_path, monkeypatch):
    """C08 (F6) and F7b: a pre-F6 13-column file and a 15-column file (+ seller, origin) with the
    same rows give the same processed CSVs except `origin`: old rows stay readable (origin
    'unknown'), the seller and the source are not used, and the origin is carried (F7b)."""
    rows = [
        _scraper_row("Cleaner 500 ml", 10.0, "ml", 500.0),
        _scraper_row("Cleaner 2 L", 20.0, "L", 2.0),
        _scraper_row("Cleaner 32 fl oz", 30.0, "fl oz", 32.0),
        _scraper_row("Wipes 80", 8.0, "units", 80.0),
    ]
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    old_volume, old_units = _run_main(old_dir, monkeypatch, rows)
    new_rows = [{**r, "source": "noon", "seller": "Shop", "origin": o}
                for r, o in zip(rows, [" Local", "imported", None, "local"])]
    new_volume, new_units = _run_main(new_dir, monkeypatch, new_rows,
                                      scraper_columns=SCRAPER_COLUMNS + ["seller", "origin"])
    assert "seller" not in new_volume.columns
    assert old_volume["origin"].tolist() == ["unknown"] * 4
    assert new_volume["origin"].tolist() == ["local", "imported", "unknown", "unknown"]  # last: Odoo row
    assert new_units["origin"].tolist() == ["local"]
    pd.testing.assert_frame_equal(old_volume.drop(columns="origin"), new_volume.drop(columns="origin"))
    pd.testing.assert_frame_equal(old_units.drop(columns="origin"), new_units.drop(columns="origin"))
    assert len(new_volume) == 4 and len(new_units) == 1  # 3 scraped + the Odoo row; 1 per-piece row


def test_odoo_source_channel_is_carried_and_older_files_get_empty(tmp_path, monkeypatch):
    """F7b: the raw Odoo channel (retail / ecommerce / b2b) reaches the processed files; scraper
    rows and an Odoo CSV from before F7b (no `Source channel` column) get ''."""
    (tmp_path / "a").mkdir()
    retail = {**ODOO_ROW, "Source channel": " Retail "}
    volume, _ = _run_main(tmp_path / "a", monkeypatch, [_scraper_row("Cleaner 500 ml", 10.0, "ml", 500.0)],
                          [retail], odoo_columns=ODOO_COLUMNS + ["Source channel"])
    assert volume["source_channel"].fillna("").tolist() == ["", "retail"]
    (tmp_path / "b").mkdir()
    volume, _ = _run_main(tmp_path / "b", monkeypatch, [_scraper_row("Cleaner 500 ml", 10.0, "ml", 500.0)])
    assert volume["source_channel"].fillna("").tolist() == ["", ""]
