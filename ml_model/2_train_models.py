import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, r2_score, mean_absolute_percentage_error
import joblib
import os
import sys

# Add the root directory to the path so log_config can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_config import get_logger

logger = get_logger()

# F7a robustness rules (docs/plan.md, F7a; docs/decisions.md, 2026-11-24).
# p10-p90 trimming only for types with at least this many competitor rows: with 2 different
# prices the bounds fell strictly between them and emptied the type (bug 6). From 3 rows the
# trimming always keeps the median row, so it never empties a type; it stays as before there
# (keeping all 3-4 rows let single absurd rows into training: x181 on one type in the F1 data).
MIN_ROWS_TO_TRIM = 3
# Competitor sizes of 5 mL or less are not plausible liquids (samples, typos such as 1 mL at
# 59.98 SAR); they are dropped before the quantiles so they cannot move the bounds either.
MIN_VOLUME_LITERS = 0.005
# Market rows (the rows 3_predicted_prices.py computes each SKU's market band from).
# `is_reference` (F7b): a Saudi reference price, used for the B2C ceiling (see is_saudi_reference).
MARKET_ROWS_COLUMNS = ['type_of_product', 'price_per_unit', 'size', 'is_reference']
# Price per liter vs size elasticity b (price_per_liter ~ size ** b), measured on the volumetric
# competitor rows (Mafer, 2026-11-25). 3_predicted_prices.py uses it to adjust a price per liter
# to the SKU's size when the market has too few similar-size rows. Clipped to a plausible range;
# DEFAULT when it cannot be measured (no type with ELASTICITY_MIN_ROWS rows of different sizes).
# Measured on the single-product rows after the variant collapse and BEFORE the p10-p90 trim:
# trimming each type on the price per liter cuts the small (dear) and bulk (cheap) ends of the
# curve and flattens the slope (2026-11-25 data: about -0.75 before the trim, -0.44 after).
ELASTICITY_MIN_ROWS = 3
ELASTICITY_BOUNDS = (-1.0, -0.3)
ELASTICITY_DEFAULT = -0.6
ELASTICITY_FILE = 'vol_size_elasticity.joblib'
# Company values that name no brand: such rows are never collapsed as variants of each other.
UNKNOWN_BRANDS = {'', 'brand not found', 'company not found', 'unknown', 'n/a', 'na', 'none', 'nan', 'generic'}

def load_training_data():
    """Loads all CSV files necessary for training."""
    logger.info("--- 1. Loading base files ---")
    df_quotes_raw = pd.read_csv('ml_model/wayakit_cotizations.csv')
    df_wayakit_products = pd.read_csv('ml_model/wayakit_products_to_predict_odoo.csv')
    df_vol_comp = pd.read_csv('ml_model/competitor_volumetric_processed.csv')
    df_unit_comp = pd.read_csv('ml_model/competitor_unit_processed.csv')
    logger.info("✅ Files loaded.")
    return df_quotes_raw, df_wayakit_products, df_vol_comp, df_unit_comp

