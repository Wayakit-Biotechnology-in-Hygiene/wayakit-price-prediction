import math

import pandas as pd
import numpy as np
import joblib
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_config import get_logger

logger = get_logger()

# Pricing rules (F7a; docs/plan.md and docs/decisions.md, 2026-11-24).
MARGIN_FLOOR = 1.30            # the final price is never below cost x 1.30 (rounded up to the cent)
MIN_COMPETITORS_FOR_CLAMP = 3  # with 1-2 rows the range collapses to a point and forces absurd prices
SIZE_MATCH_RATIO = 2.0         # "similar size": from size / 2 to size x 2
MIN_SIZE_MATCHES = 3           # fewer similar-size rows -> the band uses every row of the type
# F7b (Mafer, 2026-11-24): for a B2C SKU, the ceiling comes from the Saudi reference prices of its
# type (Odoo retail, scraped 'local' origin) when there are at least this many; the floor and the
# model keep using every competitor row. B2B SKUs are not affected.
MIN_REFERENCE_ROWS = 3
CEILING_QUANTILE = 0.75        # the ceiling is the p75 per liter / per piece x the SKU's size
CAT_FEATURES = ['type_of_product', 'subindustry', 'channel', 'generic_product_type', 'industry', 'company']

def load_artifacts():
    """Loads trained models, column lists and market rows from disk."""
    logger.info("--- 1. Loading trained models and columns ---")
    model_dir = 'ml_model/trained_models'

    # A missing file raises FileNotFoundError (run 2_train_models.py first); it is not swallowed.
    model_vol = joblib.load(os.path.join(model_dir, 'volumetric_model.joblib'))
    model_unit = joblib.load(os.path.join(model_dir, 'unit_model.joblib'))
    vol_cols = joblib.load(os.path.join(model_dir, 'volumetric_model_columns.joblib'))
    unit_cols = joblib.load(os.path.join(model_dir, 'unit_model_columns.joblib'))
    vol_market = joblib.load(os.path.join(model_dir, 'vol_market_rows.joblib'))
    unit_market = joblib.load(os.path.join(model_dir, 'unit_market_rows.joblib'))

    logger.info("✅ Models and columns loaded successfully.")
    return model_vol, model_unit, vol_cols, unit_cols, vol_market, unit_market

def prepare_prediction_data():
    """Loads Wayakit products, cleans them and enriches with past quotation data."""
    logger.info("\n--- 2. Preparing Wayakit product list for prediction ---")
    df_wayakit = pd.read_csv('ml_model/wayakit_products_to_predict_odoo.csv')

    # 1. Clean and rename columns.
    df_wayakit.columns = df_wayakit.columns.str.strip()
    df_wayakit.rename(columns={
        'SubIndustry': 'subindustry', 'Industry': 'industry',
        'Generic product type': 'generic_product_type', 'Type_of_product':'type_of_product'
    }, inplace=True)

    # 2. Add 'company' and 'channel' columns.
    df_wayakit['company'] = 'Wayakit'
    b2c_subindustries = ['home', 'automotive', 'pets']
    df_wayakit['channel'] = np.where(df_wayakit['subindustry'].str.lower().isin(b2c_subindustries), 'B2C', 'B2B')
    
    logger.info(f"✅ Prepared {len(df_wayakit)} products for prediction.")
    return df_wayakit

def calculate_confidence(model, X):
    """Computes the prediction confidence based on the variance of the trees."""
    
    # --- FIX: Convert DataFrame to numpy array ---
    # This removes the warnings because the internal trees expect only numbers, not names.
    X_input = X.values if hasattr(X, 'values') else X
    
    # model.estimators_ contains all the decision trees
    preds = np.array([tree.predict(X_input) for tree in model.estimators_])
    
    mean_pred = np.mean(preds)
    std_dev = np.std(preds)
    
    if mean_pred == 0:
        return 0.0
        
    cov = std_dev / mean_pred
    
    # If the deviation is low, the confidence is high.
    confidence = max(0, (1 - (cov * 2))) * 100
    return round(confidence, 2)

def market_band(rows, size):
    """Market band of one SKU from the market rows of its type (price per unit and size columns).

    - `count`: rows of the type; `low`: their minimum price per unit; `max`: their maximum;
      `size_min` / `size_max`: their size range.
    - `median` / `p75`: from the rows of similar size (size / 2 to size x 2) when there are at
      least MIN_SIZE_MATCHES of them (`match` = 'size'), else from every row (`match` = 'all').
      Small packs cost more per liter or per piece, so a p75 over all sizes would not bind.
    Returns None when the type has no market rows.
    """
    if rows is None or len(rows) == 0:
        return None
    prices = rows['price_per_unit'].to_numpy(dtype=float)
    sizes = rows['size'].to_numpy(dtype=float)
    similar = (sizes >= size / SIZE_MATCH_RATIO) & (sizes <= size * SIZE_MATCH_RATIO)
    match = 'size' if similar.sum() >= MIN_SIZE_MATCHES else 'all'
    used = prices[similar] if match == 'size' else prices
    return {
        'count': len(prices), 'low': float(prices.min()), 'max': float(prices.max()),
        'median': float(np.quantile(used, 0.5)), 'p75': float(np.quantile(used, CEILING_QUANTILE)),
        'size_min': float(sizes.min()), 'size_max': float(sizes.max()), 'match': match,
    }


