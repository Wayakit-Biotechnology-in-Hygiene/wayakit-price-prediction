import xmlrpc.client
import pandas as pd
import numpy as np
import boto3
import json
import os
import base64
from botocore.exceptions import ClientError
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_config import get_logger

logger = get_logger()

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
        logger.error("ERROR: Essential Odoo secrets retrieved from AWS Secrets Manager are missing.")
        exit()

    logger.info(f"Secrets loaded successfully from AWS Secrets Manager for DB: {ODOO_DB}")

except Exception as e:
    logger.error(f"CRITICAL ERROR: The secrets could not be loaded.", exc_info=True)
    exit()

OUTPUT_CSV_FILE = 'ml_model/wayakit_cotizations.csv'

try:
    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, API_TOKEN, {})
    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
    logger.info(f"Authentication successful. User ID (uid): {uid}")
except Exception as e:
    logger.error(f"Authentication ERROR", exc_info=True)
    exit()

MODEL_NAME = 'sale.order.line'

domain = [
    ['order_id.state', '=', 'sale'],
    ['product_uom_qty', '>', 0],
    ['price_subtotal', '>', 0],
    ['name', 'like', '[FP-%'],
    ['order_id.date_order', '>=', '2025-01-01 00:00:00'],
    ['order_id.date_order', '<=', '2025-12-31 23:59:59'],
]

fields_to_get = [
    'name',
    'product_uom_qty',
    'price_subtotal',
]

logger.info(f"\nSearching records in '{MODEL_NAME}' for year 2025...")

logger.info(f"\nSearching records in '{MODEL_NAME}' with description filter...")

try:
    records = models.execute_kw(
        ODOO_DB, uid, API_TOKEN,
        MODEL_NAME,
        'search_read',
        [domain],
        {'fields': fields_to_get}
    )
    logger.info(f"Success. Found {len(records)} order lines matching the format.")

except Exception as e:
    logger.error(f"Critical ERROR during the query", exc_info=True)
    exit()

# --- Data processing (MODIFIED SECTION) ---
if records:
    df = pd.DataFrame(records)
    
    column_mapping = {
        'name': 'Description',
        'product_uom_qty': 'Quantity',
        'price_subtotal': 'Subtotal'
    }
    df_processed = df.rename(columns=column_mapping)
    
    df_processed['Product_ID'] = df_processed['Description'].str.extract(r'\[(.*?)\]')
    
    df_processed['Description'] = df_processed['Description'].str.replace(r'\[.*?\]\s*', '', regex=True)
    
    df_processed['approved_quote_price'] = df_processed['Subtotal'] / df_processed['Quantity']
    
    # --- 3. NEW: We update the final column order ---
    final_columns = [
        'Product_ID',
        'Description',
        'approved_quote_price',
    ]
    df_to_export = df_processed[final_columns]
    
    # Save the result to a new CSV file
    df_to_export.to_csv(OUTPUT_CSV_FILE, index=False, encoding='utf-8-sig')
    logger.info(f"\n✅ Success! The file '{OUTPUT_CSV_FILE}' has been created.")

    logger.info("\n📊 Preview of the first 5 rows of the final result:")
    logger.info(f"\n{df_to_export.head()}")
    
else:
    logger.warning("No records matching all the filter criteria were found.")

logger.info("\nProcess completed.")