def prepare_wayakit_training_data(df_quotes_raw, df_wayakit_products):
    """Prepares Wayakit's historical quotation data."""
    logger.info("--- 2. Preparing Wayakit quotation data ---")
    df_quotes_raw.columns = df_quotes_raw.columns.str.strip()
    df_wayakit_products.columns = df_wayakit_products.columns.str.strip()
    
    df_wayakit_products_renamed = df_wayakit_products.rename(columns={
        'Industry': 'industry', 'SubIndustry': 'subindustry',
        'Generic product type': 'generic_product_type', 'Type_of_product': 'type_of_product'
    })

    # A SKU repeated in the catalog would duplicate its quotes in the merge (bug 7). One row per
    # SKU is kept: the first one with a size (volume or pieces), else the first one.
    has_size = pd.Series(False, index=df_wayakit_products_renamed.index)
    for column in ('Volume_Liters', 'Pack_quantity_Units'):
        if column in df_wayakit_products_renamed:
            has_size |= pd.to_numeric(df_wayakit_products_renamed[column], errors='coerce').fillna(0) > 0
    ordered = df_wayakit_products_renamed.assign(_sized=has_size).sort_values('_sized', ascending=False, kind='stable')
    duplicated = ordered.duplicated(subset='Product_ID', keep='first')
    if duplicated.any():
        logger.warning(f"{int(duplicated.sum())} repeated Product_ID rows in the catalog: "
                       "one row per SKU (the first with a size) is used to join the quotes.")
    df_wayakit_products_renamed = ordered[~duplicated].drop(columns='_sized').sort_index()

    df_wayakit_train_base = pd.merge(df_quotes_raw, df_wayakit_products_renamed, on='Product_ID', how='inner')
    df_wayakit_train_base['company'] = 'Wayakit'
    
    b2c_conditions = ['Home', 'Automotive', 'Pets']
    df_wayakit_train_base['channel'] = np.where(df_wayakit_train_base['subindustry'].isin(b2c_conditions), 'B2C', 'B2B')
    
    logger.info(f"✅ Prepared {len(df_wayakit_train_base)} Wayakit quotations.")
    return df_wayakit_train_base

def drop_implausible_sizes(df_vol):
    """Drops volumetric competitor rows of 5 mL or less (MIN_VOLUME_LITERS), before any quantile."""
    keep = df_vol['volume_liters'] > MIN_VOLUME_LITERS
    logger.info(f"Excluded {int((~keep).sum())} volumetric competitor rows of {MIN_VOLUME_LITERS * 1000:g} mL or less.")
    return df_vol[keep].copy()


def collapse_identical_variants(df_comp, size_col, label):
    """Keeps one row per (type_of_product, company, size, price_sar) (Mafer, 2026-11-25).

    Identical variants (five scents of one floor cleaner at the same size and price, the same
    product listed twice) are ONE competitor product: counted five times they passed the 3-row
    band threshold alone and overweighted the forest. Runs before the p10-p90 trimming, so for
    training and for the market rows. The company is compared trimmed and case-insensitive; rows
    with no known brand (UNKNOWN_BRANDS) are never collapsed; a multipack and a single bottle never
    collapse together; the size and the price to 1e-6 and to the cent. Of a group,
    a Saudi reference row (`is_saudi_reference`) is kept if there is one, else the first row; the
    input order is kept.
    """
    if df_comp.empty:
        return df_comp.copy()
    company = (df_comp['company'].astype('string').str.strip().str.lower().fillna('')
               if 'company' in df_comp else pd.Series('', index=df_comp.index))
    multipack = (df_comp['is_multipack'].astype('boolean').fillna(False).astype(bool)
                 if 'is_multipack' in df_comp else pd.Series(False, index=df_comp.index))
    key = pd.DataFrame({
        'type': df_comp['type_of_product'],
        'company': company,
        'size': pd.to_numeric(df_comp[size_col], errors='coerce').round(6),
        'price': pd.to_numeric(df_comp['price_sar'], errors='coerce').round(2),
        'multipack': multipack,
    }, index=df_comp.index)
    # Reference rows first, so the one kept of a group is a reference row when the group has one.
    order = (~is_saudi_reference(df_comp)).sort_values(kind='stable').index
    duplicated = key.loc[order].duplicated(keep='first').reindex(df_comp.index)
    # Without a known brand, the same size and price is not evidence of the same product.
    duplicated &= ~company.isin(UNKNOWN_BRANDS)
    logger.info(f"Collapsed {int(duplicated.sum())} identical competitor variants ({label}): "
                f"{len(df_comp)} -> {int((~duplicated).sum())} rows.")
    return df_comp[~duplicated].copy()


