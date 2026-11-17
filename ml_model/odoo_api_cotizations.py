"""Reads confirmed Wayakit sales (`sale.order.line`) from Odoo into ml_model/wayakit_cotizations.csv.

The window is the last 12 months up to the run date (UTC), computed by `sales_window()`;
before F2 it was fixed to 2025 and 2026 sales never reached training (C12).

Importing this module does not read secrets or connect to Odoo: that happens in `main()`.
`fetch_sale_lines()` takes an `OdooSession` (pi_odoo) so tests can pass a fake `models`.
Any failure, including 0 sale lines in the window, exits with code 1.
"""
import argparse
import calendar
import os
import sys
from datetime import date, datetime, timezone

import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_config import get_logger
from pi_odoo import OdooSession, connect
from pi_secrets import get_odoo_credentials

logger = get_logger()

OUTPUT_CSV_FILE = 'ml_model/wayakit_cotizations.csv'
MODEL_NAME = 'sale.order.line'
ODOO_DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'
WINDOW_MONTHS = 12

FIELDS_TO_GET = [
    'name',
    'product_uom_qty',
    'price_subtotal',
]

FINAL_COLUMNS = [
    'Product_ID',
    'Description',
    'approved_quote_price',
]


def _months_back(d: date, months: int) -> date:
    """Same day `months` calendar months earlier; clamped to the last day of a shorter month
    (e.g. 2028-02-29 minus 12 months is 2027-02-28)."""
    year, month0 = divmod(d.year * 12 + (d.month - 1) - months, 12)
    month = month0 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def sales_window(today) -> tuple:
    """Returns `(start, end)` Odoo datetime strings for the last 12 months up to `today`.

    Pure: `today` is injected (a `date` or `datetime`; only the date part is used).
    `start` is the same calendar day 12 months earlier at 00:00:00 and `end` is `today` at
    23:59:59, so both the first and the last day are included.
    """
    if isinstance(today, datetime):
        today = today.date()
    start = datetime.combine(_months_back(today, WINDOW_MONTHS), datetime.min.time())
    end = datetime.combine(today, datetime.max.time().replace(microsecond=0))
    return start.strftime(ODOO_DATETIME_FORMAT), end.strftime(ODOO_DATETIME_FORMAT)


def build_domain(start: str, end: str) -> list:
    """`sale.order.line` domain: confirmed orders, positive lines, `[FP-...]` products, in window."""
    return [
        ['order_id.state', '=', 'sale'],
        ['product_uom_qty', '>', 0],
        ['price_subtotal', '>', 0],
        ['name', 'like', '[FP-%'],
        ['order_id.date_order', '>=', start],
        ['order_id.date_order', '<=', end],
    ]


def fetch_sale_lines(session: OdooSession, start: str, end: str) -> list:
    """Reads the sale lines of the window (one `search_read` call)."""
    logger.info(f"Searching records in '{MODEL_NAME}' from {start} to {end} (UTC)...")
    records = session.execute(MODEL_NAME, 'search_read', build_domain(start, end), fields=FIELDS_TO_GET)
    logger.info(f"Found {len(records)} order lines matching the format.")
    return records


def records_to_dataframe(records: list) -> pd.DataFrame:
    """Sale lines -> `Product_ID, Description, approved_quote_price`. Pure: no I/O.

    `Product_ID` is the code in brackets at the start of the line description; the price is
    `price_subtotal / product_uom_qty`.
    """
    df = pd.DataFrame(records).rename(columns={
        'name': 'Description',
        'product_uom_qty': 'Quantity',
        'price_subtotal': 'Subtotal'
    })
    df['Product_ID'] = df['Description'].str.extract(r'\[(.*?)\]')
    df['Description'] = df['Description'].str.replace(r'\[.*?\]\s*', '', regex=True)
    df['approved_quote_price'] = df['Subtotal'] / df['Quantity']
    return df[FINAL_COLUMNS]


def main(argv=None, session: OdooSession = None, today=None) -> int:
    parser = argparse.ArgumentParser(description="Reads the last 12 months of Wayakit sales from Odoo.")
    parser.add_argument('--output', default=OUTPUT_CSV_FILE, help=f"Output CSV (default: {OUTPUT_CSV_FILE})")
    args = parser.parse_args(argv)

    start, end = sales_window(today or datetime.now(timezone.utc).date())

    if session is None:
        session = connect(get_odoo_credentials())

    records = fetch_sale_lines(session, start, end)
    if not records:
        # 12 months without a single sale means a broken query or the wrong database, not a
        # quiet month; training on the previous CSV would hide it.
        logger.error(f"No sale lines found in '{MODEL_NAME}' between {start} and {end}. Aborting.")
        return 1

    df_to_export = records_to_dataframe(records)
    df_to_export.to_csv(args.output, index=False, encoding='utf-8-sig')
    logger.info(f"File '{args.output}' created with {len(df_to_export)} lines.")
    logger.info(f"Preview of the first 5 rows:\n{df_to_export.head()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("CRITICAL ERROR while reading sales from Odoo.")
        sys.exit(1)
