import argparse
import math
import re

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
# Size adjustment of the ceiling (Mafer, 2026-11-25: small bottles): only rows LARGER than the SKU
# are adjusted, up to the SKU's size, and a row counts as at most 4x larger, so the curve is never
# extrapolated far. Rows smaller than the SKU are taken as they are: adjusting them down too
# (Nov 25 data) cut 4-20 L SKUs with an unchanged market by up to 56 %; the stability brake bounds
# those instead.
SIZE_ADJUST_MAX_RATIO = 4.0
# Audit 2026-11-26 (Mafer): these subindustries' model-only prices (low_confidence) are held, not
# uploaded: Aviation has no market reference at all, and its model prices reached 5-20x cost.
HOLD_LOW_CONFIDENCE_SUBINDUSTRIES = ('Aviation',)
# A liquid size written in a product name, "[700 mL Jasmine]", "[4 L]".
_NAME_SIZE_RE = re.compile(r"\[\s*(\d+(?:[.,]\d+)?)\s*(ml|l)\b", re.I)
CAT_FEATURES = ['type_of_product', 'subindustry', 'channel', 'generic_product_type', 'industry', 'company']
MODEL_DIR = 'ml_model/trained_models'
# Saved by 2_train_models.py (ELASTICITY_FILE there): price per liter ~ size ** b.
ELASTICITY_FILE = 'vol_size_elasticity.joblib'
ELASTICITY_BOUNDS = (-1.0, -0.3)  # as clipped by 2_train_models.py (ELASTICITY_BOUNDS there)
# Stability brake (Mafer, 2026-11-25): with the same market as the previous run, the model may
# move a price at most this much against the previous run's price.
STABILITY_LIMIT = 0.15
MARKET_CHANGE_TOLERANCE = 0.005  # market_*_found within 0.5 % counts as unchanged
# The report of the last real upload to Odoo, copied there by odoo_api_price_suggestion.py
# (LAST_UPLOADED_REPORT there), relative to the log and state folder.
LAST_UPLOADED_REPORT = os.path.join('state', 'last_uploaded_prediction_report.csv')
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log_dir(environ=os.environ, project_dir: str = PROJECT_DIR) -> str:
    """The single log and state folder: PI_LOG_DIR if set and non-empty, else <project>/../logs
    (same rule as log_config.LOG_DIR and odoo_api_price_suggestion.log_dir)."""
    return os.path.abspath(environ.get('PI_LOG_DIR') or os.path.join(project_dir, '..', 'logs'))


def load_size_elasticity(model_dir=MODEL_DIR):
    """The price per liter vs size elasticities saved by 2_train_models.py, as {channel: b} with
    '' for the pooled one (a bare float, as first saved, is the pooled one), or None for artifacts
    trained before they existed (then the band keeps the all-sizes p75, as before)."""
    path = os.path.join(model_dir, ELASTICITY_FILE)
    if not os.path.exists(path):
        logger.warning(f"'{path}' not found (models trained before 2026-11-25): no size-adjusted ceiling.")
        return None
    saved = joblib.load(path)
    elasticities = {str(k): float(v) for k, v in saved.items()} if isinstance(saved, dict) else {'': float(saved)}
    if '' not in elasticities or not all(math.isfinite(b) for b in elasticities.values()):
        raise ValueError(f"'{path}' holds an invalid elasticity: {saved!r}")
    low, high = ELASTICITY_BOUNDS
    outside = {k or 'pooled': b for k, b in elasticities.items() if not low <= b <= high}
    if outside:
        # A b of 0 or more would adjust larger rows DOWN, so the ceiling could fall below the
        # band minimum; 2_train_models.py clips b to this range (ELASTICITY_BOUNDS there).
        raise ValueError(f"'{path}' holds an elasticity outside [{low}, {high}]: {outside!r}")
    logger.info("Price per liter vs size elasticity: "
                + ", ".join(f"{k or 'pooled'} {b:.3f}" for k, b in sorted(elasticities.items())))
    return elasticities


def elasticity_for(elasticities, channel):
    """The SKU's channel's elasticity, else the pooled one; None without elasticities."""
    if elasticities is None:
        return None
    if not isinstance(elasticities, dict):  # one pooled b
        return float(elasticities)
    return elasticities.get(str(channel).strip() if channel is not None else '', elasticities[''])