def estimate_size_elasticity(market_rows):
    """Price per liter vs size elasticity b from the volumetric market rows (`MARKET_ROWS_COLUMNS`).

    Pooled within-type OLS of log(price per liter) on log(size): both are demeaned within each
    type, so b compares sizes of the same kind of product, never one type with another. Only types
    with ELASTICITY_MIN_ROWS rows or more and at least two different sizes count. Returns
    `(b, raw, types_used, rows_used)`: b clipped to ELASTICITY_BOUNDS, or ELASTICITY_DEFAULT
    (raw None) when it cannot be estimated.
    """
    prices = pd.to_numeric(market_rows['price_per_unit'], errors='coerce')
    sizes = pd.to_numeric(market_rows['size'], errors='coerce')
    valid = (prices > 0) & (sizes > 0) & market_rows['type_of_product'].notna()
    data = pd.DataFrame({'type': market_rows['type_of_product'][valid],
                         'y': np.log(prices[valid]), 'x': np.log(sizes[valid])})
    groups = data.groupby('type')['x']
    usable = (groups.transform('count') >= ELASTICITY_MIN_ROWS) & (groups.transform('nunique') >= 2)
    data = data[usable]
    if data.empty:
        return ELASTICITY_DEFAULT, None, 0, 0
    x = data['x'] - data.groupby('type')['x'].transform('mean')
    y = data['y'] - data.groupby('type')['y'].transform('mean')
    denominator = float((x * x).sum())
    if not denominator > 0:
        return ELASTICITY_DEFAULT, None, 0, 0
    raw = float((x * y).sum() / denominator)
    if not np.isfinite(raw):
        return ELASTICITY_DEFAULT, None, 0, 0
    b = float(min(max(raw, ELASTICITY_BOUNDS[0]), ELASTICITY_BOUNDS[1]))
    return b, raw, int(data['type'].nunique()), int(len(data))


def size_curve_rows(df_vol):
    """The rows the elasticity is measured on, as `MARKET_ROWS_COLUMNS`: the volumetric competitor
    rows given (main passes them after the variant collapse, before the trim) without multipacks."""
    multipack = df_vol['is_multipack'] if 'is_multipack' in df_vol else False
    multipack = pd.Series(multipack, index=df_vol.index).fillna(False).astype(bool)
    rows = df_vol[~multipack]
    return pd.DataFrame({
        'type_of_product': rows['type_of_product'].values,
        'price_per_unit': rows['price_per_liter'].astype(float).values,
        'size': rows['volume_liters'].astype(float).values,
        'is_reference': is_saudi_reference(rows).values,
    }, columns=MARKET_ROWS_COLUMNS)


def save_size_elasticity(df_vol, model_dir):
    """Estimates the elasticity (`estimate_size_elasticity`) on `df_vol` (volumetric competitor rows,
    collapsed and not trimmed) once over every row and once per channel, logs them and saves a dict
    {channel: b, '': pooled b} to ELASTICITY_FILE next to the models. Bulk B2B prices fall less
    with size than retail ones (2026-11-25 data with these fixes: pooled -0.76, B2B -0.63, B2C -0.85), so each SKU uses
    its own channel's b; a channel that cannot be estimated gets the pooled one."""
    def estimate(rows, label):
        b, raw, types_used, rows_used = estimate_size_elasticity(size_curve_rows(rows))
        if raw is None:
            logger.warning(f"Price per liter vs size elasticity ({label}) could not be estimated.")
            return None
        logger.info(f"Price per liter vs size elasticity ({label}): {b:.3f} (raw {raw:.3f}, clipped to "
                    f"{list(ELASTICITY_BOUNDS)}; {types_used} types, {rows_used} rows).")
        return b

    pooled = estimate(df_vol, 'all channels')
    elasticities = {'': ELASTICITY_DEFAULT if pooled is None else pooled}
    if pooled is None:
        logger.warning(f"Using the default elasticity {ELASTICITY_DEFAULT}.")
    channels = df_vol['channel'].astype('string').str.strip().fillna('') if 'channel' in df_vol else None
    if channels is not None:
        for channel in sorted(set(channels) - {''}):
            b = estimate(df_vol[channels == channel], channel)
            elasticities[channel] = elasticities[''] if b is None else b
    joblib.dump(elasticities, os.path.join(model_dir, ELASTICITY_FILE))
    return elasticities


