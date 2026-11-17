"""Builds the scraping list (scraper/analysis-odoo.csv) from the Odoo products CSV.

One row per unique (type of product, subindustry, industry, generic product type), with the
search modifiers of `scraper/modifiers_mapping.csv` for its type of product.

Despite its name, this script does not talk to Odoo: it reads the CSV that
`ml_model/odoo_api_products.py` wrote. Until F2 it read the Odoo secrets and authenticated
without using the connection; that was removed (one copy less of `get_secret()`).

Any failure (input or modifiers file missing or malformed, write error) exits with code 1:
a scraping list without modifiers would silently change every search keyword.
"""
import argparse
import os
import sys

import pandas as pd

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
from log_config import get_logger

logger = get_logger()

PRODUCTS_COLUMN_MAPPING = {
    'Type_of_product': 'Type of product',
    'SubIndustry': 'Sub industry',
    'Industry': 'Industry',
    'Generic product type': 'Generic product type'
}
UNIQUENESS_COLUMNS = ['Type of product', 'Sub industry', 'Industry', 'Generic product type']
MODIFIER_COLUMNS = ['Type of product', 'Search Modifiers']
FINAL_COLUMNS = UNIQUENESS_COLUMNS + ['Search Modifiers']


class AnalysisListError(ValueError):
    """An input does not have the columns the scraping list needs."""


def build_analysis_list(df_products: pd.DataFrame, df_modifiers: pd.DataFrame) -> pd.DataFrame:
    """Unique product combinations joined with their search modifiers. Pure: no I/O.

    Raises `AnalysisListError` if a required column is missing. Types without a modifier get ''.
    """
    df_renamed = df_products.rename(columns=PRODUCTS_COLUMN_MAPPING)
    missing = [c for c in UNIQUENESS_COLUMNS if c not in df_renamed.columns]
    if missing:
        raise AnalysisListError(f"Required columns are missing in the products file: {missing}")
    missing = [c for c in MODIFIER_COLUMNS if c not in df_modifiers.columns]
    if missing:
        raise AnalysisListError(f"Required columns are missing in the modifiers file: {missing}")

    df_final = df_renamed.drop_duplicates(subset=UNIQUENESS_COLUMNS).copy()
    df_final['Type of product'] = df_final['Type of product'].astype(str)
    df_modifiers = df_modifiers[MODIFIER_COLUMNS].copy()
    df_modifiers['Type of product'] = df_modifiers['Type of product'].astype(str)

    df_final = pd.merge(df_final, df_modifiers, on='Type of product', how='left')
    df_final['Search Modifiers'] = df_final['Search Modifiers'].fillna('')
    return df_final[FINAL_COLUMNS]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generates the list of products/subindustries for scraping.")
    parser.add_argument('--input_odoo_products_file', default='ml_model/wayakit_products.csv',
                        help="Input CSV file with Odoo products (e.g. wayakit_products.csv or wayakit_new_products_temp.csv).")
    parser.add_argument('--output_analysis_file', default='scraper/analysis-odoo.csv',
                        help="Output CSV file for the scraper (e.g. analysis-odoo.csv or analysis-odoo_partial.csv).")
    parser.add_argument('--modifiers_file', default='scraper/modifiers_mapping.csv',
                        help="CSV file with the search modifiers mapping.")
    args = parser.parse_args(argv)

    logger.info(f"Reading Odoo products file: '{args.input_odoo_products_file}'")
    df_products = pd.read_csv(args.input_odoo_products_file)
    logger.info(f"Read {len(df_products)} products.")
    df_modifiers = pd.read_csv(args.modifiers_file)

    df_to_export = build_analysis_list(df_products, df_modifiers)
    logger.info(f"Unique combinations found: {len(df_to_export)}")

    df_to_export.to_csv(args.output_analysis_file, index=False, encoding='utf-8-sig')
    logger.info(f"Analysis file generated: '{args.output_analysis_file}'")
    logger.info("Preview:\n" + df_to_export.head().to_string())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("CRITICAL ERROR while generating the scraping list.")
        sys.exit(1)
