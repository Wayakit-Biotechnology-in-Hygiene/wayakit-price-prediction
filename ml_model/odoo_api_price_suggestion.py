"""Uploads the price suggestions (ml_model/wayakit_prediction_report.csv) to Odoo
(`product.price.suggestion`). WRITES TO ODOO: only run for real with Mafer's OK.

Modes:
- Products without a size (no-size parent templates: `has_size` False or `volume` 0) are never
  uploaded: their price is only the cost x 1.30 floor (F7a). In full mode their previous
  suggestions are removed with the rest.
- `--dry-run`: builds the records and writes them to a CSV (default
  ml_model/price_suggestion_dry_run.csv). Reads no secrets and connects to nothing.
- `--run_mode partial` (default): creates the new records; deletes nothing.
- `--run_mode full`: replaces the table without ever leaving it empty:
    1. reads the IDs of the current suggestions and saves them to
       odoo_previous_suggestion_ids.json in the log folder (PI_LOG_DIR, default <repo>/../logs;
       see `log_dir()`) (`ids`, no `created_ids` yet);
    2. creates the new batch (one call: Odoo creates all or none). If it fails, exit 1;
    3. as soon as the create returns, writes the new IDs to the file (`created_ids`): from then
       on the file proves that a new batch exists;
    4. checks that the created IDs exist, archives the PREVIOUS IDs only (read + create in
       `product.price.suggestion.history`) and unlinks the PREVIOUS IDs only. If this fails,
       exit 1: the table has old and new rows, and the file stays for `--cleanup-previous`.
- `--cleanup-previous`: finishes step 4 from the file (skips the archive if it was already
  done), then deletes the file. It refuses (exit 1, deletes nothing) if the file has no
  `created_ids` or those records no longer exist: then the previous suggestions may be the
  only ones left (e.g. the process was killed during the create).
- A full upload that finds a file WITHOUT `created_ids` (killed before step 3) deletes it and
  starts again: it re-reads every current ID, so whatever that create did is replaced too. A file
  WITH `created_ids` means a cleanup is pending: the upload refuses until `--cleanup-previous`.

As soon as a real upload's create returns (partial or full; never `--dry-run` or
`--cleanup-previous`), before the cleanup of the previous batch, the uploaded report is copied
atomically to `--state-file` (default state/last_uploaded_prediction_report.csv in the log
folder): the new prices are what Odoo now shows, and they are what 3_predicted_prices.py's
stability brake compares the next run against, also when the cleanup then fails. If only that
copy fails, the upload and the cleanup still go on, and the script exits 1 (see `upload`).

Before any write the script checks that the Odoo user sees exactly one company (the PI
database is KSA only); if it sees more, it exits 1: a company filter is needed, not a guess.

Importing this module does not read secrets or connect to Odoo. The functions take an
`OdooSession` (pi_odoo) so tests can pass a fake `models`.
"""
import argparse
import contextlib
import json
import math
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_config import get_logger
# pi_odoo / pi_secrets are imported only where Odoo is used, so `--dry-run` works in a copy that
# has only ml_model/ and log_config.py (the E2E harness) and never loads the secrets code.

logger = get_logger()

MODEL_NAME = 'product.price.suggestion'
HISTORY_MODEL_NAME = 'product.price.suggestion.history'

REPORT_FILE = 'ml_model/wayakit_prediction_report.csv'
DRY_RUN_FILE = 'ml_model/price_suggestion_dry_run.csv'
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log_dir(environ=os.environ, project_dir: str = PROJECT_DIR) -> str:
    """The single log and state folder: PI_LOG_DIR if set and non-empty, else <project>/../logs.

    Same rule as log_config.LOG_DIR and run_pipeline.sh. Outside the code so the IDs file survives
    a new code folder (F4 releases). Computed here rather than imported from log_config, so a copy
    with only ml_model/ (the E2E harness) or a stand-in log_config resolves it the same way.
    """
    return os.path.abspath(environ.get('PI_LOG_DIR') or os.path.join(project_dir, '..', 'logs'))


