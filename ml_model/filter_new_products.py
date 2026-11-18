# ml_model/filter_new_products.py
import pandas as pd
import datetime
import os
import sys
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
import argparse # To handle command-line arguments
from log_config import get_logger # Make sure log_config.py is accessible

logger = get_logger()

# Default files (can be overridden by arguments)
DEFAULT_FULL_PRODUCTS_FILE = 'wayakit_products.csv'
# The single log and state folder (same rule as log_config.LOG_DIR, run_pipeline.sh and
# odoo_api_price_suggestion.log_dir()): PI_LOG_DIR if set and non-empty, else <repo>/../logs.
# Outside the code so a new code folder (F4 releases) keeps it; not relative to the cwd.
DEFAULT_TIMESTAMP_FILE = os.path.join(
    os.path.abspath(os.environ.get('PI_LOG_DIR') or os.path.join(project_root, '..', 'logs')),
    'last_full_run.txt')
DEFAULT_OUTPUT_FILE = 'wayakit_new_products_temp.csv'

def read_last_full_run_timestamp(filepath):
    """Reads the timestamp of the last full run."""
    try:
        with open(filepath, 'r') as f:
            timestamp_str = f.read().strip()
        # Odoo returns UTC dates; we assume the saved timestamp is UTC too
        # Expected format: YYYY-MM-DD HH:MM:SS
        ts = datetime.datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
        logger.info(f"Last full run timestamp read: {ts}")
        return ts
    except FileNotFoundError:
        logger.error(f"Timestamp file '{filepath}' not found. New products cannot be filtered.")
        return None
    except Exception as e:
        logger.error(f"Error reading timestamp from '{filepath}': {e}", exc_info=True)
        return None

def filter_new_products(full_products_file, timestamp_file, output_file):
    """Filters products based on the creation date and the timestamp."""
    logger.info(f"Starting new products filter.")
    logger.info(f"Reading full products from: {full_products_file}")
    logger.info(f"Using timestamp from: {timestamp_file}")
    logger.info(f"Output file for new products: {output_file}")

    last_run_ts = read_last_full_run_timestamp(timestamp_file)
    if last_run_ts is None:
        logger.error("Could not get the timestamp. Exiting the filter script.")
        # Write an empty file to avoid later errors? Or handle it in the bash script.
        # For now, we exit. The bash script should detect the failure.
        sys.exit(1) # Exit with error

    try:
        # Read the full CSV, making sure to parse the create_date column
        # Odoo stores dates as UTC strings 'YYYY-MM-DD HH:MM:SS'
        df_all = pd.read_csv(full_products_file, parse_dates=['create_date'])
        logger.info(f"Read {len(df_all)} products from {full_products_file}.")
    except FileNotFoundError:
        logger.error(f"Full products file '{full_products_file}' not found.")
        sys.exit(1)
    except KeyError:
        logger.error(f"Column 'create_date' was not found in '{full_products_file}'. Did you run the modified 'odoo_api_products.py'?")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error reading '{full_products_file}': {e}", exc_info=True)
        sys.exit(1)

    # Filter products whose creation date is later than the last timestamp
    # Both dates must be in the same time zone (or have no time zone) to be compared
    # By default Pandas parses without a time zone, which should work if the timestamp has none either.
    df_new = df_all[df_all['create_date'] > last_run_ts].copy()

    logger.info(f"Found {len(df_new)} products created after {last_run_ts}.")

    try:
        # Save only the new products
        df_new.to_csv(output_file, index=False, encoding='utf-8-sig')
        logger.info(f"File with new products saved to: '{output_file}'")
    except Exception as e:
        logger.error(f"Error saving filtered file '{output_file}': {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    # Configure command-line arguments
    parser = argparse.ArgumentParser(description="Filters Wayakit products based on the date of the last full run.")
    parser.add_argument('--input', default=DEFAULT_FULL_PRODUCTS_FILE, help=f"CSV file with all products (default: {DEFAULT_FULL_PRODUCTS_FILE})")
    parser.add_argument('--timestamp', default=DEFAULT_TIMESTAMP_FILE, help=f"File with the timestamp of the last full run (default: {DEFAULT_TIMESTAMP_FILE})")
    parser.add_argument('--output', default=DEFAULT_OUTPUT_FILE, help=f"CSV file where the filtered products are saved (default: {DEFAULT_OUTPUT_FILE})")
    args = parser.parse_args()

    filter_new_products(args.input, args.timestamp, args.output)