def filter_outliers_with_percentiles(df_comp, metric_col, p_lower=0.10, p_upper=0.90):
    """Per type_of_product, keeps the rows between the p10 and p90 of `metric_col` (inclusive).

    Types with fewer than MIN_ROWS_TO_TRIM rows are kept whole (bug 6: with 2 different prices
    the bounds fell strictly between them and the type lost every competitor). Rows without a
    type or without a metric never pass.
    """
    logger.info(f"--- 3. Filtering outliers for '{metric_col}' using percentiles {p_lower}-{p_upper} "
                f"(types with >= {MIN_ROWS_TO_TRIM} rows) ---")
    groups = df_comp.groupby('type_of_product')[metric_col]
    lower_bound = groups.transform('quantile', p_lower)
    upper_bound = groups.transform('quantile', p_upper)
    group_size = groups.transform('count')

    original_rows = len(df_comp)
    in_bounds = (df_comp[metric_col] >= lower_bound) & (df_comp[metric_col] <= upper_bound)
    small_group = group_size < MIN_ROWS_TO_TRIM
    has_values = df_comp[metric_col].notna() & group_size.notna()
    df_cleaned = df_comp[has_values & (in_bounds | small_group)].copy()

    logger.info(f"Rows before: {original_rows} | Rows after: {len(df_cleaned)}")
    df_cleaned['approved_quote_price'] = 0.0
    return df_cleaned


def is_saudi_reference(df):
    """True for a price that is known to be from the Saudi market itself (F7b, Mafer 2026-11-24):
    an Odoo `competitor.product` price seen in a physical store (`source_channel` 'retail'), or a
    scraped product whose data says it is made in Saudi Arabia (`origin` 'local', F6). Odoo
    e-commerce prices are not (they can be the same imported listings), nor B2B ones. Only from
    the data; no brand lists. Files without the columns (pre-F7b) have no reference rows.
    """
    def text(column):
        if column not in df:
            return pd.Series('', index=df.index)
        return df[column].astype('string').str.strip().str.lower().fillna('')
    return ((text('source_channel') == 'retail') | (text('origin') == 'local')).astype(bool)


def build_market_rows(df_comp_cleaned, metric_col, size_col):
    """The competitor rows the market band is computed from: the trimmed rows minus multipacks.

    Returns `MARKET_ROWS_COLUMNS`: type, price per liter or per piece, size (liters or pieces)
    and `is_reference` (`is_saudi_reference`). Rows from a processed file without `is_multipack`
    (pre-F7a) count as single products.
    """
    multipack = df_comp_cleaned['is_multipack'] if 'is_multipack' in df_comp_cleaned else False
    multipack = pd.Series(multipack, index=df_comp_cleaned.index).fillna(False).astype(bool)
    rows = df_comp_cleaned[~multipack]
    reference = is_saudi_reference(rows)
    logger.info(f"Market rows ({metric_col}): {len(rows)} ({int(multipack.sum())} multipacks left out; "
                f"{int(reference.sum())} Saudi reference prices).")
    return pd.DataFrame({
        'type_of_product': rows['type_of_product'].values,
        'price_per_unit': rows[metric_col].astype(float).values,
        'size': rows[size_col].astype(float).values,
        'is_reference': reference.values,
    }, columns=MARKET_ROWS_COLUMNS)