PREVIOUS_IDS_FILE = os.path.join(log_dir(), 'odoo_previous_suggestion_ids.json')
# The report of the last real upload: 3_predicted_prices.py's stability brake compares the next
# run against it (LAST_UPLOADED_REPORT there). Written by a real upload as soon as its create returns.
LAST_UPLOADED_REPORT_FILE = os.path.join(log_dir(), 'state', 'last_uploaded_prediction_report.csv')

# More unparseable rows than this means a broken report, not a few bad products.
MAX_SKIPPED_FRACTION = 0.05

FIELDS_TO_ARCHIVE = [
    'product_id_str', 'product_type', 'generic_product_type',
    'subindustry', 'industry', 'volume_units',
    'production_cost', 'suggested_price', 'profit', 'create_date',
    'product_name', 'predicted_price_per_unit', 'model_confidence',
    'market_min_found', 'market_max_found', 'competitors_count'
]

# Odoo field -> report column, by type.
TEXT_FIELDS = {
    'product_type': 'product_type',
    'generic_product_type': 'generic_product_type',
    'subindustry': 'subindustry',
    'industry': 'industry',
    'product_name': 'product_name',
}
FLOAT_FIELDS = {
    'production_cost': 'cost_per_unit',
    'suggested_price': 'predicted_price',
    'profit': 'porcentaje_de_ganancia',
    'predicted_price_per_unit': 'predicted_price_per_unit',
    'model_confidence': 'model_confidence',
    'market_min_found': 'market_min_found',
    'market_max_found': 'market_max_found',
}


class UploadError(RuntimeError):
    """The upload cannot go on safely. The message says what state Odoo was left in."""


class StateFileError(UploadError):
    """The upload to Odoo succeeded, but the uploaded report could not be saved as the stability
    brake's state. `created_ids` are the records the upload created (they stay in Odoo)."""

    def __init__(self, message, created_ids):
        super().__init__(message)
        self.created_ids = created_ids


# --- Records (pure) ---------------------------------------------------------------------------

def _finite_float(value, column: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"'{column}' is not a finite number: {value!r}")
    return number


def row_to_record(row) -> dict:
    """One report row -> one `product.price.suggestion` record. Pure: no I/O.

    Raises (ValueError, TypeError, KeyError) if the row cannot be uploaded: missing column,
    empty `Product_ID`, non-numeric or non-finite number, `volume` not starting with a number
    ("0.5 L", "12 units"). Empty text fields become False (Odoo's empty value) instead of NaN.
    """
    product_id = row['Product_ID']
    if product_id is None or (isinstance(product_id, float) and math.isnan(product_id)) \
            or not str(product_id).strip():
        raise ValueError("'Product_ID' is empty")

    record = {
        'product_id_str': str(product_id),
        'volume_units': _finite_float(str(row['volume']).split(' ')[0], 'volume'),
    }
    for field, column in TEXT_FIELDS.items():
        value = row[column]
        record[field] = False if pd.isna(value) else value
    for field, column in FLOAT_FIELDS.items():
        record[field] = _finite_float(row[column], column)
    competitors = _finite_float(row['competitors_count'], 'competitors_count')
    if competitors != int(competitors):
        raise ValueError(f"'competitors_count' is not an integer: {row['competitors_count']!r}")
    record['competitors_count'] = int(competitors)
    return record


def without_size(df: pd.DataFrame) -> pd.Series:
    """True for report rows of products with no size (no-size parent templates). Pure.

    Their price is only the cost x 1.30 floor, not a suggestion, so they are not uploaded (F7a);
    the fix is their size in Odoo's product master data. A row is "no size" if the report says
    `has_size` False (since F7a) or its `volume` starts with 0 or less ("0 units").
    """
    volume = pd.to_numeric(df['volume'].astype(str).str.split(' ').str[0], errors='coerce')
    no_size = volume <= 0
    if 'has_size' in df:
        no_size |= df['has_size'].astype(str).str.strip().str.lower().isin(['false', '0'])
    return no_size.fillna(False).astype(bool)


