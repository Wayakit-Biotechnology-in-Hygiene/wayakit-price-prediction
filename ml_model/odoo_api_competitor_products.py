"""Reads the competitor products registered in Odoo (`competitor.product`) into
ml_model/competitor_products_from_odoo.csv.

Importing this module does not read secrets or connect to Odoo: that happens in `main()`.
`fetch_competitor_products()` takes an `OdooSession` (pi_odoo) so tests can pass a fake `models`.
Any failure (secrets, authentication, query, 0 records, write) exits with code 1.
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

MODEL_NAME = 'competitor.product'
OUTPUT_CSV_FILE = 'ml_model/competitor_products_from_odoo.csv'

FIELDS_TO_GET = [
    'date',
    'product_channel',       # -> 'Channel'
    'classification_id',     # Many2one -> name, used as 'Type of product'
    'product_category',
    'subindustry_id',        # Many2one -> name
    'industry_id',           # Many2one -> name
    'generic_product_type',
    'total_quantity',
    'uom',                   # -> 'Unit of measurement [mL,g,units]'
    'price_unit_sar',
    'company',
]

CHANNEL_MAP = {
    'b2b': 'B2B',
    'retail': 'B2C',
    'ecommerce': 'B2C'
}

UOM_MAP = {
    'ml': 'mL',
    'g': 'g',
    'units': 'Units'
}

COLUMN_MAPPING = {
    'date': 'Date',
    'product_channel': 'Channel',
    'classification_id': 'Type of product',
    'product_category': 'Product category',
    'subindustry_id': 'Sub industry',
    'industry_id': 'Industry',
    'generic_product_type': 'Generic product type',
    'total_quantity': 'Total quantity',
    'uom': 'Unit of measurement [mL,g,units]',
    'price_unit_sar': 'Price per unit SAR',
    'company': 'Company',
}

FINAL_COLUMNS_ORDER = [
    'Date', 'Channel', 'Type of product', 'Product category', 'Sub industry',
    'Industry', 'Generic product type', 'Total quantity',
    'Unit of measurement [mL,g,units]', 'Price per unit SAR', 'Company'
]


def fetch_competitor_products(session: OdooSession) -> list:
    """Reads every `competitor.product` record (empty domain, one `search_read` call)."""
    logger.info(f"Searching records in Odoo model '{MODEL_NAME}'...")
    records = session.execute(MODEL_NAME, 'search_read', [], fields=FIELDS_TO_GET)
    logger.info(f"{len(records)} competitor products found.")
    return records


def records_to_dataframe(records: list) -> pd.DataFrame:
    """Turns `competitor.product` records into the CSV layout. Pure: no I/O.

    Many2one fields (`[id, 'name']`) keep only the name; channel and unit codes are mapped
    to their labels ('Unknown' when not recognized); the date becomes DD/MM/YYYY.
    """
    df = pd.DataFrame(records)

    for col in ('classification_id', 'subindustry_id', 'industry_id'):
        if col in df.columns:
            df[col] = df[col].apply(lambda x: x[1] if isinstance(x, list) and len(x) == 2 else None)
    if 'product_channel' in df.columns:
        df['product_channel'] = df['product_channel'].map(CHANNEL_MAP).fillna('Unknown')
    if 'uom' in df.columns:
        df['uom'] = df['uom'].map(UOM_MAP).fillna('Unknown')

    df_renamed = df.rename(columns=COLUMN_MAPPING)
    for col in FINAL_COLUMNS_ORDER:
        if col not in df_renamed.columns:
            logger.warning(f"Column '{col}' not found in fetched data. Adding it as empty.")
            df_renamed[col] = None

    df_final = df_renamed[FINAL_COLUMNS_ORDER].copy()
    df_final['Date'] = pd.to_datetime(df_final['Date']).dt.strftime('%d/%m/%Y')
    return df_final


def main(argv=None, session: OdooSession = None) -> int:
    parser = argparse.ArgumentParser(description="Reads the competitor products registered in Odoo.")
    parser.add_argument('--output', default=OUTPUT_CSV_FILE, help=f"Output CSV (default: {OUTPUT_CSV_FILE})")
    args = parser.parse_args(argv)

    if session is None:
        session = connect(get_odoo_credentials())

    records = fetch_competitor_products(session)
    if not records:
        # Without records the previous CSV would stay in place and 1a would use stale data.
        logger.error(f"No records found in '{MODEL_NAME}'. Aborting.")
        return 1

    df_final = records_to_dataframe(records)
    df_final.to_csv(args.output, index=False, encoding='utf-8-sig')
    logger.info(f"File '{args.output}' created with {len(df_final)} competitor products.")
    logger.info(f"Preview of the first 5 rows:\n{df_final.head()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception(f"CRITICAL ERROR while reading '{MODEL_NAME}' from Odoo.")
        sys.exit(1)
