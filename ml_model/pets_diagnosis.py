"""F5 Pets diagnosis: why the suggested prices of one subindustry (Pets by default) are high.

What it does (read-only; it never writes inside ml_model/ or scraper/, reads no secrets and
never touches Odoo or the network):

1. Raw competitor rows. Calls ``load_and_clean_raw_data()`` from ``ml_model/1a_preprocess_data.py``
   (loaded with importlib, run with the data folder as the working directory) so the rows,
   price cleaning and unit lower-casing are exactly the pipeline's. The scraper's ``product``
   title is aligned back by position (1a concatenates the scraper rows first, then the Odoo
   ``competitor.product`` rows). Each raw row is assigned to a model with 1a's rule: a volume or
   mass unit (``ml``, ``l`` and its spellings, ``fl oz``, ``g``, ``kg``...) -> volumetric (1a then
   drops the mass rows), anything else -> units.
2. Processed competitor rows (``competitor_volumetric_processed.csv`` /
   ``competitor_unit_processed.csv``): price per liter / per piece as training reads it.
   ``n_raw - n_before_trim`` shows the rows 1a dropped (for example ``g`` rows, missing prices).
3. Trimming. Calls ``filter_outliers_with_percentiles()`` from ``ml_model/2_train_models.py``
   on the full processed files (not only the subindustry), after ``drop_implausible_sizes()``
   for the volumetric rows (F7a), so "after trimming" is exactly what training saw (the market
   rows, ``*_market_rows.joblib``, are those rows minus multipacks). A type with rows before and
   none after was emptied by the p10-p90 trimming (finding 12, bug 6; fixed in F7a).
4. Wayakit products of the subindustry (``wayakit_products_to_predict_odoo.csv``) joined with
   the production prediction report (``wayakit_prediction_report.csv``, what the run produced;
   local re-runs differ by float noise, finding 13) and the approved quotes
   (``wayakit_cotizations.csv``).

Definitions (also written into pets_report.md):

- ``pct_fl_oz``: share of the raw volumetric rows whose unit is literally ``fl oz``.
- ``pct_us_size_ml``: share of the raw volumetric rows stated in mL or L whose size is a common
  US bottle size converted to mL (4, 8, 10, 12, 16, 20, 24, 32, 64 or 128 fl oz, within
  max(1.5 mL, 0.3 % of the US size); e.g. 473 mL = 16 fl oz, 3780 mL = 128 fl oz). A heuristic hint of US-sourced products when the unit was already
  converted (Odoo ``competitor.product`` only stores mL, g or units).
- ``pct_multipack``: share of the raw rows *with a product title* whose title has a pack marker
  ("pack of N", "N-pack", "N x <size>", "N bottles", "set of N", "bundle", "twin pack"). Only
  scraper rows have titles; Odoo rows have none, so they are left out of the denominator
  (``n_with_title``) and the value is empty when no row of the type has a title. For the units
  model ``total_quantity`` is the piece count (wipes), not the number of packs, so it is not
  used as a multipack signal.
- ``n_price_zero``: raw rows whose cleaned price is exactly 0 (1a keeps them: price per liter 0).
  ``n_price_missing``: raw rows whose price could not be parsed (1a drops them).
- ``n_le_5ml``: raw volumetric rows whose size, converted to mL, is <= 5 (non-positive included;
  ``g`` rows are not volumes and are not counted). ``n_le_5ml_after_trim`` counts the same rule
  on the trimmed processed rows.
- Price statistics (``median`` / ``p75`` / ``max`` / ``min``) are SAR per liter (volumetric) or
  SAR per piece (units), on the processed rows before trimming and, with the ``_after_trim``
  suffix, after trimming (``max_after_trim`` = ``market_max`` used to clamp predictions).
- Wayakit product ``volume_position``: where its size sits versus the competitor size range of
  its type after trimming (the same ``vol_min``/``vol_max`` production used for
  ``volume_outside_market``): ``below_min``, ``inside``, ``above_max`` or ``no_competitors``.
- ``median_quote``: median ``approved_quote_price`` per Product_ID. It is a proxy for the
  current selling price; the live price in Odoo KSA is not in the data and is not read.
- ``floor_binding``: the production price equals ``round(cost x 1.30, 2)`` (within 0.011 SAR),
  i.e. the margin floor, not the model or the market clamp, set the price.

Outputs in ``--out-dir`` (local, under the git-ignored ``data/`` by default; they may contain
costs): ``pets_by_type.csv``, ``pets_wayakit_products.csv``, ``pets_report.md``.

How to run it (from the repo root, conda environment ``wayakit-price-prediction``)::

    python ml_model/pets_diagnosis.py --data-dir data/f1_before_2026-11-15   # F1 snapshot
    python ml_model/pets_diagnosis.py                                        # current repo data
    python ml_model/pets_diagnosis.py --subindustry Home --out-dir data/f5_home

``--data-dir`` is a folder with ``ml_model/`` and ``scraper/`` subfolders laid out like the repo
(default ``.``), so F7 can rerun the same diagnosis before and after its changes. Exit code 1
with a message on stderr if an input is missing or the subindustry has no competitor rows.
"""
import argparse
import contextlib
import importlib.util
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CODE_DIR = Path(__file__).resolve().parent  # ml_model/ of the code (not of --data-dir)
REPO_ROOT = CODE_DIR.parent

