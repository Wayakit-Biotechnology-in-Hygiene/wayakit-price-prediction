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

    # Trimming per type (not for small types), after dropping implausible sizes.
    df_vol_comp = drop_implausible_sizes(df_vol_comp)
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
    
    logger.info("🎉 Models and columns successfully saved.")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Error occurred in training process.")
        sys.exit(1)