def train_and_evaluate(X, y, label):
    """Trains a RandomForest, logs 80/20 holdout metrics and returns the final model trained on all the data."""
    # min_samples_leaf=1 and extended features chosen by holdout with real data (Jul 2026):
    # volumetric MAPE 60%->44% and MAE on Wayakit rows 27.4->16.3 SAR vs the previous config.
    rf_params = dict(n_estimators=300, random_state=42, min_samples_leaf=1, oob_score=True, n_jobs=-1)

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    eval_model = RandomForestRegressor(**rf_params).fit(X_train, y_train)
    y_pred = eval_model.predict(X_test)
    logger.info(
        f"📊 Holdout metrics ({label}): "
        f"MAE={mean_absolute_error(y_test, y_pred):.2f} SAR | "
        f"R²={r2_score(y_test, y_pred):.3f} | "
        f"MAPE={mean_absolute_percentage_error(y_test, y_pred) * 100:.1f}%"
    )

    final_model = RandomForestRegressor(**rf_params).fit(X, y)
    logger.info(f"📊 OOB R² ({label}, final model): {final_model.oob_score_:.3f}")
    return final_model

def market_stats(market_rows):
    """Per type: min, max and count of the price per unit, and the size range of the market rows."""
    return market_rows.groupby('type_of_product').agg(
        market_min=('price_per_unit', 'min'),
        market_max=('price_per_unit', 'max'),
        competitor_count=('price_per_unit', 'count'),
        # Observed size range: outside it the price per liter or per piece is not extrapolated
        vol_min=('size', 'min'),
        vol_max=('size', 'max'),
    ).reset_index()


def save_market_stats(vol_rows, unit_rows, model_dir):
    """Saves the market rows (what prediction computes each SKU's band from) and, as a summary
    for the logs and diagnosis, the per-type stats (min, max, count, size range)."""
    logger.info("--- 5a. Saving market rows and stats for the guardrails ---")
    joblib.dump(vol_rows, os.path.join(model_dir, 'vol_market_rows.joblib'))
    joblib.dump(unit_rows, os.path.join(model_dir, 'unit_market_rows.joblib'))
    joblib.dump(market_stats(vol_rows), os.path.join(model_dir, 'vol_market_stats.joblib'))
    joblib.dump(market_stats(unit_rows), os.path.join(model_dir, 'unit_market_stats.joblib'))
    logger.info("✅ Market rows and stats saved.")