# Same rule as ml_model/1a_preprocess_data.py main() (LITERS_PER_UNIT | MASS_UNITS since F7a):
# these units go to the volumetric step, which then drops the mass rows.
VOLUMETRIC_UNITS = ['ml', 'l', 'litre', 'litres', 'liter', 'liters', 'fl oz',
                    'g', 'gr', 'gram', 'grams', 'kg', 'kgs', 'kilogram', 'kilograms', 'mg']
FL_OZ_ML = 29.5735
LITER_SPELLINGS = ('litre', 'litres', 'liter', 'liters')
# 2 fl oz (59.1 mL) and 22 fl oz (650.6 mL) are left out: they collide with the metric 60 mL and
# 650 mL bottles (e.g. Dr. Beckmann 650 mL).
US_SIZES_FL_OZ = [4, 8, 10, 12, 16, 20, 24, 32, 64, 128]
# Relative tolerance: labels round large bottles (1 gal = 3785.4 mL is sold as 3.78 L or 3788 mL,
# 64 fl oz = 1892.7 mL as 1.89 L), so an absolute 1.5 mL can never match them.
US_SIZE_TOLERANCE_ML = 1.5
US_SIZE_TOLERANCE_REL = 0.003
SMALL_ML = 5.0
MARGIN_FLOOR = 1.30
PACK_MARKERS = re.compile(
    r"pack\s*of\s*\d+|\b\d+\s*-?\s*packs?\b|\b\d+\s*[x×]\s*\d|\b\d+\s*bottles\b|"
    r"set\s*of\s*\d+|\bbundle\b|twin\s*pack",
    re.IGNORECASE,
)

INPUTS = {
    'scraper_raw': 'scraper/competitors_complete.csv',
    'odoo_raw': 'ml_model/competitor_products_from_odoo.csv',
    'vol_processed': 'ml_model/competitor_volumetric_processed.csv',
    'unit_processed': 'ml_model/competitor_unit_processed.csv',
    'to_predict': 'ml_model/wayakit_products_to_predict_odoo.csv',
    'report': 'ml_model/wayakit_prediction_report.csv',
    'quotes': 'ml_model/wayakit_cotizations.csv',
}
METRIC = {'volumetric': 'price_per_liter', 'units': 'price_per_item'}
SIZE = {'volumetric': 'volume_liters', 'units': 'total_quantity'}


class DiagnosisError(Exception):
    """An input problem that ends the run with exit code 1 and a clear message."""


