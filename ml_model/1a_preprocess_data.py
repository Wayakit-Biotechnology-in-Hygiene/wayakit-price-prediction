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
# A liquid size in an Arabic title ("1 لتر", "350 ملي", "(5لتر)").
_ARABIC_LIQUID_SIZE_RE = re.compile(r"\d\s*(?:مل|ملي|لتر|لترات|ليتر)(?![\w])")


def case_pack(title):
    """Number of units in a case notation of `title` (2 or more), 1 when it states no case.

    Reads only the notations above: "<size> <container> x N", "N cartons", "N كراتين" and an
    Arabic count of the same product, in a carton or next to a liquid size ("1 لتر 3 حبات"). Everything else is left to the adapters' readers (`volume_pack`, `pack_count`).
    """
    if title is None or pd.isna(title):
        return 1
    text = str(title).translate(_ARABIC_DIGITS)
    for pattern in (_CASE_SIZE_CONTAINER_X_N_RE, _CASE_CARTONS_RE, _ARABIC_CARTONS_RE):
        for match in pattern.finditer(text):
            if int(match.group(1)) >= 2:
                return int(match.group(1))
    # An Arabic count of the same product: with a carton word, or (audit 2026-11-26, D2) next to a
    # liquid size without one ("سائل الصحون 1 لتر 3 حبات"). A count with no liquid size is left to
    # the units-mode readers (a piece count such as "50 حبة" is not a pack).
    if _ARABIC_CARTON_RE.search(text) or _ARABIC_LIQUID_SIZE_RE.search(text):
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


# Audit 2026-11-26 (D1): a product whose price per liter is far above the rest of the same search
# in the same store has a wrong size (GoGreen "750 مل" at 220 SAR is a 5 L can; OfficeSupply
# "(1 Liter x 12 bottles)" at 165 SAR is stored as 1 L). Only stores that sell one catalog of
# their own: on Amazon and Noon a premium brand at many times the median is a real price, and the
# SACO and Mumzworld rows are not changed (ticket A).
OUTLIER_CHECK_SOURCES = ('gogreen', 'officesupply', 'fine', 'aerosense')
# Each other product's price per liter is first brought to the product's size with this elasticity
# (price per liter ~ size^b; a smaller bottle costs more per liter). -0.5 is near the B2B value
# measured per run in 2_train_models.py, which needs this step's output, so it cannot be used here.
OUTLIER_SIZE_ELASTICITY = -0.5
# A product is dropped above this ratio to the median of the other products of its search, with at
# least OUTLIER_MIN_OTHERS of them. In the 2026-11-24 and 2026-11-26 data, 4 flags exactly the four
# wrong sizes and nothing else; 3 already flags right ones (AeroSense cases, FRESH 150 ml).
OUTLIER_MAX_RATIO = 4.0
OUTLIER_MIN_OTHERS = 3
_SEARCH_COLUMNS = ['source', 'industry', 'subindustry', 'type_of_product', 'generic_product_type']


def price_per_liter_outliers(df):
    """Boolean Series over `df` (the scraper CSV): True for every row of a product that, in at least
    one search, costs more than OUTLIER_MAX_RATIO times the median price per liter of the other
    products of that search, after bringing them to its size. Only volumetric rows of
    OUTLIER_CHECK_SOURCES are compared; a product is its URL (else its title), size and price in its
    store, so all its rows go, also those in searches with too few products to compare."""
    flags = pd.Series(False, index=df.index)
    if not set(_SEARCH_COLUMNS + ['price_sar', 'unit_of_measurement', 'total_quantity']) <= set(df.columns):
        return flags
    source = df['source'].astype('string').str.strip().str.lower()
    liters = pd.to_numeric(df['total_quantity'], errors='coerce') * \
        normalize_unit(df['unit_of_measurement']).map(LITERS_PER_UNIT).astype(float)
    price = pd.to_numeric(df['price_sar'].astype(str).str.replace('SAR', '').str.replace(',', '').str.strip(),
                          errors='coerce')
    url = df['url'].astype('string') if 'url' in df else pd.Series(pd.NA, index=df.index, dtype='string')
    title = df['product'].astype('string') if 'product' in df else pd.Series('', index=df.index, dtype='string')
    # One product = one item sold: AeroSense lists every size and case pack of a product on one page,
    # so the URL alone would merge them (and drop a right case pack with a wrong one).
    product = (source.fillna('') + '|' + url.fillna(title.fillna('')) + '|' + liters.astype(str)
               + '|' + price.astype(str))
    checked = (source.isin(OUTLIER_CHECK_SOURCES) & (liters > 0) & (price > 0)).fillna(False).astype(bool)
    rows = df.loc[checked, _SEARCH_COLUMNS].copy()
    rows['liters'], rows['per_liter'], rows['product'] = liters[checked], (price / liters)[checked], product[checked]
    flagged = set()
    for _, search in rows.groupby(_SEARCH_COLUMNS, dropna=False):
        search = search.drop_duplicates('product')
        if len(search) < OUTLIER_MIN_OTHERS + 1:
            continue
        for index, row in search.iterrows():
            others = search.drop(index)
            at_its_size = others['per_liter'] * (row['liters'] / others['liters']) ** OUTLIER_SIZE_ELASTICITY
            if row['per_liter'] > OUTLIER_MAX_RATIO * at_its_size.median():
                flagged.add(row['product'])
    return product.isin(flagged).fillna(False).astype(bool)


def drop_price_per_liter_outliers(df):
    """`df` (the scraper CSV) without the products `price_per_liter_outliers` flags, with a log line
    naming each one (store, title, price)."""
    outliers = price_per_liter_outliers(df)
    if outliers.any():
        names = df.loc[outliers, [c for c in ('source', 'product', 'price_sar') if c in df]].drop_duplicates()
        for values in names.itertuples(index=False):
            logger.info(f"Excluded a product whose price per liter is far above its search (wrong size?): {tuple(values)}")
    logger.info(f"Excluded {int(outliers.sum())} scraper rows with an outlier price per liter.")
    return df[~outliers].reset_index(drop=True)


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
    df_test4 = drop_price_per_liter_outliers(df_test4)
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
