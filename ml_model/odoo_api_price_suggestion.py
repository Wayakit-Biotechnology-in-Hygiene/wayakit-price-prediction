import xmlrpc.client
from botocore.exceptions import ClientError
import pandas as pd
import os
from datetime import datetime
import boto3
import json
import base64
import sys
import argparse 
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_config import get_logger

logger = get_logger()

MODEL_NAME = 'product.price.suggestion'
HISTORY_MODEL_NAME = 'product.price.suggestion.history'

def get_secret(secret_name, region_name="eu-north-1"):
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=region_name
    )
    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
    except ClientError as e:
        logger.error(f"Error getting the secret", exc_info=True)
        raise e 
    else:
        if 'SecretString' in get_secret_value_response:
            secret = get_secret_value_response['SecretString']
            return json.loads(secret)
        else:
            decoded_binary_secret = base64.b64decode(get_secret_value_response['SecretBinary'])
            return json.loads(decoded_binary_secret)

SECRET_NAME = "prod/wayakit-app" 
AWS_REGION = "eu-north-1" 
try:
    secrets = get_secret(SECRET_NAME, AWS_REGION)
    ODOO_URL = secrets.get('ODOO_URL')
    ODOO_DB = secrets.get('ODOO_DB')
    ODOO_USERNAME = secrets.get('ODOO_USERNAME')
    API_TOKEN = secrets.get('ODOO_API_TOKEN')
    if not all([ODOO_URL, ODOO_DB, ODOO_USERNAME, API_TOKEN]):
        logger.error("ERROR: Essential Odoo secrets are missing.")
        exit()
    logger.info(f"Secrets loaded successfully for DB: {ODOO_DB}")
except Exception as e:
    logger.error(f"CRITICAL ERROR: The secrets could not be loaded.", exc_info=True)
    exit()

try:
    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, API_TOKEN, {})
    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
    logger.info(f"Authentication successful. User ID (uid): {uid}")
except Exception as e:
    logger.error(f"Authentication ERROR", exc_info=True)
    exit()

parser = argparse.ArgumentParser(description="Uploads price suggestions to Odoo.")
parser.add_argument('--run_mode', default='partial', choices=['partial', 'full'],
                    help="Sets the run mode: 'partial' (only adds) or 'full' (archives, deletes and adds).")
args = parser.parse_args()

csv_file_path = 'ml_model/wayakit_prediction_report.csv'
try:
    df_results = pd.read_csv(csv_file_path)
    logger.info(f"CSV file '{csv_file_path}' loaded. Found {len(df_results)} rows.")
except FileNotFoundError:
    logger.error(f"ERROR: The file '{csv_file_path}' was not found. Nothing will be uploaded to Odoo.")
    exit()

all_records_data = []
for index, row in df_results.iterrows():
    try:
        volume_str = str(row['volume']).split(' ')[0]
        volume_float = float(volume_str)

        record_data = {
            'product_id_str': row['Product_ID'],
            'product_type': row['product_type'],
            'generic_product_type': row['generic_product_type'],
            'subindustry': row['subindustry'],
            'industry': row['industry'],
            'volume_units': volume_float,
            'production_cost': float(row['cost_per_unit']),
            'suggested_price': float(row['predicted_price']),
            'profit': float(row['porcentaje_de_ganancia']),
            'product_name': row['product_name'],
            'predicted_price_per_unit': float(row['predicted_price_per_unit']),
            'model_confidence': float(row['model_confidence']),
            'market_min_found': float(row['market_min_found']),
            'market_max_found': float(row['market_max_found']),
            'competitors_count': int(row['competitors_count']),
        }
        all_records_data.append(record_data)
    except Exception as e:
        logger.warning(f"Error processing row {index+1} (Product ID: {row.get('Product_ID', 'N/A')}). Skipping record.", exc_info=True)

if not all_records_data:
    logger.warning("There are no valid records to upload from the CSV. Process finished.")
    exit()
if args.run_mode == 'full':
    logger.info("--- RUN MODE: FULL ---")
    logger.info(f"Starting archiving from '{MODEL_NAME}' to '{HISTORY_MODEL_NAME}'...")
    try:
        existing_ids = models.execute_kw(
            ODOO_DB, uid, API_TOKEN,
            MODEL_NAME, 'search', [[('id', '!=', 0)]] 
        )
        
        if existing_ids:
            logger.info(f"Found {len(existing_ids)} records to archive.")
            
            fields_to_archive = [
                'product_id_str', 'product_type', 'generic_product_type', 
                'subindustry', 'industry', 'volume_units', 
                'production_cost', 'suggested_price', 'profit', 'create_date',
                'product_name', 'predicted_price_per_unit', 'model_confidence',
                'market_min_found', 'market_max_found', 'competitors_count'
            ]
            old_records = models.execute_kw(
                ODOO_DB, uid, API_TOKEN,
                MODEL_NAME, 'read', [existing_ids], {'fields': fields_to_archive}
            )
            
            records_to_create_in_history = []
            for record in old_records:
                record.pop('id', None)
                if 'create_date' in record:
                    record['original_create_date'] = record.pop('create_date')
                records_to_create_in_history.append(record)
            
            logger.info(f"Creating {len(records_to_create_in_history)} records in history '{HISTORY_MODEL_NAME}'...")
            models.execute_kw(
                ODOO_DB, uid, API_TOKEN,
                HISTORY_MODEL_NAME, 'create', [records_to_create_in_history]
            )
            logger.info("Records archived to history successfully!")

            logger.info(f"Deleting {len(existing_ids)} old records from '{MODEL_NAME}'...")
            models.execute_kw(
                ODOO_DB, uid, API_TOKEN,
                MODEL_NAME, 'unlink', [existing_ids]
            )
            logger.info("Old records deleted successfully!")

        else:
            logger.info("No existing records found. No archiving required.")

    except Exception as e:
        logger.error(f"CRITICAL ERROR during archiving/deletion.", exc_info=True)
        logger.error("Aborting to prevent a duplicate upload. The suggestions table may not be clean.")
        exit(1)

else:
    logger.info("--- RUN MODE: PARTIAL ---")
    logger.info("Only the new records will be added (without deleting).")

logger.info(f"\n🚀 Sending {len(all_records_data)} new records to '{MODEL_NAME}' in a single call...")
try:
    new_record_ids = models.execute_kw(
        ODOO_DB, uid, API_TOKEN,
        MODEL_NAME,
        'create',
        [all_records_data]
    )
    
    logger.info(f"Bulk upload completed successfully!")
    logger.info(f"   Created {len(new_record_ids)} new records.")

except Exception as e:
    logger.error(f"CRITICAL ERROR during the bulk upload", exc_info=True)
    logger.error("   The operation failed. No record was created in this batch.")
    
logger.info("\nProcess finished.")