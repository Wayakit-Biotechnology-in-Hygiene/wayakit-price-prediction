"""Examples for ``ml_model/pets_diagnosis.py`` (F5) with synthetic CSVs in ``tmp_path``.

The processed competitor files are built by running ``1a_preprocess_data.main()`` on the
synthetic raw files, so the diagnosis sees the same layout as a real data folder. No snapshot
files, no network, nothing written outside ``tmp_path``.
"""
import pandas as pd
import pytest

from tests.conftest import load_module

diagnosis = load_module("ml_model/pets_diagnosis.py")
preprocessing = load_module("ml_model/1a_preprocess_data.py")
training = load_module("ml_model/2_train_models.py")

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
PRODUCT_COLUMNS = ["Product_ID", "Product_Name", "Volume_Liters", "Pack_quantity_Units",
                   "Type_of_product", "SubIndustry", "Industry", "Generic product type", "Unit_cost_SAR"]


def _odoo(product_type, quantity, unit, price, company="Brand", subindustry="Pets"):
    return {
        "Date": "16/02/2025", "Channel": "B2C", "Type of product": product_type, "Product category": "x",
        "Sub industry": subindustry, "Industry": "Households", "Generic product type": "Odor eliminator",
        "Total quantity": quantity, "Unit of measurement [mL,g,units]": unit,
        "Price per unit SAR": price, "Company": company,
    }


def _scraped(product_type, title, price, unit, quantity, company="Brand"):
    return {
        "date": "2026-11-14", "industry": "Households", "subindustry": "Pets", "type_of_product": product_type,
        "generic_product_type": "Wipes", "product": title, "price_sar": price, "company": company,
        "source": "Amazon", "url": "https://example.invalid/p", "unit_of_measurement": unit,
        "total_quantity": quantity, "channel": "B2C",
    }


def _odoo_rows():
    rows = [_odoo("P1-Two prices", 1000.0, "mL", p, company=c) for p, c in ((10.0, "A"), (20.0, "B"))]
    rows += [_odoo("P2-Spray", 500.0, "mL", float(p), company=f"Brand{p}") for p in range(10, 100, 10)]
    rows.append(_odoo("P2-Spray", 473.0, "mL", 100.0, company="US brand"))  # 16 fl oz in mL
    rows.append(_odoo("P2-Spray", 1.0, "mL", 50.0, company="Tiny"))  # 1 mL -> 50,000 SAR/L
    rows.append(_odoo("P2-Spray", 500.0, "mL", 0.0, company="Free", subindustry="pets"))  # case-insensitive
    rows.append(_odoo("P3-Wipes", 50.0, "Units", 15.0, company="Odoo wipes"))
    rows.append(_odoo("A1-Cabin cleaner", 500.0, "mL", 16.0, subindustry="Aviation"))
    return rows


def _scraper_rows():
    return [
        _scraped("P2-Spray", "Odor spray 16 fl oz", 60.0, "fl oz", 16.0, company="Imported"),
        _scraped("P3-Wipes", "Pet wipes, Pack of 3 (80 wipes each)", 30.0, "units", 240.0),
        _scraped("P3-Wipes", "Pet wipes 100 count", 20.0, "units", 100.0, company="Other"),
    ]


