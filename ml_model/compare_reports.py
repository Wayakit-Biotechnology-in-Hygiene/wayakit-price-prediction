"""Before/after comparison of two prediction reports (``wayakit_prediction_report.csv``).

Read-only: it reads two report CSVs, reads no secrets, never touches Odoo or the network, and
writes only to ``--out-dir``. Used for F7a (old pipeline vs new pipeline on the same frozen
data, both run offline in temporary copies; see docs/runbook.md) and for the F8 review.

Rows are matched by ``Product_ID`` and occurrence number (the catalog can repeat a SKU). Only
products with a size in both reports are compared (no-size templates are left out: they get
only the margin floor and, since F7a, are not uploaded).

Per group (``--by``, default ``subindustry``):

- ``n``: compared products; ``n_changed``: products whose price moved by more than 0.5 %.
- ``median_ratio``: median of new price / old price (products with an old price > 0);
  ``min_ratio`` / ``max_ratio``: its range.
- ``n_with_p75``: products whose new report has a local p75 from at least 3 market rows
  (``competitors_count`` >= 3, the same minimum as the clamp; the p75 comes from the
  similar-size rows when there are enough).
  The same yardstick is used for both reports, so old and new are measured against one p75.
- ``pct_old_above_p75`` / ``pct_new_above_p75``: share of those products priced above the local
  p75 by more than half a cent. ``pct_new_above_p75_floor``: share above it only because the
  cost x 1.30 floor is higher than the p75 (the floor always wins).

How to run it::

    python ml_model/compare_reports.py --old <old report.csv> --new <new report.csv> --out-dir <dir>

Outputs: ``comparison_by_<by>.csv`` and ``comparison_by_<by>.md``. Exit code 1 with a message if
an input is missing a column.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

MARGIN_FLOOR = 1.30
MIN_COMPETITORS = 3  # same minimum as the market clamp in 3_predicted_prices.py
CHANGE_TOLERANCE = 0.005  # relative change below this is "unchanged"
CENT = 0.005
REQUIRED = ['Product_ID', 'subindustry', 'product_type', 'volume', 'cost_per_unit', 'predicted_price']


class CompareError(Exception):
    """An input problem that ends the run with exit code 1."""


def _keyed(report, label):
    missing = [c for c in REQUIRED if c not in report.columns]
    if missing:
        raise CompareError(f"The {label} report has no columns {missing}.")
    report = report.copy()
    report['key'] = report['Product_ID'].astype(str) + '#' + report.groupby('Product_ID').cumcount().astype(str)
    return report.set_index('key')


def _has_size(report):
    volume = pd.to_numeric(report['volume'].astype(str).str.split(' ').str[0], errors='coerce')
    return (volume > 0).fillna(False)


def join_reports(old, new):
    """Old and new report rows of the products with a size in both, side by side. Pure."""
    old, new = _keyed(old, 'old'), _keyed(new, 'new')
    keys = old.index.intersection(new.index)
    old, new = old.loc[keys], new.loc[keys]
    keep = _has_size(old) & _has_size(new)
    p75 = new['market_p75_found'] if 'market_p75_found' in new else pd.Series(0.0, index=new.index)
    p75 = p75.where(new['competitors_count'] >= MIN_COMPETITORS, 0.0) if 'competitors_count' in new else p75
    joined = pd.DataFrame({
        'subindustry': new['subindustry'], 'product_type': new['product_type'],
        'cost': new['cost_per_unit'].astype(float),
        'old_price': old['predicted_price'].astype(float), 'new_price': new['predicted_price'].astype(float),
        'p75': p75.astype(float),
    }, index=keys)
    return joined[keep]


def summarize(joined, by='subindustry'):
    """Per-group metrics (see the module docstring). Pure."""
    def group_metrics(g):
        ratio = (g['new_price'] / g['old_price']).where(g['old_price'] > 0).dropna()
        with_p75 = g[g['p75'] > 0]
        floor = with_p75['cost'] * MARGIN_FLOOR
        new_above = with_p75['new_price'] > with_p75['p75'] + CENT
        floor_above = new_above & (floor > with_p75['p75'])
        pct = (lambda mask: round(100 * mask.mean(), 1) if len(with_p75) else np.nan)
        return pd.Series({
            'n': len(g),
            'n_changed': int(((ratio - 1).abs() > CHANGE_TOLERANCE).sum()),
            'median_ratio': round(ratio.median(), 3) if len(ratio) else np.nan,
            'min_ratio': round(ratio.min(), 3) if len(ratio) else np.nan,
            'max_ratio': round(ratio.max(), 3) if len(ratio) else np.nan,
            'n_with_p75': len(with_p75),
            'pct_old_above_p75': pct(with_p75['old_price'] > with_p75['p75'] + CENT),
            'pct_new_above_p75': pct(new_above),
            'pct_new_above_p75_floor': pct(floor_above),
        })

    rows = [group_metrics(g).rename(name) for name, g in joined.groupby(by, sort=True, dropna=False)]
    rows.append(group_metrics(joined).rename('All'))
    table = pd.DataFrame(rows)
    table.index.name = by
    for column in ('n', 'n_changed', 'n_with_p75'):
        table[column] = table[column].astype(int)
    return table.reset_index()


def to_markdown(table):
    header = '| ' + ' | '.join(table.columns) + ' |'
    rule = '|' + '|'.join('---' for _ in table.columns) + '|'
    body = ['| ' + ' | '.join('' if pd.isna(v) else str(v) for v in row) + ' |' for row in table.itertuples(index=False)]
    return '\n'.join([header, rule, *body]) + '\n'


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--old', required=True, help='report of the old pipeline')
    parser.add_argument('--new', required=True, help='report of the new pipeline')
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--by', default='subindustry', choices=['subindustry', 'product_type'])
    parser.add_argument('--subindustry', help='only this subindustry (e.g. Pets)')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        joined = join_reports(pd.read_csv(args.old), pd.read_csv(args.new))
        if args.subindustry:
            joined = joined[joined['subindustry'].astype(str).str.casefold() == args.subindustry.casefold()]
        table = summarize(joined, args.by)
    except (CompareError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    table.to_csv(out / f'comparison_by_{args.by}.csv', index=False)
    (out / f'comparison_by_{args.by}.md').write_text(to_markdown(table))
    print(to_markdown(table))
    return 0


if __name__ == '__main__':
    sys.exit(main())