def load_artifacts():
    """Loads trained models, column lists and market rows from disk."""
    logger.info("--- 1. Loading trained models and columns ---")
    model_dir = MODEL_DIR

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

def market_band(rows, size, elasticity=None):
    """Market band of one SKU from the market rows of its type (price per unit and size columns).

    - `count`: rows of the type; `low`: their minimum price per unit; `max`: their maximum;
      `size_min` / `size_max`: their size range.
    - `median` / `p75`: from the rows of similar size (size / 2 to size x 2) when there are at
      least MIN_SIZE_MATCHES of them (`match` = 'size'). Otherwise from every row: as they are
      (`match` = 'all') without `elasticity`, or, with it (volumetric SKUs only; Mafer
      2026-11-25), from each row's price per unit adjusted to the SKU's size,
      price x (size / row size) ** elasticity, with the size ratio clipped to
      [1 / SIZE_ADJUST_MAX_RATIO, 1] (`match` = 'size_adjusted'): bulk cans must not set a small
      bottle's ceiling per liter. Rows smaller than the SKU are not adjusted. The adjusted median
      and p75 never exceed the highest price per unit of the rows (`max`; audit 2026-11-26, E3:
      35 adjusted ceilings were above any price per liter seen at any size).
    Returns None when the type has no market rows.
    """
    if rows is None or len(rows) == 0:
        return None
    prices = rows['price_per_unit'].to_numpy(dtype=float)
    sizes = rows['size'].to_numpy(dtype=float)
    similar = (sizes >= size / SIZE_MATCH_RATIO) & (sizes <= size * SIZE_MATCH_RATIO)
    if similar.sum() >= MIN_SIZE_MATCHES:
        match, used = 'size', prices[similar]
    elif elasticity is not None:
        ratio = np.clip(size / sizes, 1 / SIZE_ADJUST_MAX_RATIO, 1.0)  # larger rows only
        match, used = 'size_adjusted', prices * ratio ** elasticity
    else:
        match, used = 'all', prices
    return {
        'count': len(prices), 'low': float(prices.min()), 'max': float(prices.max()),
        'median': float(min(np.quantile(used, 0.5), prices.max())),
        'p75': float(min(np.quantile(used, CEILING_QUANTILE), prices.max())),
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


def generate_predictions(df_wayakit, model_vol, model_unit, vol_cols, unit_cols, vol_market, unit_market,
                         vol_elasticity=None, previous_report=None, limit=STABILITY_LIMIT):
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
       With `vol_elasticity` ({channel: b} or one b, saved by 2_train_models.py; None for older
       artifacts; `elasticity_for` picks the SKU's channel), a volumetric
       SKU with fewer than MIN_SIZE_MATCHES similar-size rows gets a size-adjusted median and p75
       (`market_band`, `market_match` 'size_adjusted'), also on the reference path. Only rows larger
       than the SKU are adjusted, and only upward, so the band never inverts.
    3. Stability brake (Mafer, 2026-11-25), with `previous_report` (the report of the last real
       upload; None = no brake). A SKU with a previous row (`_previous_rows`: Product_ID plus its
       occurrence) with a price, the same market (`_same_market`: competitors count, ceiling
       source and size; minimum and maximum within 0.5 %) and a price not set by the band in step 2
       (not clamped) gets its price per unit clipped to the previous price / size x
       (1 -/+ `limit`); then, with a band (MIN_COMPETITORS_FOR_CLAMP or more rows), to the same
       [minimum, p75] as step 2, so the brake never lifts a banded price above its p75 or leaves
       it below the minimum where the minimum applies.
    4. A SKU larger than every market row of its type (`volume_outside_market`) costs at most,
       per liter or per piece, what the smallest-priced smaller SKU of the same subindustry and
       type costs after steps 2 and 3 (`volume_capped`).
    5. x size, then the margin floor (`apply_margin_floor`), which always wins.
    6. Family rule (`family_floor`, audit 2026-11-26): a SKU never costs less in total than a
       smaller SKU of the same subindustry, type and kind; it is raised to that price
       (`family_raised`). Raising keeps the margin floor. Held SKUs take no part.

    `hold_reason` (`hold_reason()`) is '' for a price the upload may send, else why it is held:
    cost 0 or missing, a size in the name that disagrees with Volume_Liters, or a model-only price
    in HOLD_LOW_CONFIDENCE_SUBINDUSTRIES.

    `previous_price` is the previous row's `predicted_price` (NaN without one) and
    `stability_limited` is True when the brake changed the price per unit (steps 4 and 5 may still
    change the final price). `predicted_price_per_unit` stays the model's own price per unit.

    A product without a size (no-size parent templates) gets the floor and `has_size` False; the
    upload skips it. `low_confidence` is True when the price comes from the model alone (fewer
    than MIN_COMPETITORS_FOR_CLAMP market rows, so no band); it is for the review, not uploaded.
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
                'clamped': False, 'outside': False, 'capped': False, 'lower': 0.0}
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
        elasticity = elasticity_for(vol_elasticity, row.get('channel')) if kind == 'volume' else None
        band = market_band(rows, item['size'], elasticity)
        if band is not None:
            band['ceiling_source'] = 'all' if band['count'] >= MIN_COMPETITORS_FOR_CLAMP else ''
            if band['ceiling_source'] and row.get('channel') == 'B2C' and 'is_reference' in rows:
                reference = rows[rows['is_reference'].fillna(False).astype(bool)]
                if len(reference) >= MIN_REFERENCE_ROWS:
                    reference_band = market_band(reference, item['size'], elasticity)
                    # Never trade a similar-size ceiling for an all-sizes one: shelf bottles of
                    # another size would set the wrong price per liter (small packs cost more).
                    if reference_band['match'] == 'size' or band['match'] != 'size':
                        band.update({key: reference_band[key] for key in ('median', 'p75', 'match')})
                        band['ceiling_source'] = 'reference'
        item['band'] = band
        if band is None:
            continue
        below, above = item['size'] < band['size_min'], item['size'] > band['size_max']
        item['outside'] = below or above
        # Volume: a size above the market is cheaper per liter, so no minimum. Pieces: always.
        item['lower'] = lower = 0.0 if above and item['kind'] == 'volume' else band['low']
        if band['count'] >= MIN_COMPETITORS_FOR_CLAMP and item['pu'] > 0:
            clamped = min(max(item['pu'], lower), band['p75'])
            item['clamped'] = clamped != item['pu']
            item['pu'] = clamped

    # Stability brake, before the sibling cap so the cap sees the braked prices.
    for item, previous in zip(items, _previous_rows([item['row']['Product_ID'] for item in items], previous_report)):
        item['previous_price'], item['limited'] = np.nan, False
        if previous is None:
            continue
        last = item['previous_price'] = _number(previous.get('predicted_price'))
        # A price held from the upload never reached Odoo: nothing to keep stable.
        if (item['kind'] is None or not last > 0 or item['clamped'] or _text(previous.get('hold_reason'))
                or not _same_market(_market_columns(item), previous)):
            continue
        braked = min(max(item['pu'], last / item['size'] * (1 - limit)), last / item['size'] * (1 + limit))
        band = item['band']
        if band is not None and band['count'] >= MIN_COMPETITORS_FOR_CLAMP:
            braked = min(max(braked, item['lower']), band['p75'])
        if braked != item['pu']:
            item['pu'], item['limited'] = braked, True

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

    finals = [apply_margin_floor(item['pu'] * item['size'], item['row']['Unit_cost_SAR']) for item in items]
    lows = [_market_columns(item)['competitors_count'] < MIN_COMPETITORS_FOR_CLAMP for item in items]
    holds = [hold_reason(item['row'], item['kind'] is not None, low) for item, low in zip(items, lows)]
    raised = family_floor(items, finals, eligible=[not reason for reason in holds])

    report_list = []
    for item, final_price, family_raised, low_confidence, held in zip(items, finals, raised, lows, holds):
        row, band, size = item['row'], item['band'], item['size']
        cost = row['Unit_cost_SAR']
        profit_margin = ((final_price - cost) / cost) * 100 if cost > 0 else 0
        market = _market_columns(item)

        report_list.append({
            'Product_ID': row['Product_ID'], 'product_name': row['Product_Name'],
            'product_type': row.get('type_of_product', 'N/A'), 'generic_product_type': row.get('generic_product_type', 'N/A'),
            'subindustry': row.get('subindustry', 'N/A'), 'industry': row.get('industry', 'N/A'),
            'volume': market['volume'],
            'cost_per_unit': cost, 'predicted_price': final_price,
            'predicted_price_per_unit': round(item['model_pu'], 2), 'porcentaje_de_ganancia': round(profit_margin, 2),
            'model_confidence': item['confidence'],
            'market_min_found': market['market_min_found'],
            'market_max_found': market['market_max_found'],
            'competitors_count': market['competitors_count'],
            'clamped_to_market': item['clamped'],
            'volume_outside_market': item['outside'],
            'market_median_found': round(band['median'] * size, 2) if band else 0.0,
            'market_p75_found': round(band['p75'] * size, 2) if band else 0.0,
            'market_match': band['match'] if band else '',
            'volume_capped': item['capped'],
            'has_size': item['kind'] is not None,
            'ceiling_source': market['ceiling_source'],
            'low_confidence': low_confidence,
            'previous_price': item['previous_price'],
            'stability_limited': item['limited'],
            'family_raised': family_raised,
            'hold_reason': held,
        })

    return pd.DataFrame(report_list)


def family_floor(items, finals, eligible=None):
    """Raises `finals` in place so that no SKU costs less in total than a strictly smaller SKU of
    the same subindustry, type of product and kind (volume or pieces); returns, per item, True
    where it raised the price. Items without a size, and those not `eligible` (held from the
    upload: their size or cost may be wrong), are left alone and never set another's price. The new price is a smaller
    SKU's final price, which already holds its own margin floor, and it is only ever raised, so the
    item's floor holds too."""
    raised = [False] * len(items)
    eligible = [True] * len(items) if eligible is None else eligible
    families = {}
    for index, item in enumerate(items):
        if item['kind'] is None or not finals[index] > 0 or not eligible[index]:
            continue
        row = item['row']
        families.setdefault((row.get('subindustry'), row.get('type_of_product'), item['kind']), []).append(index)
    for members in families.values():
        members.sort(key=lambda index: items[index]['size'])
        dearest_smaller, position = 0.0, 0
        while position < len(members):
            size = items[members[position]]['size']
            same_size = [index for index in members[position:] if items[index]['size'] == size]
            for index in same_size:
                if finals[index] < dearest_smaller:
                    finals[index], raised[index] = dearest_smaller, True
            dearest_smaller = max([dearest_smaller] + [finals[index] for index in same_size])
            position += len(same_size)
    return raised


def name_size_liters(name):
    """The liquid size written in brackets in a product name ("[700 mL Jasmine]" -> 0.7), or None."""
    match = _NAME_SIZE_RE.search(_text(name))
    if not match:
        return None
    value = float(match.group(1).replace(',', '.'))
    return value / 1000 if match.group(2).lower() == 'ml' else value


def hold_reason(row, has_size, low_confidence):
    """'' when the upload may send this SKU's price, else why it is held (audit 2026-11-26, D3 and
    E2): Odoo master data to fix first, or a model-only price in HOLD_LOW_CONFIDENCE_SUBINDUSTRIES.
    Products without a size are skipped by the upload anyway (no reason here)."""
    if not has_size:
        return ''
    reasons = []
    cost = _number(row.get('Unit_cost_SAR'))
    if not cost > 0:
        reasons.append('cost is 0 or missing in Odoo')
    named = name_size_liters(row.get('Product_Name'))
    volume = _number(row.get('Volume_Liters'))
    if named is not None and not (volume > 0 and abs(named - volume) <= 0.01 * volume):
        reasons.append(f'the name says {named:g} L but Volume_Liters is {volume:g}')
    if low_confidence and _text(row.get('subindustry')) in HOLD_LOW_CONFIDENCE_SUBINDUSTRIES:
        reasons.append(f"model-only price in {_text(row.get('subindustry'))} (held for review)")
    return '; '.join(reasons)


def _market_columns(item):
    """The report's market columns of one SKU, as the stability brake compares them."""
    band, size, row = item['band'], item['size'], item['row']
    return {
        'volume': f"{row['Volume_Liters']} L" if item['kind'] == 'volume' else f"{int(size)} units",
        'competitors_count': band['count'] if band else 0,
        'ceiling_source': band['ceiling_source'] if band else '',
        'market_min_found': round(band['low'] * size, 2) if band else 0.0,
        'market_max_found': round(band['max'] * size, 2) if band else 0.0,
    }


def _number(value):
    """A float, NaN for a missing or non-numeric value (as read back from a CSV)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def _text(value):
    """Stripped text, '' for a missing value (an empty CSV cell reads back as NaN)."""
    return '' if pd.isna(value) else str(value).strip()


def _same_value(new, old, tolerance=MARKET_CHANGE_TOLERANCE):
    """Both NaN, or within `tolerance` relative to the larger absolute value."""
    new, old = _number(new), _number(old)
    if math.isnan(new) or math.isnan(old):
        return math.isnan(new) and math.isnan(old)
    return abs(new - old) <= tolerance * max(abs(new), abs(old))


def _same_market(current, previous):
    """The stability brake's "same market": the same competitors count, ceiling source and size
    (`volume`, when the previous report has it; older reports do not); the minimum and the
    maximum within MARKET_CHANGE_TOLERANCE or both missing. Not the p75: a size-adjusted p75 moves
    with the elasticity, which is measured again every run, even with the same competitor rows.
    `current` is `_market_columns(item)`; `previous` a previous report row (a dict)."""
    same = (_number(current['competitors_count']) == _number(previous.get('competitors_count'))
            and _text(current['ceiling_source']) == _text(previous.get('ceiling_source'))
            and _same_value(current['market_min_found'], previous.get('market_min_found'))
            and _same_value(current['market_max_found'], previous.get('market_max_found')))
    if 'volume' in previous:  # the same SKU size (repeated Product_IDs)
        same = same and _text(current['volume']) == _text(previous['volume'])
    return same


def _previous_rows(product_ids, previous_report):
    """For each product, in order, its previous report row as a dict, or None. The n-th row of a
    Product_ID meets the n-th previous row of that Product_ID. Pure."""
    if previous_report is None or previous_report.empty:
        return [None] * len(product_ids)
    for column in ('Product_ID', 'predicted_price'):
        if column not in previous_report:
            raise ValueError(f"The previous report has no '{column}' column.")
    by_key = {}
    for previous in previous_report.to_dict('records'):
        by_key.setdefault(str(previous['Product_ID']), []).append(previous)
    seen, matched = {}, []
    for product in map(str, product_ids):
        occurrence = seen[product] = seen.get(product, -1) + 1
        candidates = by_key.get(product, [])
        matched.append(candidates[occurrence] if occurrence < len(candidates) else None)
    return matched


def load_previous_report(path):
    """The previous run's report, or None (logged) when the file does not exist."""
    if not os.path.exists(path):
        logger.warning(f"No previous report at '{path}': the stability brake does not apply this run.")
        return None
    previous = pd.read_csv(path)
    logger.info(f"Previous report for the stability brake: '{path}' ({len(previous)} rows).")
    return previous


def parse_args(argv=None):
    default = os.path.join(log_dir(), LAST_UPLOADED_REPORT)
    parser = argparse.ArgumentParser(description="Generates the price suggestion report.")
    parser.add_argument('--previous_report', default=default,
                        help="Report of the previous run for the stability brake (default: the report "
                             f"of the last real upload to Odoo, {default}). A missing file: no brake.")
    return parser.parse_args(argv)


def main(argv=None):
    """Orchestrates the complete prediction and report generation process."""
    args = parse_args(argv)
    logger.info("\n" + "="*60)
    logger.info("--- Starting Price Prediction Script ---")
    logger.info("="*60)

    artifacts = load_artifacts()
    model_vol, model_unit, vol_cols, unit_cols, vol_market, unit_market = artifacts
    vol_elasticity = load_size_elasticity()

    df_to_predict = prepare_prediction_data()
    report_df = generate_predictions(df_to_predict, model_vol, model_unit, vol_cols, unit_cols, vol_market, unit_market,
                                     vol_elasticity=vol_elasticity,
                                     previous_report=load_previous_report(args.previous_report))
    logger.info(f"Size-adjusted ceilings: {int((report_df['market_match'] == 'size_adjusted').sum())} SKUs. "
                f"Low confidence (model only, fewer than {MIN_COMPETITORS_FOR_CLAMP} competitor rows): "
                f"{int(report_df['low_confidence'].sum())} of {len(report_df)}. "
                f"Stability-limited (same market, model moved more than {STABILITY_LIMIT:.0%}): "
                f"{int(report_df['stability_limited'].sum())}. "
                f"Raised to a smaller size's price (family rule): {int(report_df['family_raised'].sum())}. "
                f"Held from the upload: {int((report_df['hold_reason'] != '').sum())}.")

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