# --- Loading --------------------------------------------------------------------------------
def load_pipeline_module(file_name):
    """Imports a pipeline script from ml_model/ by file name (names start with a digit)."""
    path = CODE_DIR / file_name
    spec = importlib.util.spec_from_file_location('pets_diag_' + path.stem.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _working_directory(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def check_inputs(data_dir):
    missing = [rel for rel in INPUTS.values() if not (Path(data_dir) / rel).is_file()]
    if missing:
        raise DiagnosisError(f"Missing input files under {data_dir}: {missing}")


def read_csv(path):
    df = pd.read_csv(path, encoding='utf-8-sig')
    df.columns = df.columns.str.strip()
    return df


def load_raw_competitors(data_dir, preprocess_module):
    """Raw competitor rows exactly as 1a builds them, plus ``product`` titles and ``origin_file``."""
    with _working_directory(data_dir):
        raw = preprocess_module.load_and_clean_raw_data()
    scraper = read_csv(Path(data_dir) / INPUTS['scraper_raw'])
    n_scraper = len(scraper)
    raw = raw.reset_index(drop=True)
    titles = scraper['product'].tolist() if 'product' in scraper else [None] * n_scraper
    raw['product'] = titles + [None] * (len(raw) - n_scraper)
    raw['origin_file'] = ['scraper'] * n_scraper + ['odoo'] * (len(raw) - n_scraper)
    return raw


def normalize_labels(df, columns=('subindustry', 'type_of_product', 'company')):
    df = df.copy()
    for column in columns:
        if column in df:
            df[column] = df[column].astype('string').str.strip()
    return df


def in_subindustry(df, subindustry, column='subindustry'):
    return df[column].astype('string').str.strip().str.casefold() == subindustry.strip().casefold()


# --- Per-row facts --------------------------------------------------------------------------
def model_of_unit(unit):
    unit = '' if pd.isna(unit) else str(unit).strip().lower()
    return 'volumetric' if unit in VOLUMETRIC_UNITS else 'units'


def size_in_ml(unit, quantity):
    """Size in mL for volume units; NaN for ``g``, pieces or an unparseable quantity."""
    quantity = pd.to_numeric(quantity, errors='coerce')
    unit = '' if pd.isna(unit) else str(unit).strip().lower()
    if pd.isna(quantity):
        return np.nan
    unit = 'l' if unit in LITER_SPELLINGS else unit  # 1a reads these as liters since F7a
    return {'ml': quantity, 'l': quantity * 1000, 'fl oz': quantity * FL_OZ_ML}.get(unit, np.nan)


def is_us_size_ml(ml):
    if pd.isna(ml) or ml <= 0:
        return False
    return any(abs(ml - n * FL_OZ_ML) <= max(US_SIZE_TOLERANCE_ML, US_SIZE_TOLERANCE_REL * n * FL_OZ_ML)
               for n in US_SIZES_FL_OZ)


def has_pack_marker(title):
    return False if title is None or pd.isna(title) else bool(PACK_MARKERS.search(str(title)))


def add_raw_facts(raw):
    raw = normalize_labels(raw)
    raw['model'] = raw['unit_of_measurement'].map(model_of_unit)
    raw['size_ml'] = [size_in_ml(u, q) for u, q in zip(raw['unit_of_measurement'], raw['total_quantity'])]
    unit = raw['unit_of_measurement'].astype('string').str.strip().str.lower()
    raw['is_fl_oz'] = unit.eq('fl oz').fillna(False)
    raw['is_us_size_ml'] = unit.isin(['ml', 'l', *LITER_SPELLINGS]).fillna(False) & raw['size_ml'].map(is_us_size_ml)
    raw['has_title'] = raw['product'].map(lambda t: t is not None and not pd.isna(t) and str(t).strip() != '')
    raw['is_multipack'] = raw['product'].map(has_pack_marker)
    raw['price_zero'] = raw['price_sar'].eq(0).fillna(False)
    raw['price_missing'] = raw['price_sar'].isna()
    raw['le_5ml'] = (raw['model'] == 'volumetric') & (raw['size_ml'] <= SMALL_ML)
    return raw


# --- Per-type table -------------------------------------------------------------------------
def _pct(numerator, denominator):
    return round(100.0 * numerator / denominator, 1) if denominator else None


def _stat(series, how, digits=2):
    series = pd.to_numeric(series, errors='coerce').dropna()
    if series.empty:
        return None
    value = float({'median': series.median(), 'p75': series.quantile(0.75), 'max': series.max(), 'min': series.min()}[how])
    return value if digits is None else round(value, digits)


def _brand_count(df):
    return int(df['company'].dropna().str.casefold().nunique()) if len(df) else 0


def trim_processed(vol_processed, unit_processed, trim_fn, drop_sizes_fn=None):
    """Applies the pipeline's trimming to the full processed files (as 2_train_models main():
    since F7a the volumetric rows of 5 mL or less are dropped first, with `drop_sizes_fn`)."""
    volumetric = vol_processed.copy()
    if drop_sizes_fn is not None:
        volumetric = drop_sizes_fn(volumetric)
    return {
        'volumetric': trim_fn(volumetric, METRIC['volumetric']),
        'units': trim_fn(unit_processed.copy(), METRIC['units']),
    }


def build_by_type(raw, processed, trimmed, wayakit, subindustry):
    """One row per (model, type_of_product) of the subindustry, competitors and Wayakit SKUs."""
    raw_s = raw[in_subindustry(raw, subindustry)]
    keys = set(zip(raw_s['model'], raw_s['type_of_product']))
    for model in ('volumetric', 'units'):
        proc = processed[model]
        keys |= {(model, t) for t in proc.loc[in_subindustry(proc, subindustry), 'type_of_product']}
    keys |= set(zip(wayakit['model'], wayakit['type_of_product']))
    keys = {(m, t) for m, t in keys if m in METRIC and not pd.isna(t)}

    rows = []
    for model, product_type in sorted(keys, key=lambda k: (k[0] != 'volumetric', str(k[1]))):
        metric, size = METRIC[model], SIZE[model]
        r = raw_s[(raw_s['model'] == model) & (raw_s['type_of_product'] == product_type)]
        before = processed[model][processed[model]['type_of_product'] == product_type]
        after = trimmed[model][trimmed[model]['type_of_product'] == product_type]
        w = wayakit[(wayakit['model'] == model) & (wayakit['type_of_product'] == product_type)]
        n_titled = int(r['has_title'].sum())
        n_vol_raw = int((r['model'] == 'volumetric').sum())
        n_metric_vol = int(r['unit_of_measurement'].astype('string').str.lower().isin(['ml', 'l']).sum())
        rows.append({
            'type_of_product': product_type,
            'model': model,
            'n_raw': len(r),
            'n_raw_scraper': int((r['origin_file'] == 'scraper').sum()),
            'n_raw_odoo': int((r['origin_file'] == 'odoo').sum()),
            'n_before_trim': len(before),
            'n_dropped_by_1a': len(r) - len(before),
            'n_after_trim': len(after),
            'emptied_by_trim': bool(len(before) > 0 and len(after) == 0),
            'n_distinct_prices': int(before[metric].nunique()),
            'min_per_unit': _stat(before[metric], 'min'),
            'median_per_unit': _stat(before[metric], 'median'),
            'p75_per_unit': _stat(before[metric], 'p75'),
            'max_per_unit': _stat(before[metric], 'max'),
            'min_after_trim': _stat(after[metric], 'min'),
            'median_after_trim': _stat(after[metric], 'median'),
            'p75_after_trim': _stat(after[metric], 'p75'),
            'max_after_trim': _stat(after[metric], 'max'),
            # Not rounded: compared with the Wayakit sizes (0.7 L vs 0.71 L must stay exact).
            'size_min_after_trim': _stat(after[size], 'min', digits=None),
            'size_max_after_trim': _stat(after[size], 'max', digits=None),
            'pct_fl_oz': _pct(int(r['is_fl_oz'].sum()), n_vol_raw) if model == 'volumetric' else None,
            'pct_us_size_ml': _pct(int(r['is_us_size_ml'].sum()), n_metric_vol) if model == 'volumetric' else None,
            'n_brands': _brand_count(before),
            'n_brands_after_trim': _brand_count(after),
            'n_with_title': n_titled,
            'n_multipack': int(r['is_multipack'].sum()),
            'pct_multipack': _pct(int(r['is_multipack'].sum()), n_titled),
            'n_price_zero': int(r['price_zero'].sum()),
            'n_price_missing': int(r['price_missing'].sum()),
            'n_le_5ml': int(r['le_5ml'].sum()) if model == 'volumetric' else None,
            'n_le_5ml_after_trim': int((after['volume_liters'] * 1000 <= SMALL_ML).sum()) if model == 'volumetric' else None,
            'n_wayakit_skus': len(w),
            'wayakit_median_pred_per_unit': _stat(w['pred_price_per_unit'], 'median') if len(w) else None,
        })
    return pd.DataFrame(rows)


# --- Wayakit products -----------------------------------------------------------------------
def prepare_wayakit(to_predict, report, quotes, subindustry):
    """Wayakit SKUs of the subindustry joined with the production report and the quotes."""
    products = to_predict.rename(columns={'SubIndustry': 'subindustry', 'Type_of_product': 'type_of_product'})
    products = normalize_labels(products, ('subindustry', 'type_of_product'))
    products['Product_ID'] = products['Product_ID'].astype('string').str.strip()
    products = products[in_subindustry(products, subindustry)].copy()

    report = report.copy()
    report['Product_ID'] = report['Product_ID'].astype('string').str.strip()
    report_cols = ['Product_ID', 'cost_per_unit', 'predicted_price', 'predicted_price_per_unit',
                   'market_min_found', 'market_max_found', 'competitors_count', 'clamped_to_market',
                   'volume_outside_market']
    # Repeated SKUs (finding 12, bug 7) repeat identical report rows: keep the first one.
    report = report[report_cols].drop_duplicates('Product_ID')
    df = products.merge(report, on='Product_ID', how='left', indicator='in_report')
    df['in_report'] = df['in_report'].eq('both')

    quotes = quotes.copy()
    quotes['Product_ID'] = quotes['Product_ID'].astype('string').str.strip()
    quotes['approved_quote_price'] = pd.to_numeric(quotes['approved_quote_price'], errors='coerce')
    q = quotes.groupby('Product_ID')['approved_quote_price'].agg(median_quote='median', n_quotes='count').reset_index()
    df = df.merge(q, on='Product_ID', how='left')
    df['n_quotes'] = df['n_quotes'].fillna(0).astype(int)

    volume = pd.to_numeric(df['Volume_Liters'], errors='coerce')
    pieces = pd.to_numeric(df['Pack_quantity_Units'], errors='coerce')
    # Same rule as 3_predicted_prices.py: volumetric if Volume_Liters > 0, else units if pieces > 0.
    df['model'] = np.select([volume > 0, pieces > 0], ['volumetric', 'units'], default='no_size')
    df['size'] = np.where(df['model'] == 'volumetric', volume, np.where(df['model'] == 'units', pieces, np.nan))
    df['cost'] = pd.to_numeric(df['cost_per_unit'], errors='coerce').fillna(pd.to_numeric(df['Unit_cost_SAR'], errors='coerce'))
    df['pred_price_per_unit'] = df['predicted_price'] / df['size']
    return df


def build_wayakit_table(wayakit, by_type):
    stats = by_type.set_index(['model', 'type_of_product'])
    rows = []
    for _, p in wayakit.iterrows():
        key = (p['model'], p['type_of_product'])
        s = stats.loc[key] if key in stats.index else None
        lo = s['size_min_after_trim'] if s is not None else None
        hi = s['size_max_after_trim'] if s is not None else None
        if p['model'] == 'no_size':
            position = 'no_size'
        elif lo is None or pd.isna(lo):
            position = 'no_competitors'
        elif p['size'] < lo:
            position = 'below_min'
        elif p['size'] > hi:
            position = 'above_max'
        else:
            position = 'inside'
        cost, price, quote = p['cost'], p['predicted_price'], p['median_quote']
        floor = round(cost * MARGIN_FLOOR, 2) if pd.notna(cost) else np.nan
        comp_median = s['median_after_trim'] if s is not None else None
        model_ppu, final_ppu = p['predicted_price_per_unit'], p['pred_price_per_unit']
        clamped = pd.notna(p['clamped_to_market']) and bool(p['clamped_to_market'])
        if not clamped:
            clamp_direction = 'none'
        else:
            clamp_direction = 'up' if final_ppu > model_ppu else 'down'
        rows.append({
            'Product_ID': p['Product_ID'],
            'product_name': p.get('Product_Name'),
            'type_of_product': p['type_of_product'],
            'model': p['model'],
            'size': p['size'],
            'size_unit': {'volumetric': 'L', 'units': 'pieces'}.get(p['model']),
            'comp_size_min_after_trim': lo,
            'comp_size_max_after_trim': hi,
            'volume_position': position,
            'competitors_count': p['competitors_count'],
            'competitors_count_reproduced': int(s['n_after_trim']) if s is not None else 0,
            'clamped_to_market': p['clamped_to_market'],
            'clamp_direction': clamp_direction,
            'volume_outside_market': p['volume_outside_market'],
            'cost': cost,
            'margin_floor_price': floor,
            'model_price_per_unit': p['predicted_price_per_unit'],
            'predicted_price': price,
            'predicted_price_per_unit': round(p['pred_price_per_unit'], 2) if pd.notna(p['pred_price_per_unit']) else None,
            'comp_median_per_unit_after_trim': comp_median,
            'pred_vs_comp_median': round(p['pred_price_per_unit'] / comp_median, 2)
            if comp_median and pd.notna(p['pred_price_per_unit']) else None,
            'predicted_over_cost': round(price / cost, 2) if pd.notna(price) and cost else None,
            'median_quote': quote,
            'n_quotes': p['n_quotes'],
            'predicted_over_quote': round(price / quote, 2) if pd.notna(price) and pd.notna(quote) and quote else None,
            'floor_binding': bool(pd.notna(price) and pd.notna(floor) and abs(price - floor) <= 0.011),
            'in_report': p['in_report'],
        })
    return pd.DataFrame(rows)


# --- Report ---------------------------------------------------------------------------------
def _md_table(df, columns):
    def fmt(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return ''
        return f"{v:g}" if isinstance(v, float) else str(v)
    lines = ['| ' + ' | '.join(columns) + ' |', '|' + '---|' * len(columns)]
    lines += ['| ' + ' | '.join(fmt(row[c]) for c in columns) + ' |' for _, row in df.iterrows()]
    return '\n'.join(lines)


def summarize(by_type, products, raw_s):
    """Totals for the subindustry (printed and written to the report)."""
    vol = by_type[by_type['model'] == 'volumetric']
    unit = by_type[by_type['model'] == 'units']
    sized = products[products['model'] != 'no_size']
    return {
        'raw_rows': len(raw_s),
        'raw_rows_scraper': int((raw_s['origin_file'] == 'scraper').sum()),
        'raw_rows_odoo': int((raw_s['origin_file'] == 'odoo').sum()),
        'volumetric_types_with_competitors': int((vol['n_before_trim'] > 0).sum()),
        'volumetric_types_emptied': int(vol['emptied_by_trim'].sum()),
        'unit_types_with_competitors': int((unit['n_before_trim'] > 0).sum()),
        'unit_types_emptied': int(unit['emptied_by_trim'].sum()),
        'n_before_trim': int(by_type['n_before_trim'].sum()),
        'n_after_trim': int(by_type['n_after_trim'].sum()),
        'n_price_zero': int(by_type['n_price_zero'].sum()),
        'n_le_5ml': int(by_type['n_le_5ml'].fillna(0).sum()),
        'n_fl_oz_rows': int(raw_s['is_fl_oz'].sum()),
        'n_us_size_ml_rows': int(raw_s['is_us_size_ml'].sum()),
        'wayakit_skus': len(products),
        'wayakit_no_size': int((products['model'] == 'no_size').sum()),
        'floor_binding': int(products['floor_binding'].sum()),
        'clamped_to_market': int(products['clamped_to_market'].fillna(False).astype(bool).sum()),
        'clamped_up': int((products['clamp_direction'] == 'up').sum()),
        'clamped_down': int((products['clamp_direction'] == 'down').sum()),
        'median_pred_over_cost_sized': _stat(sized['predicted_over_cost'], 'median'),
        'median_pred_over_cost_all': _stat(products['predicted_over_cost'], 'median'),
        'median_pred_over_quote': _stat(products['predicted_over_quote'], 'median'),
        'count_mismatch': int((products['competitors_count'].fillna(-1) != products['competitors_count_reproduced'])
                              [products['model'] != 'no_size'].sum()),
    }


def write_report(path, args, by_type, products, totals):
    type_cols = ['type_of_product', 'model', 'n_raw', 'n_before_trim', 'n_after_trim', 'emptied_by_trim',
                 'median_per_unit', 'p75_per_unit', 'max_per_unit', 'max_after_trim', 'pct_fl_oz',
                 'pct_us_size_ml', 'n_brands', 'pct_multipack', 'n_price_zero', 'n_le_5ml', 'n_wayakit_skus']
    product_cols = ['Product_ID', 'type_of_product', 'size', 'size_unit', 'volume_position', 'competitors_count',
                    'clamp_direction', 'predicted_price', 'predicted_price_per_unit',
                    'comp_median_per_unit_after_trim', 'predicted_over_cost', 'median_quote',
                    'predicted_over_quote', 'floor_binding']
    text = f"""# {args.subindustry} diagnosis (F5)

Data folder: `{args.data_dir}`. Generated by `ml_model/pets_diagnosis.py`; local file, may contain costs.

## Totals

""" + '\n'.join(f"- {k}: {v}" for k, v in totals.items()) + f"""

## Definitions

- Prices per unit are SAR per liter (volumetric) or SAR per piece (units), from the processed
  competitor files; `*_per_unit` = before trimming, `*_after_trim` = after the pipeline's p10-p90
  trimming (`filter_outliers_with_percentiles`, run on the full processed files).
- `emptied_by_trim`: rows before trimming and none after (finding 12, bug 6).
- `pct_fl_oz`: raw volumetric rows with unit `fl oz`. `pct_us_size_ml`: raw mL/L rows whose size is a
  common US size ({', '.join(map(str, US_SIZES_FL_OZ))} fl oz, within max({US_SIZE_TOLERANCE_ML} mL,
  {US_SIZE_TOLERANCE_REL:.1%} of the US size); 2 and 22 fl oz are left out because they collide with metric
  60 mL and 650 mL bottles.
- `pct_multipack`: raw rows with a product title whose title has a pack marker, over rows with a title
  (only scraper rows have titles; empty when none). Piece counts of wipes are not multipacks.
- `n_price_zero`: raw price exactly 0. `n_le_5ml`: raw volumetric size <= {SMALL_ML:g} mL.
- `clamp_direction`: `up` when the market clamp raised the model price to `market_min`, `down` when it
  cut it to `market_max` (compares the model's price per unit with the final one).
- `volume_position`: Wayakit size versus the trimmed competitor size range of its type.
- `median_quote`: median approved quote per Product_ID, a proxy for the current selling price (the live
  Odoo KSA price is not in the data). `floor_binding`: price = round(cost x {MARGIN_FLOOR:.2f}, 2).
- `no_size`: Wayakit SKU with Volume_Liters and Pack_quantity_Units both 0; the model is not used and
  the price is the margin floor.
- `median_pred_over_cost_sized`: median of predicted price / Unit_cost_SAR over the Wayakit SKUs with a
  size (the `no_size` SKUs, priced at the cost x {MARGIN_FLOOR:.2f} floor, are excluded).
  `median_pred_over_cost_all`: the same median over all Wayakit SKUs, `no_size` included. Do not
  confuse the two.

## By type

{_md_table(by_type, type_cols)}

## Wayakit products

{_md_table(products, product_cols)}
"""
    Path(path).write_text(text, encoding='utf-8')


# --- CLI ------------------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="F5 diagnosis of a subindustry's competitor data and suggested prices.")
    parser.add_argument('--data-dir', default='.', help="Folder with ml_model/ and scraper/ subfolders (default: the repo).")
    parser.add_argument('--subindustry', default='Pets', help="Subindustry to diagnose, case-insensitive (default: Pets).")
    parser.add_argument('--out-dir', default='data/f5_pets', help="Output folder (default: data/f5_pets).")
    return parser.parse_args(argv)


def check_out_dir(out_dir, data_dir):
    out = Path(out_dir).resolve()
    for base in {REPO_ROOT, Path(data_dir).resolve()}:
        for sub in ('ml_model', 'scraper'):
            forbidden = base / sub
            if out == forbidden or forbidden in out.parents:
                raise DiagnosisError(f"--out-dir must not be inside {forbidden}")


def run(args, trim_fn=None, preprocess_module=None):
    """Runs the diagnosis and writes the three outputs; returns (by_type, products, totals)."""
    data_dir = Path(args.data_dir)
    check_inputs(data_dir)
    check_out_dir(args.out_dir, data_dir)
    if preprocess_module is None:
        preprocess_module = load_pipeline_module('1a_preprocess_data.py')
    drop_sizes_fn = None
    if trim_fn is None:
        training = load_pipeline_module('2_train_models.py')
        trim_fn = training.filter_outliers_with_percentiles
        drop_sizes_fn = training.drop_implausible_sizes

    raw = add_raw_facts(load_raw_competitors(data_dir, preprocess_module))
    raw_s = raw[in_subindustry(raw, args.subindustry)]
    if raw_s.empty:
        raise DiagnosisError(f"Subindustry '{args.subindustry}' has 0 competitor rows in {data_dir}")

    vol_processed = read_csv(data_dir / INPUTS['vol_processed'])
    unit_processed = read_csv(data_dir / INPUTS['unit_processed'])
    # Trim the files as training reads them (labels untouched), then normalize labels for grouping.
    trimmed = {model: normalize_labels(df)
               for model, df in trim_processed(vol_processed, unit_processed, trim_fn, drop_sizes_fn).items()}
    processed = {'volumetric': normalize_labels(vol_processed), 'units': normalize_labels(unit_processed)}
    wayakit = prepare_wayakit(read_csv(data_dir / INPUTS['to_predict']), read_csv(data_dir / INPUTS['report']),
                              read_csv(data_dir / INPUTS['quotes']), args.subindustry)
    by_type = build_by_type(raw, processed, trimmed, wayakit, args.subindustry)
    products = build_wayakit_table(wayakit, by_type)
    totals = summarize(by_type, products, raw_s)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    by_type.to_csv(out / 'pets_by_type.csv', index=False)
    products.to_csv(out / 'pets_wayakit_products.csv', index=False)
    write_report(out / 'pets_report.md', args, by_type, products, totals)
    return by_type, products, totals


def main(argv=None):
    args = parse_args(argv)
    try:
        _, _, totals = run(args)
    except DiagnosisError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    for key, value in totals.items():
        print(f"{key}: {value}")
    print(f"Outputs written to {Path(args.out_dir).resolve()}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
