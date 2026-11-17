import xmlrpc.client
import os
import pandas as pd
import argparse
import boto3
import json
import base64
from botocore.exceptions import ClientError
import sys
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
from log_config import get_logger

logger = get_logger()

def get_secret(secret_name, region_name="eu-north-1"):
    session = boto3.session.Session()
    client = session.client(service_name='secretsmanager', region_name=region_name)
    try:
        get_secret_value_response = client.get_secret_value(SecretId=secret_name)
    except ClientError as e:
        logger.error(f"Error getting the secret '{secret_name}': {e}", exc_info=True)
        raise e
    else:
        if 'SecretString' in get_secret_value_response:
            secret = get_secret_value_response['SecretString']
            return json.loads(secret)
        else:
            decoded_binary_secret = base64.b64decode(get_secret_value_response['SecretBinary'])
            return json.loads(decoded_binary_secret)


parser = argparse.ArgumentParser(description="Generates the list of products/subindustries for scraping.")
parser.add_argument('--input_odoo_products_file', default='ml_model/wayakit_products.csv',
                    help="Input CSV file with Odoo products (e.g. wayakit_products.csv or wayakit_new_products_temp.csv).")
parser.add_argument('--output_analysis_file', default='scraper/analysis-odoo.csv',
                    help="Output CSV file for the scraper (e.g. analysis-odoo.csv or analysis-odoo_partial.csv).")
parser.add_argument('--modifiers_file', default='scraper/modifiers_mapping.csv',
                    help="CSV file with the search modifiers mapping.")
args = parser.parse_args()

SECRET_NAME = "prod/wayakit-app"
AWS_REGION = "eu-north-1"
try:
    secrets = get_secret(SECRET_NAME, AWS_REGION)
    ODOO_URL = secrets.get('ODOO_URL')
    ODOO_DB = secrets.get('ODOO_DB')
    ODOO_USERNAME = secrets.get('ODOO_USERNAME')
    API_TOKEN = secrets.get('ODOO_API_TOKEN')
    if not all([ODOO_URL, ODOO_DB, ODOO_USERNAME, API_TOKEN]):
        logger.error("Essential retrieved Odoo secrets are missing.")
        exit(1)
    logger.info(f"Secrets loaded for DB: {ODOO_DB}")
except Exception as e:
    logger.critical(f"The secrets could not be loaded.", exc_info=True)
    exit(1)

INPUT_ODOO_FILE = args.input_odoo_products_file
OUTPUT_ANALYSIS_FILE = args.output_analysis_file
MODIFIERS_FILE = args.modifiers_file

try:
    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, API_TOKEN, {})
    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
    logger.info(f"Odoo authentication successful. UID: {uid}")
except Exception as e:
    logger.error(f"Odoo authentication error.", exc_info=True)
    exit(1)

logger.info(f"Reading Odoo products file: '{INPUT_ODOO_FILE}'")
try:
    df_odoo_products = pd.read_csv(INPUT_ODOO_FILE)
    logger.info(f"Read {len(df_odoo_products)} products from '{INPUT_ODOO_FILE}'.")

    column_mapping = {
        'Type_of_product': 'Type of product',
        'SubIndustry': 'Sub industry',
        'Industry': 'Industry',
        'Generic product type': 'Generic product type'
    }
    df_renamed = df_odoo_products.rename(columns=column_mapping)

    columns_for_uniqueness = [
        'Type of product', 'Sub industry', 'Industry', 'Generic product type'
    ]
    missing_cols = [col for col in columns_for_uniqueness if col not in df_renamed.columns]
    if missing_cols:
        logger.error(f"Required columns are missing in '{INPUT_ODOO_FILE}': {missing_cols}")
        exit(1)

    df_unique = df_renamed.drop_duplicates(subset=columns_for_uniqueness)
    logger.info(f"Unique combinations found: {len(df_unique)}")

    df_final = df_unique.copy()
    try:
        df_modifiers = pd.read_csv(MODIFIERS_FILE)
        if 'Type of product' in df_modifiers.columns and 'Search Modifiers' in df_modifiers.columns:
            df_final['Type of product'] = df_final['Type of product'].astype(str)
            df_modifiers['Type of product'] = df_modifiers['Type of product'].astype(str)

            df_final = pd.merge(df_final, df_modifiers[['Type of product', 'Search Modifiers']], on='Type of product', how='left')
            df_final['Search Modifiers'] = df_final['Search Modifiers'].fillna('')
            logger.info("Merge with Search Modifiers successful.")
        else:
            logger.warning(f"The file '{MODIFIERS_FILE}' must have 'Type of product' and 'Search Modifiers'. No modifiers will be added.")
            df_final['Search Modifiers'] = ''
    except FileNotFoundError:
        logger.warning(f"'{MODIFIERS_FILE}' not found. No modifiers will be added.")
        df_final['Search Modifiers'] = ''
    except Exception as e:
        logger.error(f"Error processing modifiers file '{MODIFIERS_FILE}'.", exc_info=True)
        df_final['Search Modifiers'] = ''

    final_columns = [
        'Type of product', 'Sub industry', 'Industry', 'Generic product type', 'Search Modifiers'
    ]
    final_columns_exist = [col for col in final_columns if col in df_final.columns]
    df_to_export = df_final[final_columns_exist] 

    df_to_export.to_csv(OUTPUT_ANALYSIS_FILE, index=False, encoding='utf-8-sig')
    logger.info(f"Success! Analysis file generated: '{OUTPUT_ANALYSIS_FILE}'")
    logger.info("Preview:")
    logger.info("\n" + df_to_export.head().to_string()) 

except FileNotFoundError:
    logger.error(f"Input file '{INPUT_ODOO_FILE}' not found.")
    exit(1)
except Exception as e:
    logger.critical(f"Unexpected error processing products.", exc_info=True)
    exit(1)

logger.info("Process completed.")