def _write(df, path):
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _build_data_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "ml_model").mkdir(parents=True)
    (data / "scraper").mkdir()
    _write(pd.DataFrame(_scraper_rows(), columns=SCRAPER_COLUMNS), data / "scraper/competitors_complete.csv")
    _write(pd.DataFrame(_odoo_rows(), columns=ODOO_COLUMNS), data / "ml_model/competitor_products_from_odoo.csv")
    monkeypatch.chdir(data)
    preprocessing.main()  # writes the two processed files inside the synthetic data folder

    products = pd.DataFrame([
        ["FP-PET-1", "Spray [500 mL]", 0.5, 0, "P2-Spray", "Pets", "Households", "Odor eliminator", 10.0],
        ["FP-PET-2", "Spray [4 L]", 4.0, 0, "P2-Spray", "Pets", "Households", "Odor eliminator", 10.0],
        ["FP-PET-3", "Spray concentrate", 0.0, 0, "P2-Spray", " Pets ", "Households", "Odor eliminator", 3.0],
        ["FP-AVI-1", "Cabin cleaner", 0.7, 0, "A1-Cabin cleaner", "Aviation", "Transportation", "Cleaner", 5.0],
    ], columns=PRODUCT_COLUMNS)
    _write(products, data / "ml_model/wayakit_products_to_predict_odoo.csv")
    report = pd.DataFrame({
        "Product_ID": ["FP-PET-1", "FP-PET-2", "FP-PET-3", "FP-AVI-1"],
        "cost_per_unit": [10.0, 10.0, 3.0, 5.0],
        "predicted_price": [13.0, 400.0, 3.9, 30.0],
        "predicted_price_per_unit": [20.0, 100.0, 0.0, 42.86],
        "market_min_found": [0.0] * 4, "market_max_found": [0.0] * 4,
        "competitors_count": [0, 0, 0, 0],
        "clamped_to_market": [False] * 4, "volume_outside_market": [False, True, False, False],
    })
    _write(report, data / "ml_model/wayakit_prediction_report.csv")
    quotes = pd.DataFrame({"Product_ID ": ["FP-PET-2", "FP-PET-2 ", "FP-PET-2", "FP-AVI-1"],
                           "Description": ["x"] * 4, "approved_quote_price": [100.0, 300.0, 200.0, 25.0]})
    _write(quotes, data / "ml_model/wayakit_cotizations.csv")
    return data


def _run(data, tmp_path, *extra):
    out = tmp_path / "out"
    code = diagnosis.main(["--data-dir", str(data), "--out-dir", str(out), *extra])
    return code, out


def test_unit_helpers():
    assert diagnosis.model_of_unit(" mL ") == "volumetric" and diagnosis.model_of_unit("units") == "units"
    assert diagnosis.size_in_ml("fl oz", 16) == pytest.approx(473.176)
    assert diagnosis.size_in_ml("L", 2) == 2000 and pd.isna(diagnosis.size_in_ml("g", 100))
    assert diagnosis.size_in_ml("Litre", 2) == 2000 and diagnosis.size_in_ml("liters", 0.5) == 500  # as 1a (F7a)
    assert diagnosis.is_us_size_ml(473.0) and diagnosis.is_us_size_ml(946.0)
    assert not diagnosis.is_us_size_ml(500.0) and not diagnosis.is_us_size_ml(60.0)
    # Large US bottles with rounded labels (relative tolerance): half gallon and gallon.
    assert diagnosis.is_us_size_ml(1890.0) and diagnosis.is_us_size_ml(3780.0) and diagnosis.is_us_size_ml(3788.0)
    # Metric sizes close to US sizes stay out (650 mL is not 22 fl oz; 1.9 L, 3.8 L are metric).
    assert not diagnosis.is_us_size_ml(650.0) and not diagnosis.is_us_size_ml(1900.0)
    assert not diagnosis.is_us_size_ml(3800.0) and not diagnosis.is_us_size_ml(300.0)
    assert diagnosis.has_pack_marker("Spray, Pack of 2") and diagnosis.has_pack_marker("2 x 500 ml")
    assert diagnosis.has_pack_marker("Wipes 3-pack") and not diagnosis.has_pack_marker("Pet wipes 100 count")
    assert not diagnosis.has_pack_marker(None)


