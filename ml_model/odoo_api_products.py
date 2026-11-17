"""Reads the active Wayakit products (`product.master`) from Odoo into ml_model/wayakit_products.csv.

Importing this module does not read secrets or connect to Odoo: that happens in `main()`.
`fetch_products()` takes an `OdooSession` (pi_odoo) so tests can pass a fake `models`.
Any failure (secrets, authentication, query, 0 products, write) exits with code 1.
"""
import argparse
import os
import sys

import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_config import get_logger
from pi_odoo import OdooSession, connect
from pi_secrets import get_odoo_credentials

logger = get_logger()

OUTPUT_CSV_FILE = 'ml_model/wayakit_products.csv'
MODEL_NAME = 'product.master'

PRODUCTS_TO_EXCLUDE = [
    'F12-Other consumables for FM',
    'P11-Other consumables for pets',
    'H13-Other products, consumables or kits for homes',
    'C5-Other transportation products or items'
]

DOMAIN = [
    ['status', '=', 'active'],
    ['subindustry_id.name', 'not in', ['Maritime', 'Defense']],
    ['type_of_product_id.name', 'not in', PRODUCTS_TO_EXCLUDE]
]

FIELDS_TO_GET = [
    'product_id',
    'product_name',
    'label_product_name',
    'presentation',
    'volume_liters',
    'pack_quantity_units',
    'type_of_product_id',
    'category',
    'generic_product_type',
    'subindustry_id',
    'industry_id',
    'bottle_cost',
    'label_cost',
    'liquid_cost',
    'microfibers_cost',
    'plastic_bag_cost',
    'labor_cost',
    'shipping_cost',
    'other_costs',
    'unit_cost_sar',
    'create_date',
]

# Odoo field name -> CSV column name
COLUMN_MAPPING = {
    'create_date': 'create_date',
    'product_id': 'Product_ID',
    'product_name': 'Product_Name',
    'label_product_name': 'Label_Product_Name',
    'presentation': 'Presentation',
    'volume_liters': 'Volume_Liters',
    'pack_quantity_units': 'Pack_quantity_Units',
    'type_of_product_id': 'Type_of_product',
    'category': 'Category',
    'generic_product_type': 'Generic product type',
    'subindustry_id': 'SubIndustry',
    'industry_id': 'Industry',
    'bottle_cost': 'Bottle',
    'label_cost': 'Label',
    'liquid_cost': 'Liquid',
    'microfibers_cost': 'Microfibers',
    'plastic_bag_cost': 'Plastic bag',
    'labor_cost': 'Labor',
    'shipping_cost': 'Shipping',
    'other_costs': 'Other costs',
    'unit_cost_sar': 'Unit_cost_SAR'
}

FINAL_COLUMNS_ORDER = [
    'create_date', 'Product_ID', 'Product_Name', 'Label_Product_Name', 'Presentation',
    'Volume_Liters', 'Pack_quantity_Units', 'Type_of_product', 'Category',
    'Generic product type', 'SubIndustry', 'Industry', 'Bottle', 'Label',
    'Liquid', 'Microfibers', 'Plastic bag', 'Labor', 'Shipping',
    'Other costs', 'Unit_cost_SAR'
]


def fetch_products(session: OdooSession) -> list:
    """Reads the active products from `product.master` (one `search_read` call)."""
    logger.info(f"Searching records in model '{MODEL_NAME}'...")
    records = session.execute(MODEL_NAME, 'search_read', DOMAIN, fields=FIELDS_TO_GET)
    logger.info(f"Found {len(records)} records.")
    return records


def records_to_dataframe(records: list) -> pd.DataFrame:
    """Turns `product.master` records into the CSV layout. Pure: no I/O.

    Many2one fields come from Odoo as `[id, 'name']`; only the name is kept.
    """
    df = pd.DataFrame(records)
    df['type_of_product_id'] = df['type_of_product_id'].apply(lambda x: x[1] if isinstance(x, list) and len(x) > 1 else None)
    df['subindustry_id'] = df['subindustry_id'].apply(lambda x: x[1] if isinstance(x, list) else None)
    df['industry_id'] = df['industry_id'].apply(lambda x: x[1] if isinstance(x, list) else None)
    return df.rename(columns=COLUMN_MAPPING)[FINAL_COLUMNS_ORDER]


def main(argv=None, session: OdooSession = None) -> int:
    parser = argparse.ArgumentParser(description="Reads the active Wayakit products from Odoo.")
    parser.add_argument('--output', default=OUTPUT_CSV_FILE, help=f"Output CSV (default: {OUTPUT_CSV_FILE})")
    args = parser.parse_args(argv)

    if session is None:
        session = connect(get_odoo_credentials())

    records = fetch_products(session)
    if not records:
        # Writing nothing would leave the previous run's CSV in place and the pipeline would
        # price a stale catalog without noticing.
        logger.error(f"No records found in '{MODEL_NAME}' with the filter criteria. Aborting.")
        return 1

    df_final = records_to_dataframe(records)
    df_final.to_csv(args.output, index=False, encoding='utf-8-sig')
    logger.info(f"File '{args.output}' created with {len(df_final)} products.")
    logger.info(f"Preview of the first 5 rows:\n{df_final.head()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("CRITICAL ERROR while reading products from Odoo.")
        sys.exit(1)