def build_records(df: pd.DataFrame) -> tuple:
    """Report -> `(records, skipped)`. Pure apart from one warning log per skipped row.

    `records` keeps the order of `df`; `skipped` is a list of `(row_number, product_id, reason)`
    (1-based row number) for rows `row_to_record` rejected. `len(records) + len(skipped)
    == len(df)` always. With a default integer index (a report read from CSV, even after rows
    were filtered out) the row number is the report's own (index + 1).
    """
    records, skipped = [], []
    numbers = (df.index + 1) if pd.api.types.is_integer_dtype(df.index) else range(1, len(df) + 1)
    for position, (_, row) in zip(numbers, df.iterrows()):
        try:
            records.append(row_to_record(row))
        except (ValueError, TypeError, KeyError) as e:
            product_id = row.get('Product_ID', 'N/A')
            skipped.append((position, product_id, str(e)))
            logger.warning(f"Skipping row {position} (Product ID: {product_id}): {e}")
    return records, skipped


def check_parse_quality(records: list, skipped: list,
                        max_skipped_fraction: float = MAX_SKIPPED_FRACTION) -> None:
    """Raises `UploadError` if there is nothing to upload or more than `max_skipped_fraction`
    of the rows were skipped. Pure."""
    total = len(records) + len(skipped)
    if not records:
        raise UploadError(f"No valid records to upload ({len(skipped)} rows skipped of {total}).")
    if len(skipped) / total > max_skipped_fraction:
        raise UploadError(
            f"{len(skipped)} of {total} rows could not be parsed "
            f"(more than {max_skipped_fraction:.0%}); the report looks broken. Nothing uploaded."
        )


def history_records(old_records: list) -> list:
    """Records read from `product.price.suggestion` -> records for the history model. Pure.

    Drops `id` and renames `create_date` to `original_create_date`; does not modify the input.
    """
    result = []
    for record in old_records:
        new = {k: v for k, v in record.items() if k != 'id'}
        if 'create_date' in new:
            new['original_create_date'] = new.pop('create_date')
        result.append(new)
    return result


# --- Previous IDs file ------------------------------------------------------------------------

