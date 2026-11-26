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


# Real titles of the 2026-11-25 run (AeroSense and GoGreen), copied as literals: (title, unit,
# total_quantity, is_multipack). The totals were already right; only the case flag was missing.
CASE_PACK_TITLES = [
    ("Deep Cleaner for Leather - 946 ml spray bottle x 12", "ml", 11352.0, True),
    ("Leather Cleaner & Conditioner - 946 ml spray bottle x 12", "ml", 11352.0, True),
    ("Leather Conditioner - 473 ml bottle x 12", "ml", 5676.0, True),
    ("Cabin Disinfectant Ready-to-use - 750 ml spray bottle x 6", "ml", 4500.0, True),
    ("Cabin Disinfectant Ready-to-use - 750 ml spray bottle x 18", "ml", 13500.0, True),
    ("Cabin Disinfectant Concentrate - 5 litre can x 4", "ml", 20000.0, True),
    ("Windshield Cleaner - 750 ml spray bottle x 6", "ml", 4500.0, True),
    ("Airframe Cleaner - 5 litre can x 4", "ml", 20000.0, True),
    ("Flushman Lavatory Additive - 5 litre can x 4", "ml", 20000.0, True),
    ("AeroScent Air Freshener - 150 ml spray bottle x 24 Fresh Lemon", "ml", 3600.0, True),
    ("AeroScent Air Freshener - 150 ml spray bottle x 144 Fresh Lemon", "ml", 21600.0, True),
    ("Cabin Cleaner Triple Action - 150 ml spray bottle x 24 Linen Breeze", "ml", 3600.0, True),
    ("Cabin Cleaner Triple Action - 150 ml spray bottle x 144 Fresh Lemon", "ml", 21600.0, True),
    ("Hangar Floor Cleaner - 5 litre can x 4", "ml", 20000.0, True),
    ("Carpet Cleaner - 5 litre can x 4", "ml", 20000.0, True),
    ("Insect Remover - 750 ml spray bottle x 6", "ml", 4500.0, True),
    ("Cabin Cleaning Wipes Triple Action - 4 cartons (2000 pcs) - Fresh Lemon", "units", 2000.0, True),
    ("Cabin Cleaning Wipes Triple Action - 20-pack x 40 - Fresh Lemon", "units", 800.0, True),
    ("كرتون مطهر و معقم الاسطح 4 حبة (5لتر)", "l", 20.0, True),
    ("كرتون مزيل البقع للملابس البيضاء ليوبارد 12 حبة (1لتر)", "l", 12.0, True),
    ("كرتون 12 حبة مزيل البقع 1 لتر", "l", 12.0, True),  # the count before the size
    ("مطهر و معقم الاسطح 4 كراتين (5 لتر)", "l", 20.0, True),  # the plural: 4 cartons
    ("مناديل مبللة ٤ كراتين", "units", 400.0, True),  # Arabic-Indic digits
    ("مطهر 2 كرتونات 1 لتر", "l", 2.0, True),
    # Single packages: never flagged.
    ("Deep Cleaner for Leather - 946 ml spray bottle", "ml", 946.0, False),
    ("Leather Conditioner - 473 ml bottle", "ml", 473.0, False),
    ("Cabin Disinfectant Ready-to-use - 20 litre can", "ml", 20000.0, False),
    ("Airframe Cleaner - 20 litre can", "ml", 20000.0, False),
    ("Cabin Cleaning Wipes Triple Action - 1 carton (500 pcs) - Fresh Lemon", "units", 500.0, False),
    ("Avionics Wipes - 1 carton (400 pcs)", "units", 400.0, False),
    ("منظف الارضيات 3 لتر - ليمون", "l", 3.0, False),
    ("كرتون مطهر و معقم الاسطح 1 حبة (5لتر)", "l", 5.0, False),  # a carton of one is one package
    ("مطهر و معقم الاسطح 1 كراتين (5 لتر)", "l", 5.0, False),  # a count of one
    ("منظف و مطهر الاسطح بدون عطر ولون 30 لتر", "l", 30.0, False),
    ("صابون ايدي مضاد للبكتيريا 350 مل - ورد", "ml", 350.0, False),
    ("شامبو غسيل السجاد من GO GREEN - تنظيف وتعقيم (2 لتر)", "l", 2.0, False),
    ("Microfiber Cloth 40 x 40 cm", "units", 1.0, False),
    ("Glass Cleaner 4 L (1 Gallon)", "l", 4.0, False),  # the same single size said twice
    ("Degreaser 24oz - 709ml spray bottle", "ml", 709.0, False),
]


@pytest.mark.parametrize("title,unit,quantity,expected", CASE_PACK_TITLES)
def test_flag_multipacks_reads_aerosense_and_gogreen_case_packs(title, unit, quantity, expected):
    """Audit 2026-11-25 (AS-3, GG-11): case packs written "<size> <container> x N", "N cartons" or
    as an Arabic carton with a count are multipacks; a single package (one carton included) is not."""
    assert preprocessing.flag_multipacks([title], [unit], [quantity]) == [expected]


@pytest.mark.parametrize("title,expected", [
    ("AeroScent Air Freshener - 150 ml spray bottle x 144 Fresh Lemon", 144),  # above volume_pack's 100
    ("Cabin Disinfectant Concentrate - 5 litre can x 4", 4),
    ("Cabin Cleaning Wipes Triple Action - 4 cartons (2000 pcs) - Fresh Lemon", 4),
    ("كرتون مطهر و معقم الاسطح ٤ حبات (٥ لتر)", 4),  # Arabic-Indic digits
    ("Avionics Wipes - 1 carton (400 pcs)", 1),
    ("مطهر 4 حبة (5لتر)", 1),  # no carton word: left to the adapters' readers
    ("كرتون 4 حبة و 2 عبوة 5 لتر", 1),  # two counts: not one case
    ("Cleaner 750 ml spray bottle x 2.5", 1),
    ("Mat 60 ml bottle x 40 cm", 1),
    (None, 1),
])
def test_case_pack_examples(title, expected):
    assert preprocessing.case_pack(title) == expected


def test_main_excludes_wayakit_company_rows(tmp_path, monkeypatch):
    """Audit 2026-11-25 (AS-4): an Odoo competitor.product row with company WAYAKIT (8,400 mL at
    245.88 SAR in Aviation A2) is our own price: it is never in training or in the market band.
    Scraper rows with a Wayakit company are dropped too; another company is kept."""
    wayakit_odoo = {**ODOO_ROW, "Total quantity": 8400.0, "Price per unit SAR": 245.88, "Company": "WAYAKIT"}
    scraper_rows = [
        {**_scraper_row("Cleaner 500 ml", 10.0, "ml", 500.0), "company": " Wayakit "},
        {**_scraper_row("Cleaner 1 L", 11.0, "L", 1.0), "company": "WAYAKIT GreenTech"},
        {**_scraper_row("Wipes 80", 8.0, "units", 80.0), "company": "wayakit"},
        _scraper_row("Cleaner 750 ml", 12.0, "ml", 750.0),
    ]
    volume, units = _run_main(tmp_path, monkeypatch, scraper_rows, [wayakit_odoo, ODOO_ROW])
    assert sorted(volume["company"]) == ["Brand", "Callington"]
    assert sorted(volume["price_sar"]) == [12.0, 1000.50]
    assert units.empty
