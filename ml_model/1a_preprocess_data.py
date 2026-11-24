import pandas as pd
import numpy as np
import os
import sys
# Add the root directory to the path so log_config can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_config import get_logger
# Pure title parsing shared with the Apify adapters (no I/O); used here only to flag multipacks.
from scraper.scrapers.apify_common import pack_count, volume_pack

logger = get_logger()

# Unit (lower-cased, stripped) -> liters per unit. These rows go to the volumetric model.
LITERS_PER_UNIT = {
    'ml': 0.001,
    'l': 1.0, 'litre': 1.0, 'litres': 1.0, 'liter': 1.0, 'liters': 1.0,
    'fl oz': 0.0295735,
}
# Mass units: never a competitor size (Wayakit sells liquids only, and a weight cannot be priced
# per liter or per piece). The volumetric step drops them with a log line. Before F7a only `g` was
# dropped and `kg` fell into the per-piece model as if it were pieces (bug 5).
MASS_UNITS = {'g', 'gr', 'gram', 'grams', 'kg', 'kgs', 'kilogram', 'kilograms', 'mg'}


def normalize_unit(unit_column):
    return unit_column.astype('string').str.lower().str.strip()


def flag_multipacks(titles, units, quantities):
    """True for a row whose title states a pack of 2 or more of the same product.

    Only scraper rows have a title; Odoo `competitor.product` rows (no title) are never flagged.
    Volume rows use the adapters' volume-mode pack reader ("Twin Pack", "500ml x 2"...), the
    others the units-mode one; a per-piece row is a multipack only when the pack count is not its
    piece count ("Pack of 6, 480 wipes" is; "Wipes Pack of 72" with 72 pieces is one pack). The
    price stays the pack's total: the flag only keeps the row out of the market range (F7a), not
    out of training.
    """
    flags = []
    for title, unit, quantity in zip(titles, units, quantities):
        if title is None or pd.isna(title):
            flags.append(False)
            continue
        if not pd.isna(unit) and str(unit) in LITERS_PER_UNIT:
            flags.append(bool(volume_pack(title) > 1))
            continue
        pack = pack_count(title)
        pieces = pd.to_numeric(quantity, errors='coerce')
        flags.append(bool(pack > 1 and not (pd.notna(pieces) and pieces == pack)))
    return flags


def _text_column(df, column, default):
    """`df[column]` lower-cased and stripped, `default` where it is missing or empty."""
    if column not in df:
        return pd.Series([default] * len(df), index=df.index, dtype=object)
    values = df[column].astype('string').str.strip().str.lower()
    return values.where(values.notna() & (values != ''), default).astype(object)


def load_and_clean_raw_data():
    logger.info("--- 1. Loading and cleaning raw competitor data ---")
    df_test4 = pd.read_csv('scraper/competitors_complete.csv')
    df_prod = pd.read_csv('ml_model/competitor_products_from_odoo.csv')

    df_test4_clean = df_test4[['industry', 'subindustry', 'type_of_product', 'generic_product_type', 'price_sar', 'company', 'unit_of_measurement', 'total_quantity', 'channel']].copy()
    titles = df_test4['product'].tolist() if 'product' in df_test4 else [None] * len(df_test4)
    df_prod_clean = df_prod.rename(columns={
        'Industry': 'industry', 'Sub industry': 'subindustry', 'Type of product': 'type_of_product',
        'Generic product type': 'generic_product_type', 'Price per unit SAR': 'price_sar',
        'Company': 'company', 'Unit of measurement [mL,g,units]': 'unit_of_measurement',
        'Total quantity': 'total_quantity', 'Channel': 'channel'
    })
    df_prod_clean = df_prod_clean[df_test4_clean.columns]

    def clean_price(price_column):
        return pd.to_numeric(price_column.astype(str).str.replace('SAR', '').str.replace(',', '').str.strip(), errors='coerce')
    df_test4_clean['price_sar'] = clean_price(df_test4_clean['price_sar'])
    df_prod_clean['price_sar'] = clean_price(df_prod_clean['price_sar'])

    df_test4_clean['unit_of_measurement'] = normalize_unit(df_test4_clean['unit_of_measurement'])
    df_prod_clean['unit_of_measurement'] = normalize_unit(df_prod_clean['unit_of_measurement'])
    df_test4_clean['is_multipack'] = flag_multipacks(titles, df_test4_clean['unit_of_measurement'].tolist(),
                                                     df_test4_clean['total_quantity'].tolist())
    df_prod_clean['is_multipack'] = False
    # F7b: where each price comes from. `origin` (local / imported / unknown) only exists in the
    # scraper CSV since F6 (pre-F6 files and Odoo rows: unknown); `source_channel` (retail /
    # ecommerce / b2b) only in the Odoo CSV since F7b (scraper rows and older Odoo files: '').
    df_test4_clean['origin'] = _text_column(df_test4, 'origin', 'unknown').values
    df_test4_clean['source_channel'] = ''
    df_prod_clean['origin'] = 'unknown'
    df_prod_clean['source_channel'] = _text_column(df_prod, 'Source channel', '').values

    competitor_data = pd.concat([df_test4_clean, df_prod_clean], ignore_index=True)
    logger.info(f"✅ Raw data combined. Total: {len(competitor_data)} rows.")
    return competitor_data