def save_previous_ids(path: str, ids: list, archived: bool = False, created_ids: list = None) -> None:
    """Writes the pending-cleanup file atomically (temp file + rename).

    `ids` are the previous suggestions to remove; `created_ids` (None until the create returns)
    are the new batch that replaces them. Without `created_ids` nothing may be unlinked.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    payload = {
        'model': MODEL_NAME,
        'saved_at_utc': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'archived': archived,
        'ids': sorted(int(i) for i in ids),
        'created_ids': None if created_ids is None else sorted(int(i) for i in created_ids),
    }
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=1)
    os.replace(tmp, path)


def load_previous_ids(path: str) -> dict:
    with open(path, encoding='utf-8') as f:
        payload = json.load(f)
    if payload.get('model') != MODEL_NAME or not isinstance(payload.get('ids'), list) \
            or not isinstance(payload.get('created_ids'), (list, type(None))):
        raise UploadError(f"'{path}' is not a previous-suggestions file for '{MODEL_NAME}'.")
    return payload


def check_new_batch_exists(session: 'OdooSession', created_ids: list, ids_file: str) -> None:
    """Refuses (UploadError) to delete the previous suggestions unless the new batch is recorded
    in `ids_file` and every one of its records still exists in Odoo."""
    if not created_ids:
        raise UploadError(
            f"'{ids_file}' has no created_ids: the new batch was never confirmed (the upload was "
            f"probably killed during the create), so the previous suggestions may be the only ones. "
            f"Not deleting anything. Run the full upload again: it discards this file and replaces "
            f"every current suggestion."
        )
    found = session.execute(MODEL_NAME, 'search_count', [['id', 'in', list(created_ids)]])
    if found != len(created_ids):
        raise UploadError(
            f"Only {found} of the {len(created_ids)} new suggestions listed in '{ids_file}' exist in "
            f"'{MODEL_NAME}'. Not deleting the previous ones. Check the table in Odoo; when it is "
            f"understood, run the full upload again (after deleting '{ids_file}')."
        )


def save_uploaded_report(report_file: str, state_file: str) -> None:
    """Copies the uploaded report to `state_file` atomically: a temp file in the same folder,
    then `os.replace`, so a reader never sees half a file and a failure leaves no partial one."""
    folder = os.path.dirname(os.path.abspath(state_file))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.last_uploaded_', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as target, open(report_file, 'rb') as source:
            shutil.copyfileobj(source, target)
        os.replace(tmp, state_file)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


# --- Odoo steps -------------------------------------------------------------------------------

def confirm_company(session: 'OdooSession') -> str:
    """Company guard before any write (see module docstring). Returns the company name."""
    from pi_odoo import get_single_company
    company_id, company_name = get_single_company(session)
    logger.info(f"Odoo company confirmed: '{company_name}' (id {company_id}), the only one visible.")
    return company_name


def archive_and_unlink(session: 'OdooSession', previous_ids: list, ids_file: str,
                       created_ids: list, already_archived: bool = False) -> int:
    """Archives (unless `already_archived`) and unlinks ONLY `previous_ids`, after checking that
    the new batch `created_ids` exists (`check_new_batch_exists`). Returns how many records were
    removed. Updates `ids_file` after archiving so a retry does not archive twice, and deletes it
    when done."""
    check_new_batch_exists(session, created_ids, ids_file)
    if set(created_ids) & set(previous_ids):
        raise UploadError("New and previous IDs overlap; refusing to delete anything.")
    old_records = session.execute(MODEL_NAME, 'read', list(previous_ids), fields=FIELDS_TO_ARCHIVE)
    existing_ids = sorted(r['id'] for r in old_records)
    if len(existing_ids) != len(previous_ids):
        logger.warning(f"{len(previous_ids) - len(existing_ids)} of the previous IDs no longer exist in "
                       f"'{MODEL_NAME}'; only the {len(existing_ids)} that exist are archived and deleted.")

    if existing_ids and not already_archived:
        logger.info(f"Archiving {len(old_records)} previous records to '{HISTORY_MODEL_NAME}'...")
        session.execute(HISTORY_MODEL_NAME, 'create', history_records(old_records))
        save_previous_ids(ids_file, existing_ids, archived=True, created_ids=created_ids)
        logger.info("Previous records archived.")

    if existing_ids:
        logger.info(f"Deleting {len(existing_ids)} previous records from '{MODEL_NAME}'...")
        session.execute(MODEL_NAME, 'unlink', existing_ids)
        logger.info("Previous records deleted.")

    os.remove(ids_file)
    return len(existing_ids)


def upload(session: 'OdooSession', records: list, run_mode: str,
           ids_file: str = PREVIOUS_IDS_FILE, report_file: str = None,
           state_file: str = LAST_UPLOADED_REPORT_FILE) -> list:
    """Company guard, then the partial or full flow described in the module docstring.

    Returns the IDs of the created records. Raises `UploadError` (or the Odoo exception) on any
    failure; the message says what state Odoo was left in.

    With `report_file`, right after the create returns (the new prices are then what Odoo shows)
    and before the cleanup of the previous batch, that report is copied to `state_file`
    (`save_uploaded_report`) for the next run's stability brake; a failed cleanup therefore leaves
    the state with the new prices, and `--cleanup-previous` never writes it. If the copy fails,
    nothing in Odoo is undone: the error is logged, an older state file is removed so the next run
    does not brake against prices that are no longer the uploaded ones, the cleanup of the
    previous batch still runs, and then `StateFileError` is raised so the run exits 1 and the
    pipeline alert fires (a failed cleanup raises its own `UploadError` instead; the state error
    is already in the log).
    """
    if run_mode not in ('partial', 'full'):
        raise UploadError(f"Unknown run mode '{run_mode}'.")
    confirm_company(session)

    previous_ids = []
    if run_mode == 'full':
        if os.path.exists(ids_file):
            if load_previous_ids(ids_file).get('created_ids'):
                raise UploadError(
                    f"'{ids_file}' exists: a previous full upload created its batch but did not finish "
                    f"its cleanup. Run 'python ml_model/odoo_api_price_suggestion.py --cleanup-previous' "
                    f"first. Nothing uploaded."
                )
            # Killed before the create was confirmed: its new batch, if Odoo committed it, is
            # among the current IDs read below, so this run replaces it too.
            logger.warning(f"'{ids_file}' from an interrupted upload has no created_ids; discarding it. "
                           f"Every current suggestion is re-read and replaced by this upload.")
            os.remove(ids_file)
        previous_ids = session.execute(MODEL_NAME, 'search', [])
        logger.info(f"Found {len(previous_ids)} current suggestions in '{MODEL_NAME}'.")
        if previous_ids:
            save_previous_ids(ids_file, previous_ids)
            logger.info(f"Previous IDs saved to '{ids_file}'.")

    logger.info(f"Creating {len(records)} new records in '{MODEL_NAME}' (one call)...")
    try:
        new_ids = session.execute(MODEL_NAME, 'create', records)
    except Exception as e:
        if previous_ids:
            os.remove(ids_file)  # no created_ids: nothing may be deleted on its account
        raise UploadError(
            f"Creating the new suggestions failed ({type(e).__name__}). The previous suggestions "
            f"were not touched. Odoo creates all or none; after a timeout or a lost connection it "
            f"may still have created them: the next full upload replaces every current suggestion "
            f"either way."
        ) from e
    new_ids = new_ids if isinstance(new_ids, list) else [new_ids]
    logger.info(f"Created {len(new_ids)} new records.")
    state_error = _save_state(report_file, state_file, len(new_ids)) if report_file else None

    if run_mode == 'full' and previous_ids:
        try:
            save_previous_ids(ids_file, previous_ids, created_ids=new_ids)
            removed = archive_and_unlink(session, previous_ids, ids_file, new_ids)
        except UploadError:
            raise  # already says what state Odoo is in and what to do
        except Exception as e:
            raise UploadError(
                f"The new suggestions were created, but archiving/deleting the previous ones failed "
                f"({type(e).__name__}). The table has old and new rows. The previous IDs are in "
                f"'{ids_file}'; retry with 'python ml_model/odoo_api_price_suggestion.py --cleanup-previous'."
            ) from e
        logger.info(f"Replaced {removed} previous suggestions with {len(new_ids)} new ones.")

    if state_error is not None:
        raise StateFileError(
            f"Uploaded, but the stability-brake state '{state_file}' was not written "
            f"({type(state_error).__name__}).", new_ids) from state_error
    return new_ids


def _save_state(report_file: str, state_file: str, created: int):
    """`save_uploaded_report` for `upload`: returns None, or the exception after logging it and
    removing an older `state_file` (it would hold prices that are no longer the uploaded ones)."""
    try:
        save_uploaded_report(report_file, state_file)
    except Exception as e:
        logger.error(f"The new suggestions are in Odoo ({created} created), but saving '{report_file}' "
                     f"as '{state_file}' for the stability brake failed ({type(e).__name__}: {e}). "
                     f"Copy it there by hand; there is no need to upload again.")
        with contextlib.suppress(OSError):
            os.remove(state_file)
        return e
    logger.info(f"Uploaded report saved as '{state_file}' for the next run's stability brake.")
    return None


def cleanup_previous(session: 'OdooSession', ids_file: str = PREVIOUS_IDS_FILE) -> int:
    """`--cleanup-previous`: archives and unlinks the IDs stored by a full upload whose cleanup
    failed. Refuses (UploadError, nothing deleted) if the file has no `created_ids` or the new
    batch no longer exists in Odoo (`check_new_batch_exists`)."""
    payload = load_previous_ids(ids_file)
    confirm_company(session)
    logger.info(f"Cleaning up {len(payload['ids'])} previous suggestions from '{ids_file}' "
                f"(already archived: {payload.get('archived', False)}).")
    return archive_and_unlink(session, payload['ids'], ids_file, payload.get('created_ids'),
                              already_archived=bool(payload.get('archived', False)))


# --- Entry point ------------------------------------------------------------------------------

def _connect() -> 'OdooSession':
    """Reads the Odoo credentials from Secrets Manager and authenticates. Only for real writes."""
    from pi_odoo import connect
    from pi_secrets import get_odoo_credentials
    return connect(get_odoo_credentials())


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Uploads price suggestions to Odoo (KSA).")
    parser.add_argument('--run_mode', default='partial', choices=['partial', 'full'],
                        help="'partial' only adds; 'full' creates the new batch, then archives and "
                             "deletes the previous one.")
    parser.add_argument('--dry-run', action='store_true',
                        help="Build the records and write them to --dry-run-output. No secrets, no Odoo.")
    parser.add_argument('--dry-run-output', default=DRY_RUN_FILE,
                        help=f"CSV written by --dry-run (default: {DRY_RUN_FILE}).")
    parser.add_argument('--cleanup-previous', action='store_true',
                        help="Archive and delete the previous suggestions stored in --ids-file by a "
                             "full upload whose cleanup failed. Uploads nothing.")
    parser.add_argument('--input', default=REPORT_FILE, help=f"Prediction report (default: {REPORT_FILE}).")
    parser.add_argument('--ids-file', default=PREVIOUS_IDS_FILE,
                        help="Where full mode stores the previous IDs until they are deleted "
                             f"(default: {PREVIOUS_IDS_FILE}, from PI_LOG_DIR or <repo>/../logs).")
    parser.add_argument('--state-file', default=LAST_UPLOADED_REPORT_FILE,
                        help="Where a real upload copies --input, right after its create, for the next run's "
                             f"stability brake (default: {LAST_UPLOADED_REPORT_FILE}). Never written "
                             "by --dry-run.")
    return parser.parse_args(argv)


def main(argv=None, session: 'OdooSession' = None) -> int:
    """Returns the exit code. `session` is injected by tests; otherwise it is created from
    Secrets Manager, and only when the run actually writes."""
    args = parse_args(argv)
    try:
        if args.cleanup_previous:
            if args.dry_run:
                payload = load_previous_ids(args.ids_file)
                logger.info(f"DRY RUN: would archive and delete {len(payload['ids'])} previous "
                            f"suggestions listed in '{args.ids_file}'. Odoo not contacted.")
                return 0
            if session is None:
                session = _connect()
            cleanup_previous(session, args.ids_file)
            return 0

        df_results = pd.read_csv(args.input)
        logger.info(f"Report '{args.input}' loaded: {len(df_results)} rows.")
        no_size = without_size(df_results)
        if no_size.any():
            logger.warning(f"{int(no_size.sum())} products without a size are not uploaded (only the "
                           "margin floor, not a suggestion; fix their size in Odoo): "
                           f"{sorted(df_results.loc[no_size, 'Product_ID'].astype(str).unique())}")
            df_results = df_results[~no_size]
        records, skipped = build_records(df_results)
        check_parse_quality(records, skipped)
        logger.info(f"{len(records)} records ready ({len(skipped)} rows skipped).")

        if args.dry_run:
            pd.DataFrame(records).to_csv(args.dry_run_output, index=False)
            logger.info(f"DRY RUN ({args.run_mode}): {len(records)} records written to "
                        f"'{args.dry_run_output}'. Odoo not contacted.")
            return 0

        logger.info(f"--- RUN MODE: {args.run_mode.upper()} ---")
        if session is None:
            session = _connect()
        upload(session, records, args.run_mode, args.ids_file,
               report_file=args.input, state_file=args.state_file)
        logger.info("Upload finished.")
        return 0
    except StateFileError:
        # Already logged by upload(). Exit 1 so the run is reported, but the suggestions are in Odoo.
        logger.error("Upload finished in Odoo; only the stability-brake state file is missing.")
        return 1
    except Exception:
        logger.exception("CRITICAL ERROR in the price suggestion upload.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