def main():
    """
    Orchestrates the entire training process: loading, preparation,
    training and saving models. Errors propagate: the `__main__` block logs them and exits with 1.
    """
    # Data loading
    df_quotes, df_products, df_vol_comp, df_unit_comp = load_training_data()

    # Wayakit data preparation
    df_wayakit_base = prepare_wayakit_training_data(df_quotes, df_products)

    # Trimming per type (not for small types), after dropping implausible sizes and collapsing
    # identical variants (one competitor product, one row).
    df_vol_comp = drop_implausible_sizes(df_vol_comp)
    df_vol_comp = collapse_identical_variants(df_vol_comp, 'volume_liters', 'volumetric')
    df_unit_comp = collapse_identical_variants(df_unit_comp, 'total_quantity', 'units')
    df_vol_comp_cleaned = filter_outliers_with_percentiles(df_vol_comp, 'price_per_liter')
    df_unit_comp_cleaned = filter_outliers_with_percentiles(df_unit_comp, 'price_per_item')

    # --- Data transformation and combination (your original logic) ---
    logger.info("--- 4. Combining Wayakit and competitor data for training ---")
    cols_vol = ['industry', 'subindustry', 'type_of_product', 'generic_product_type', 'price_sar', 'company', 'unit_of_measurement', 'channel', 'volume_liters', 'price_per_liter', 'approved_quote_price']
    cols_unit = ['industry', 'subindustry', 'type_of_product', 'generic_product_type', 'price_sar', 'company', 'unit_of_measurement', 'total_quantity', 'channel', 'price_per_item', 'approved_quote_price']

    df_wayakit_vol_train = df_wayakit_base[df_wayakit_base['Volume_Liters'] > 0].copy()
    df_wayakit_vol_train['volume_liters'] = df_wayakit_vol_train['Volume_Liters']
    df_wayakit_vol_train['price_per_liter'] = df_wayakit_vol_train['approved_quote_price'] / df_wayakit_vol_train['volume_liters']
    df_wayakit_vol_train['price_sar'] = df_wayakit_vol_train['approved_quote_price']
    df_wayakit_vol_train['unit_of_measurement'] = np.where(df_wayakit_vol_train['volume_liters'] < 1, 'ml', 'l')
    df_wayakit_vol_train_final = df_wayakit_vol_train[cols_vol].dropna(subset=['price_per_liter'])


    df_wayakit_unit_train = df_wayakit_base[df_wayakit_base['Pack_quantity_Units'] > 0].copy()
    df_wayakit_unit_train['price_per_item'] = df_wayakit_unit_train['approved_quote_price'] / df_wayakit_unit_train['Pack_quantity_Units']
    df_wayakit_unit_train['price_sar'] = df_wayakit_unit_train['approved_quote_price']
    df_wayakit_unit_train['total_quantity'] = pd.to_numeric(df_wayakit_unit_train['Pack_quantity_Units'])
    df_wayakit_unit_train['unit_of_measurement'] = 'units'
    df_wayakit_unit_train_final = df_wayakit_unit_train[cols_unit].dropna(subset=['price_per_item'])


    df_train_vol = pd.concat([df_vol_comp_cleaned[cols_vol], df_wayakit_vol_train_final], ignore_index=True)
    df_train_unit = pd.concat([df_unit_comp_cleaned[cols_unit], df_wayakit_unit_train_final], ignore_index=True)
    
    logger.info(f"Total data for volumetric training: {len(df_train_vol)} rows.")
    logger.info(f"Total data for unit training: {len(df_train_unit)} rows.")

    # --- Model training and saving (your original logic) ---
    logger.info("--- 5. Training and saving models ---")
    # approved_quote_price left out of the features: it is the target's numerator in Wayakit rows (leakage)
    cat_features = ['type_of_product', 'subindustry', 'channel', 'generic_product_type', 'industry', 'company']

    features_vol = ['volume_liters'] + cat_features
    X_vol_encoded = pd.get_dummies(df_train_vol[features_vol].fillna('Desconocido'), columns=cat_features, drop_first=True)
    model_vol = train_and_evaluate(X_vol_encoded, df_train_vol['price_per_liter'], 'volumetric')

    features_unit = ['total_quantity'] + cat_features
    X_unit_encoded = pd.get_dummies(df_train_unit[features_unit].fillna('Desconocido'), columns=cat_features, drop_first=True)
    model_unit = train_and_evaluate(X_unit_encoded, df_train_unit['price_per_item'], 'unit')

    model_dir = 'ml_model/trained_models'
    os.makedirs(model_dir, exist_ok=True)
    joblib.dump(model_vol, os.path.join(model_dir, 'volumetric_model.joblib'))
    joblib.dump(X_vol_encoded.columns.tolist(), os.path.join(model_dir, 'volumetric_model_columns.joblib'))
    joblib.dump(model_unit, os.path.join(model_dir, 'unit_model.joblib'))
    joblib.dump(X_unit_encoded.columns.tolist(), os.path.join(model_dir, 'unit_model_columns.joblib'))
    # Market rows from competitors only: the market range must not include Wayakit's own prices
    save_market_stats(build_market_rows(df_vol_comp_cleaned, 'price_per_liter', 'volume_liters'),
                      build_market_rows(df_unit_comp_cleaned, 'price_per_item', 'total_quantity'),
                      model_dir)
    save_size_elasticity(df_vol_comp, model_dir)  # collapsed, not trimmed
    
    logger.info("🎉 Models and columns successfully saved.")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Error occurred in training process.")
        sys.exit(1)