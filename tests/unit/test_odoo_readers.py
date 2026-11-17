"""Unit tests of the Odoo readers and the scraping-list builder after F2 (fail loudly, no import side effects).

- Importing the scripts reads no secret and opens no connection (they used to authenticate at
  import time).
- `main()` with a fake session: 0 records → return 1 and the previous CSV stays untouched
  (before F2 they logged a warning and exited 0, and the pipeline priced a stale catalog).
- `build_analysis_list`: missing columns raise `AnalysisListError`; types without modifiers get ''.
The byte-for-byte equivalence with the base version is in
`tests/property/test_properties_odoo_readers_equivalence.py`.
"""
from __future__ import annotations

import subprocess
import sys

import pandas as pd
import pytest

from pi_odoo import OdooSession
from tests.conftest import REPO_ROOT, load_module

pytestmark = pytest.mark.unit

READERS = {
    "ml_model/odoo_api_products.py": "product.master",
    "ml_model/odoo_api_competitor_products.py": "competitor.product",
    "ml_model/odoo_api_cotizations.py": "sale.order.line",
}
SCRIPTS = [*READERS, "scraper/odoo_api_connection_products.py", "ml_model/odoo_api_price_suggestion.py"]


class FakeModels:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def execute_kw(self, db, uid, token, model, method, args, kwargs=None):
        self.calls.append((model, method))
        return list(self.records)


@pytest.mark.parametrize("rel", SCRIPTS)
def test_importing_the_script_reads_no_secret_and_opens_no_connection(rel):
    """Imported in a clean interpreter with boto3 and ServerProxy rigged to explode."""
    # The real log_config would create <repo>/../logs outside the repo: replace it with the same
    # file-less stand-in that tests/conftest.py installs (logger/get_logger/setup_logging only).
    code = (
        "import sys, types, logging, runpy, boto3, xmlrpc.client\n"
        "lc = types.ModuleType('log_config'); lc.logger = logging.getLogger('pi.test')\n"
        "lc.get_logger = lambda: lc.logger; lc.setup_logging = lambda *a, **k: lc.logger\n"
        "sys.modules['log_config'] = lc\n"
        "def boom(*a, **k): raise SystemExit('touched AWS or Odoo at import')\n"
        "boto3.session.Session = boom; boto3.client = boom; xmlrpc.client.ServerProxy = boom\n"
        f"runpy.run_path({str(REPO_ROOT / rel)!r}, run_name='not_main')\n"
        "print('imported')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
                       env={"PATH": "/usr/bin:/bin", "AWS_CONFIG_FILE": "/dev/null",
                            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null", "AWS_EC2_METADATA_DISABLED": "true"})
    assert r.returncode == 0 and r.stdout.strip().endswith("imported"), r.stdout + r.stderr


@pytest.mark.parametrize("rel, model", READERS.items())
def test_zero_records_returns_1_and_keeps_the_previous_csv(tmp_path, rel, model):
    out = tmp_path / "previous.csv"
    out.write_text("col\nprevious run\n", encoding="utf-8")
    models = FakeModels([])
    session = OdooSession(models=models, db="FAKE-db", uid=1, api_token="FAKE-token")
    assert load_module(rel).main(["--output", str(out)], session=session) == 1
    assert out.read_text(encoding="utf-8") == "col\nprevious run\n"
    assert models.calls == [(model, "search_read")]


@pytest.mark.parametrize("rel", READERS)
def test_query_errors_propagate_out_of_main(tmp_path, rel):
    """`__main__` turns them into exit 1; main itself must not swallow them."""
    class Broken:
        def execute_kw(self, *args, **kwargs):
            raise ConnectionError("fake Odoo down")

    session = OdooSession(models=Broken(), db="FAKE-db", uid=1, api_token="FAKE-token")
    with pytest.raises(ConnectionError):
        load_module(rel).main(["--output", str(tmp_path / "x.csv")], session=session)
    assert not (tmp_path / "x.csv").exists()


def test_products_many2one_fields_keep_only_the_name():
    m = load_module("ml_model/odoo_api_products.py")
    rec = {f: 1.0 for f in m.FIELDS_TO_GET}
    rec.update(product_id="FP-1", type_of_product_id=[3, "H1-Floor cleaner"], subindustry_id=[2, "Home"],
               industry_id=False, create_date="2026-11-01 10:00:00")
    df = m.records_to_dataframe([rec])
    assert list(df.columns) == m.FINAL_COLUMNS_ORDER
    assert (df.loc[0, "Type_of_product"], df.loc[0, "SubIndustry"]) == ("H1-Floor cleaner", "Home")
    assert df.loc[0, "Industry"] is None


def test_competitor_products_map_channel_uom_and_date():
    m = load_module("ml_model/odoo_api_competitor_products.py")
    df = m.records_to_dataframe([
        {"date": "2026-11-14", "product_channel": "retail", "classification_id": [1, "H1-Floor cleaner"],
         "product_category": "Home", "subindustry_id": [2, "Home"], "industry_id": [3, "Home care"],
         "generic_product_type": "Cleaner", "total_quantity": 500.0, "uom": "ml", "price_unit_sar": 9.5,
         "company": "Fake brand"},
        {"date": "2026-01-02", "product_channel": "weird", "classification_id": False,
         "product_category": False, "subindustry_id": False, "industry_id": False,
         "generic_product_type": False, "total_quantity": 3.0, "uom": "kg", "price_unit_sar": 1.0,
         "company": False},
    ])
    assert df["Channel"].tolist() == ["B2C", "Unknown"]
    assert df["Unit of measurement [mL,g,units]"].tolist() == ["mL", "Unknown"]
    assert df["Date"].tolist() == ["14/11/2026", "02/01/2026"]
    assert df.loc[1, "Type of product"] is None


# --- scraper/odoo_api_connection_products.py ------------------------------------------------------
conn = load_module("scraper/odoo_api_connection_products.py")
PRODUCTS = pd.DataFrame({
    "Product_ID": ["FP-1", "FP-2", "FP-3"],
    "Type_of_product": ["H1-Floor cleaner", "H1-Floor cleaner", "P1-Pet wipes"],
    "SubIndustry": ["Home", "Home", "Pets"],
    "Industry": ["Home care", "Home care", "Pets"],
    "Generic product type": ["Cleaner", "Cleaner", "Wipes"],
})
MODIFIERS = pd.DataFrame({"Type of product": ["H1-Floor cleaner"], "Search Modifiers": ["amazon: floor cleaner"]})


def test_analysis_list_is_unique_and_types_without_modifiers_get_empty():
    df = conn.build_analysis_list(PRODUCTS, MODIFIERS)
    assert list(df.columns) == ["Type of product", "Sub industry", "Industry", "Generic product type",
                                "Search Modifiers"]
    assert df["Type of product"].tolist() == ["H1-Floor cleaner", "P1-Pet wipes"]
    assert df["Search Modifiers"].tolist() == ["amazon: floor cleaner", ""]


@pytest.mark.parametrize("drop_from, column", [("products", "SubIndustry"), ("products", "Type_of_product"),
                                               ("modifiers", "Search Modifiers"), ("modifiers", "Type of product")])
def test_analysis_list_with_a_missing_column_raises(drop_from, column):
    products = PRODUCTS.drop(columns=[column]) if drop_from == "products" else PRODUCTS
    modifiers = MODIFIERS.drop(columns=[column]) if drop_from == "modifiers" else MODIFIERS
    with pytest.raises(conn.AnalysisListError):
        conn.build_analysis_list(products, modifiers)
    assert issubclass(conn.AnalysisListError, ValueError)
