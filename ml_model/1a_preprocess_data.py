import pandas as pd
import numpy as np
import os
import re
import sys
# Add the root directory to the path so log_config can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_config import get_logger
# Pure title parsing shared with the Apify adapters (no I/O); used here only to flag multipacks
# and to recognise Wayakit's own brand.
from scraper.scrapers.apify_common import is_wayakit, pack_count, volume_pack

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


# Case notations the adapters' pack readers do not read (audit 2026-11-25). Their size totals are
# already right; only the multipack flag was missing, so a case entered the market band as if it
# were one bottle of the total size.
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
# AeroSense: "<n> ml|litre <container> x <N>" ("150 ml spray bottle x 24", "5 litre can x 4"). The
# container words must sit between the size and the "x", so "40 x 40 cm" and a plain "500 ml x 2"
# (read by `volume_pack`) never reach this pattern, and a decimal or a length after N is not a case.
_CONTAINER_WORDS = (r"(?:spray\s+|plastic\s+|glass\s+|aerosol\s+|trigger\s+)?"
                    r"(?:bottles?|cans?|jerry\s?cans?|canisters?|drums?|pails?|jugs?|pouch(?:es)?|tubes?"
                    r"|jars?|containers?|refills?|aerosols?|sprays?|gallons?)")
_CASE_SIZE_CONTAINER_X_N_RE = re.compile(
    r"(?<![\d.,])\d+(?:[.,]\d+)?\s*(?:ml|millilitres?|milliliters?|l|ltrs?|litres?|liters?)\s+"
    + _CONTAINER_WORDS + r"\s*[x×*]\s*(\d{1,4})(?![\d]|[.,]\d)(?!\s*(?:cm|mm|m|in|inch(?:es)?|ft)\b)",
    re.I)
# AeroSense units: "4 cartons (2000 pcs)". One carton ("1 carton (500 pcs)") is a single package.
_CASE_CARTONS_RE = re.compile(r"(?<![\d.,])(\d{1,4})\s*cartons?\b", re.I)
# GoGreen (Arabic): a carton (كرتون) with a count of the same product, "4 حبة (5لتر)" or "12 حبة ...
# 1 لتر" (the same count words as `gogreen_scraper._COUNT`, which already multiplied the total).
_ARABIC_CARTON_RE = re.compile(r"كرتون")
# The plural, "4 كراتين" (4 cartons) or "4 كرتونات", is itself a count of cartons.
_ARABIC_CARTONS_RE = re.compile(r"(?<![\d.,])(\d{1,4})\s*(?:كراتين|كرتونات)(?![\w])")
_ARABIC_COUNT_RE = re.compile(r"(?<![\d.,])(\d{1,4})\s*(?:حبات|حبة|قطعة|قطع|عبوات|عبوة)(?![\w])")


def case_pack(title):
    """Number of units in a case notation of `title` (2 or more), 1 when it states no case.

    Reads only the notations above: "<size> <container> x N", "N cartons", "N كراتين" and an
    Arabic carton with a count. Everything else is left to the adapters' readers (`volume_pack`, `pack_count`).
    """
    if title is None or pd.isna(title):
        return 1
    text = str(title).translate(_ARABIC_DIGITS)
    for pattern in (_CASE_SIZE_CONTAINER_X_N_RE, _CASE_CARTONS_RE, _ARABIC_CARTONS_RE):
        for match in pattern.finditer(text):
            if int(match.group(1)) >= 2:
                return int(match.group(1))
    if _ARABIC_CARTON_RE.search(text):
        counts = [int(match.group(1)) for match in _ARABIC_COUNT_RE.finditer(text)]
        if len(counts) == 1 and counts[0] >= 2:
            return counts[0]
    return 1


def flag_multipacks(titles, units, quantities):
    """True for a row whose title states a pack of 2 or more of the same product.

    Only scraper rows have a title; Odoo `competitor.product` rows (no title) are never flagged.
    Volume rows use the adapters' volume-mode pack reader ("Twin Pack", "500ml x 2"...), the
    others the units-mode one; on top of both, `case_pack` reads the AeroSense and GoGreen case
    notations ("150 ml spray bottle x 24", "4 cartons (2000 pcs)", "كرتون ... 4 حبة (5لتر)"). A
    per-piece row is a multipack only when the pack count is not its piece count ("Pack of 6, 480
    wipes" is; "Wipes Pack of 72" with 72 pieces is one pack). The price stays the pack's total:
    the flag only keeps the row out of the market range (F7a), not out of training.
    """
    flags = []
    for title, unit, quantity in zip(titles, units, quantities):
        if title is None or pd.isna(title):
            flags.append(False)
            continue
        case = case_pack(title)
        if not pd.isna(unit) and str(unit) in LITERS_PER_UNIT:
            flags.append(bool(volume_pack(title) > 1 or case > 1))
            continue
        pieces = pd.to_numeric(quantity, errors='coerce')
        flags.append(any(pack > 1 and not (pd.notna(pieces) and pieces == pack)
                         for pack in (pack_count(title), case)))
    return flags


def drop_wayakit_rows(df, company_column, label):
    """`df` without the rows whose company is Wayakit (our own price, never a competitor: e.g. an
    Odoo `competitor.product` record with company WAYAKIT), with a log line of how many."""
    if company_column not in df:
        return df
    own = df[company_column].map(lambda company: bool(pd.notna(company) and is_wayakit(str(company).strip())))
    own = own.astype(bool)
    logger.info(f"Excluded {int(own.sum())} {label} rows whose company is Wayakit (our own price).")
    return df[~own].reset_index(drop=True)


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
    # Our own prices are never a competitor price: out of training and out of the market band.
    df_test4 = drop_wayakit_rows(df_test4, 'company', 'scraper')
    df_prod = drop_wayakit_rows(df_prod, 'Company', 'Odoo competitor')

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