def _drop_invalid_prices(df):
    """Price must be a number > 0 (C18): a price of 0 gives 0 SAR per liter or per piece and
    pulls the type's low bound down."""
    price = pd.to_numeric(df['price_sar'], errors='coerce')
    keep = (price > 0).fillna(False).astype(bool)
    dropped = int((~keep).sum())
    if dropped:
        logger.info(f"Excluded {dropped} rows with a missing, zero or negative price.")
    return df[keep]


def process_volumetric_data(df):
    logger.info("--- 2a. Processing volumetric products ---")
    df['unit_of_measurement'] = normalize_unit(df['unit_of_measurement'])

    is_mass = df['unit_of_measurement'].isin(MASS_UNITS).fillna(False).astype(bool)
    logger.info(f"Excluded {int(is_mass.sum())} products measured by mass (g, kg...).")
    df = df[df['unit_of_measurement'].isin(LITERS_PER_UNIT.keys()).fillna(False).astype(bool)].copy()
    # One spelling per unit in the output (litre, liters... -> l).
    df['unit_of_measurement'] = df['unit_of_measurement'].map(
        lambda unit: unit if unit in ('ml', 'fl oz') else 'l').astype(object)

    df['total_quantity'] = pd.to_numeric(df['total_quantity'], errors='coerce')
    valid_quantity = (df['total_quantity'] > 0).fillna(False).astype(bool)
    if (~valid_quantity).any():  # bug 8: quantities <= 0 never reach training
        logger.info(f"Excluded {int((~valid_quantity).sum())} volumetric rows with a missing or non-positive quantity.")
    df = _drop_invalid_prices(df[valid_quantity]).copy()

    df['volume_liters'] = df['total_quantity'] * df['unit_of_measurement'].map(LITERS_PER_UNIT).astype(float)
    df['price_per_liter'] = df['price_sar'] / df['volume_liters']
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=['price_per_liter'])
    df = df[df['price_per_liter'] > 0]  # a tiny price can underflow to 0 per unit

    df.to_csv('ml_model/competitor_volumetric_processed.csv', index=False)
    logger.info("✅ File 'competitor_volumetric_processed.csv' saved.")


def process_unit_data(df):
    logger.info("--- 2b. Processing unit products ---")
    df['total_quantity'] = pd.to_numeric(df['total_quantity'], errors='coerce')
    df = df.dropna(subset=['total_quantity'])
    df = df[df['total_quantity'] > 0]
    df = _drop_invalid_prices(df).copy()

    df['price_per_item'] = df['price_sar'] / df['total_quantity']
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=['price_per_item'])
    df = df[df['price_per_item'] > 0]  # a tiny price can underflow to 0 per unit

    df.to_csv('ml_model/competitor_unit_processed.csv', index=False)
    logger.info("✅ File 'competitor_unit_processed.csv' saved.")


def main():
    """Errors propagate (no swallowing): the `__main__` block logs them and exits with 1."""
    competitor_data = load_and_clean_raw_data()

    # Volume and mass units go to the volumetric step (which drops the mass rows); anything else
    # (units, pieces...) is priced per piece.
    unit = normalize_unit(competitor_data['unit_of_measurement'])
    is_volumetric = unit.isin(set(LITERS_PER_UNIT) | MASS_UNITS).fillna(False).astype(bool)

    process_volumetric_data(competitor_data[is_volumetric].copy())
    process_unit_data(competitor_data[~is_volumetric].copy())

    logger.info("🎉 Competitor data preprocessing completed.")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Competitor data preprocessing failed.")
        sys.exit(1)