def test_by_type_counts_reproduce_training_trimming(tmp_path, monkeypatch):
    data = _build_data_dir(tmp_path, monkeypatch)
    code, out = _run(data, tmp_path)
    assert code == 0
    by_type = pd.read_csv(out / "pets_by_type.csv").set_index(["model", "type_of_product"])

    # Bug 6 (fixed in F7a): two different prices used to be excluded by the p10-p90 bounds; the
    # diagnosis reuses the pipeline's trimming, so it now reports the type as kept whole.
    two = by_type.loc[("volumetric", "P1-Two prices")]
    assert (two["n_before_trim"], two["n_after_trim"], bool(two["emptied_by_trim"])) == (2, 2, False)

    spray = by_type.loc[("volumetric", "P2-Spray")]
    volume = pd.read_csv(data / "ml_model/competitor_volumetric_processed.csv")
    # As 2_train_models main(): 5 mL or less is dropped before the trimming (F7a).
    expected_after = training.filter_outliers_with_percentiles(training.drop_implausible_sizes(volume), "price_per_liter")
    # 1a drops the price-0 row since F7a (C18), so 12 of the 13 raw rows reach training.
    assert spray["n_raw"] == 13 and spray["n_before_trim"] == 12
    assert spray["n_after_trim"] == (expected_after["type_of_product"] == "P2-Spray").sum()
    assert spray["n_after_trim"] < spray["n_before_trim"] and not spray["emptied_by_trim"]
    assert spray["max_per_unit"] == pytest.approx(50000.0)
    assert spray["max_after_trim"] < spray["max_per_unit"]
    assert spray["pct_fl_oz"] == pytest.approx(round(100 / 13, 1))
    assert spray["pct_us_size_ml"] == pytest.approx(round(100 / 12, 1))
    assert (spray["n_price_zero"], spray["n_le_5ml"], spray["n_le_5ml_after_trim"]) == (1, 1, 0)
    assert spray["n_brands"] == 12 and spray["n_wayakit_skus"] == 2  # the price-0 brand is dropped by 1a

    wipes = by_type.loc[("units", "P3-Wipes")]
    assert (wipes["n_raw"], wipes["n_with_title"], wipes["n_multipack"]) == (3, 2, 1)
    assert wipes["pct_multipack"] == pytest.approx(50.0) and pd.isna(wipes["pct_fl_oz"])
    assert ("volumetric", "A1-Cabin cleaner") not in by_type.index  # other subindustries are left out


def test_wayakit_products_position_floor_and_quotes(tmp_path, monkeypatch):
    data = _build_data_dir(tmp_path, monkeypatch)
    code, out = _run(data, tmp_path)
    assert code == 0
    products = pd.read_csv(out / "pets_wayakit_products.csv").set_index("Product_ID")
    assert set(products.index) == {"FP-PET-1", "FP-PET-2", "FP-PET-3"}

    small = products.loc["FP-PET-1"]
    assert small["volume_position"] == "inside" and bool(small["floor_binding"])
    assert small["margin_floor_price"] == pytest.approx(13.0) and small["n_quotes"] == 0

    big = products.loc["FP-PET-2"]
    assert big["volume_position"] == "above_max" and not big["floor_binding"]
    assert big["median_quote"] == pytest.approx(200.0) and big["n_quotes"] == 3
    assert big["predicted_over_quote"] == pytest.approx(2.0) and big["predicted_over_cost"] == pytest.approx(40.0)
    assert big["predicted_price_per_unit"] == pytest.approx(100.0) and big["clamp_direction"] == "none"

    no_size = products.loc["FP-PET-3"]
    assert no_size["model"] == "no_size" and no_size["volume_position"] == "no_size" and bool(no_size["floor_binding"])

    report = (out / "pets_report.md").read_text(encoding="utf-8")
    assert "# Pets diagnosis (F5)" in report and "| FP-PET-2 |" in report


def test_writes_nothing_inside_the_data_folders(tmp_path, monkeypatch):
    data = _build_data_dir(tmp_path, monkeypatch)
    before = sorted(p.relative_to(data) for p in data.rglob("*"))
    code, _ = _run(data, tmp_path)
    assert code == 0
    assert sorted(p.relative_to(data) for p in data.rglob("*")) == before


def test_missing_input_exits_1(tmp_path, capsys):
    code = diagnosis.main(["--data-dir", str(tmp_path), "--out-dir", str(tmp_path / "out")])
    assert code == 1
    assert "Missing input files" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_subindustry_without_rows_exits_1(tmp_path, monkeypatch, capsys):
    data = _build_data_dir(tmp_path, monkeypatch)
    code, out = _run(data, tmp_path, "--subindustry", "Automotive")
    assert code == 1
    assert "has 0 competitor rows" in capsys.readouterr().err
    assert not out.exists()


def test_out_dir_inside_ml_model_is_refused(tmp_path, monkeypatch, capsys):
    data = _build_data_dir(tmp_path, monkeypatch)
    code = diagnosis.main(["--data-dir", str(data), "--out-dir", str(data / "ml_model" / "f5")])
    assert code == 1
    assert "must not be inside" in capsys.readouterr().err
    assert not (data / "ml_model" / "f5").exists()