def apply_margin_floor(price, cost):
    """max(price, cost x 1.30) rounded to the cent, never below the floor (rounded up if needed)."""
    floor = cost * MARGIN_FLOOR
    noise = 1e-9 * max(1.0, abs(floor))  # 3873 x 1.30 = 5034.900000000001 is 5034.90, not 5034.91
    final = round(max(price, floor), 2)
    if final < floor - noise:
        final = math.ceil(round(floor * 100, 6)) / 100
        if final < floor - noise:
            final = math.ceil(floor * 100) / 100
    return final


def _size_of(value):
    return float(value) if pd.notna(value) and value > 0 else 0.0


def generate_predictions(df_wayakit, model_vol, model_unit, vol_cols, unit_cols, vol_market, unit_market):
    """One report row per product, in the input order.

    `vol_market` / `unit_market` are the market rows saved by 2_train_models.py
    (`type_of_product`, `price_per_unit`, `size`). Per product with a size:

    1. Model price per liter (or per piece).
    2. With MIN_COMPETITORS_FOR_CLAMP or more market rows of its type, clamped to [low, p75] of
       its market band (`market_band`). For a B2C SKU whose type has MIN_REFERENCE_ROWS or more
       Saudi reference rows (`is_reference`, F7b), the median and p75 come from those rows only
       (`ceiling_source` 'reference'), unless that would replace a similar-size band with an
       all-sizes one (fewer than MIN_SIZE_MATCHES similar-size reference rows while every row has
       them); `ceiling_source` is 'all' for the F7a band and '' when no band applies; `low` always comes from every row (the reference rows
       are among them, so `low` is never above that p75). A volumetric SKU larger than every market row of its
       type is never lifted to `low` (large sizes cost less per liter; `volume_outside_market`); a pack of
       pieces is, at every size (a 20-pack of cloths is still cloths: without it, wipes packs in the units
       model priced 20 microfiber cloths at a tenth of the cheapest cloth, 2026-11-24 run). The p75 ceiling
       applies at every size, also below the market's size range (the plan's "p75 x volume"
       ceiling; lifting it there raised small sizes up to x6 on the F1 data).
    3. A SKU larger than every market row of its type (`volume_outside_market`) costs at most,
       per liter or per piece, what the smallest-priced smaller SKU of the same subindustry and
       type costs (`volume_capped`).
    4. x size, then the margin floor (`apply_margin_floor`), which always wins.

    A product without a size (no-size parent templates) gets the floor and `has_size` False; the
    upload skips it.
    """
    logger.info("\n--- 3. Starting prediction generation ---")
    markets = {
        'volume': {t: g for t, g in vol_market.groupby('type_of_product')},
        'units': {t: g for t, g in unit_market.groupby('type_of_product')},
    }
    items = []

    for _, row in df_wayakit.iterrows():
        volume = _size_of(row['Volume_Liters'])
        pieces = _size_of(row.get('Pack_quantity_Units'))
        kind = 'volume' if volume > 0 else 'units' if pieces > 0 else None
        item = {'row': row, 'kind': kind, 'size': volume if kind == 'volume' else pieces,
                'model_pu': 0.0, 'pu': 0.0, 'confidence': 0.0, 'band': None,
                'clamped': False, 'outside': False, 'capped': False}
        items.append(item)
        if kind is None:
            continue

        size_column, model, columns = (('volume_liters', model_vol, vol_cols) if kind == 'volume'
                                       else ('total_quantity', model_unit, unit_cols))
        single = pd.DataFrame([row]).rename(
            columns={'Volume_Liters' if kind == 'volume' else 'Pack_quantity_Units': size_column})
        single[size_column] = item['size']
        X_pred = pd.get_dummies(single[[size_column] + CAT_FEATURES], columns=CAT_FEATURES)
        X_pred = X_pred.reindex(columns=columns, fill_value=0)
        item['model_pu'] = float(model.predict(X_pred)[0])
        item['confidence'] = calculate_confidence(model, X_pred)
        item['pu'] = item['model_pu']

        rows = markets[kind].get(row.get('type_of_product'))
        band = market_band(rows, item['size'])
        if band is not None:
            band['ceiling_source'] = 'all' if band['count'] >= MIN_COMPETITORS_FOR_CLAMP else ''
            if band['ceiling_source'] and row.get('channel') == 'B2C' and 'is_reference' in rows:
                reference = rows[rows['is_reference'].fillna(False).astype(bool)]
                if len(reference) >= MIN_REFERENCE_ROWS:
                    reference_band = market_band(reference, item['size'])
                    # Never trade a similar-size ceiling for an all-sizes one: shelf bottles of
                    # another size would set the wrong price per liter (small packs cost more).
                    if reference_band['match'] == 'size' or band['match'] == 'all':
                        band.update({key: reference_band[key] for key in ('median', 'p75', 'match')})
                        band['ceiling_source'] = 'reference'
        item['band'] = band
        if band is None:
            continue
        below, above = item['size'] < band['size_min'], item['size'] > band['size_max']
        item['outside'] = below or above
        if band['count'] >= MIN_COMPETITORS_FOR_CLAMP and item['pu'] > 0:
            # Volume: a size above the market is cheaper per liter, so no minimum. Pieces: always.
            lower = 0.0 if above and item['kind'] == 'volume' else band['low']
            clamped = min(max(item['pu'], lower), band['p75'])
            item['clamped'] = clamped != item['pu']
            item['pu'] = clamped

    # Larger than the whole market: no more per liter (or per piece) than a smaller sibling SKU.
    for item in items:
        band = item['band']
        if band is None or item['size'] <= band['size_max'] or item['pu'] <= 0:
            continue
        row = item['row']
        sibling_prices = [
            other['pu'] for other in items
            if other is not item and other['kind'] == item['kind'] and other['pu'] > 0
            and other['size'] < item['size'] and other['band'] is not None
            and other['size'] <= other['band']['size_max']
            and other['row'].get('subindustry') == row.get('subindustry')
            and other['row'].get('type_of_product') == row.get('type_of_product')
        ]
        if sibling_prices and min(sibling_prices) < item['pu']:
            item['pu'] = min(sibling_prices)
            item['capped'] = True

    report_list = []
    for item in items:
        row, band, size = item['row'], item['band'], item['size']
        cost = row['Unit_cost_SAR']
        final_price = apply_margin_floor(item['pu'] * size, cost)
        profit_margin = ((final_price - cost) / cost) * 100 if cost > 0 else 0
        scale = (lambda key: round(band[key] * size, 2)) if band else (lambda key: 0.0)

        report_list.append({
            'Product_ID': row['Product_ID'], 'product_name': row['Product_Name'],
            'product_type': row.get('type_of_product', 'N/A'), 'generic_product_type': row.get('generic_product_type', 'N/A'),
            'subindustry': row.get('subindustry', 'N/A'), 'industry': row.get('industry', 'N/A'),
            'volume': f"{row['Volume_Liters']} L" if item['kind'] == 'volume' else f"{int(size)} units",
            'cost_per_unit': cost, 'predicted_price': final_price,
            'predicted_price_per_unit': round(item['model_pu'], 2), 'porcentaje_de_ganancia': round(profit_margin, 2),
            'model_confidence': item['confidence'],
            'market_min_found': scale('low'),
            'market_max_found': scale('max'),
            'competitors_count': band['count'] if band else 0,
            'clamped_to_market': item['clamped'],
            'volume_outside_market': item['outside'],
            'market_median_found': scale('median'),
            'market_p75_found': scale('p75'),
            'market_match': band['match'] if band else '',
            'volume_capped': item['capped'],
            'has_size': item['kind'] is not None,
            'ceiling_source': band['ceiling_source'] if band else '',
        })

    return pd.DataFrame(report_list)

def main():
    """Orchestrates the complete prediction and report generation process."""
    logger.info("\n" + "="*60)
    logger.info("--- Starting Price Prediction Script ---")
    logger.info("="*60)
    
    artifacts = load_artifacts()
    model_vol, model_unit, vol_cols, unit_cols, vol_market, unit_market = artifacts
    
    df_to_predict = prepare_prediction_data()
    report_df = generate_predictions(df_to_predict, model_vol, model_unit, vol_cols, unit_cols, vol_market, unit_market)

    # Save and analyze final report
    output_filename = 'ml_model/wayakit_prediction_report.csv'
    report_df.to_csv(output_filename, index=False)
    logger.info(f"\n--- 4. Final report saved as '{output_filename}' ---")

    loss_products_count = (report_df['predicted_price'] <= report_df['cost_per_unit']).sum()
    logger.info("\n" + "-"*50)
    logger.info("Profitability Analysis:")
    logger.warning(f"Found {loss_products_count} products whose suggested selling price is LOWER or EQUAL to their cost.")
    logger.info("-" * 50)
    
    logger.info("\n🎉 Prediction process completed successfully.")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Price prediction failed.")
        sys.exit(1)