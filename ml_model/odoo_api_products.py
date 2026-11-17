import xmlrpc.client
import os
from botocore.exceptions import ClientError
import pandas as pd
import boto3
import json
import base64
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

# Output file name
OUTPUT_CSV_FILE = 'ml_model/wayakit_products.csv'

# --- 2. AUTHENTICATION WITH ODOO ---
try:
    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, API_TOKEN, {})
    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
    logger.info(f"🔑 Authentication successful. User ID (uid): {uid}")
except Exception as e:
    logger.error(f"🔥 Authentication ERROR", exc_info=True)
    exit()

# --- 3. QUERY DEFINITION ---
MODEL_NAME = 'product.master'

# Search filters (kept from your original script)
products_to_exclude = [
    'F12-Other consumables for FM',
    'P11-Other consumables for pets',
    'H13-Other products, consumables or kits for homes',
    'C5-Other transportation products or items'
]

domain = [
    ['status', '=', 'active'],
    ['subindustry_id.name', 'not in', ['Maritime', 'Defense']],
    ['type_of_product_id.name', 'not in', products_to_exclude]
]

# Fields to fetch from the 'product.master' model
fields_to_get = [
    'product_id',
    'product_name',
    'label_product_name',
    'presentation',
    'volume_liters',
    'pack_quantity_units',
    'type_of_product_id',
    'category',
    'generic_product_type',
    'subindustry_id',
    'industry_id',
    'bottle_cost',
    'label_cost',
    'liquid_cost',
    'microfibers_cost',
    'plastic_bag_cost',
    'labor_cost',
    'shipping_cost',
    'other_costs',
    'unit_cost_sar',
    'create_date',
]

logger.info(f"\n🔎 Searching records in model '{MODEL_NAME}'...")

# --- 4. QUERY EXECUTION ---
try:
    records = models.execute_kw(
        ODOO_DB, uid, API_TOKEN,
        MODEL_NAME,
        'search_read',
        [domain],
        {'fields': fields_to_get}
    )
    logger.info(f"👍 Success. Found {len(records)} records.")

except Exception as e:
    logger.error(f"🔥 Critical ERROR during the query", exc_info=True)
    exit()

# --- 5. DATA PROCESSING AND EXPORT ---
if records:
    # Convert the records to a pandas DataFrame
    df = pd.DataFrame(records)
    
    # Odoo returns Many2one fields as a list [id, 'name'].
    # This function extracts only the name.
    df['type_of_product_id'] = df['type_of_product_id'].apply(lambda x: x[1] if isinstance(x, list) and len(x) > 1 else None)
    df['subindustry_id'] = df['subindustry_id'].apply(lambda x: x[1] if isinstance(x, list) else None)
    df['industry_id'] = df['industry_id'].apply(lambda x: x[1] if isinstance(x, list) else None)

    # Mapping of Odoo field names to the desired column names in the CSV
    column_mapping = {
        'create_date': 'create_date',
        'product_id': 'Product_ID',
        'product_name': 'Product_Name',
        'label_product_name': 'Label_Product_Name',
        'presentation': 'Presentation',
        'volume_liters': 'Volume_Liters',
        'pack_quantity_units': 'Pack_quantity_Units',
        'type_of_product_id': 'Type_of_product',
        'category': 'Category',
        'generic_product_type': 'Generic product type',
        'subindustry_id': 'SubIndustry',
        'industry_id': 'Industry',
        'bottle_cost': 'Bottle',
        'label_cost': 'Label',
        'liquid_cost': 'Liquid',
        'microfibers_cost': 'Microfibers',
        'plastic_bag_cost': 'Plastic bag',
        'labor_cost': 'Labor',
        'shipping_cost': 'Shipping',
        'other_costs': 'Other costs',
        'unit_cost_sar': 'Unit_cost_SAR'
    }
    
    df_renamed = df.rename(columns=column_mapping)

    # Define the final column order for the CSV file
    final_columns_order = [
        'create_date', 'Product_ID', 'Product_Name', 'Label_Product_Name', 'Presentation', 
        'Volume_Liters', 'Pack_quantity_Units', 'Type_of_product', 'Category', 
        'Generic product type', 'SubIndustry', 'Industry', 'Bottle', 'Label', 
        'Liquid', 'Microfibers', 'Plastic bag', 'Labor', 'Shipping', 
        'Other costs', 'Unit_cost_SAR'
    ]

    # Reorder the DataFrame to match the requested order
    df_final = df_renamed[final_columns_order]
    
    # Save the final DataFrame to a CSV file
    df_final.to_csv(OUTPUT_CSV_FILE, index=False, encoding='utf-8-sig')
    logger.info(f"\n✅ Success! The file '{OUTPUT_CSV_FILE}' has been created with the final result.")

    logger.info("\n📊 Preview of the first 5 rows of the result:")
    logger.info(f"\n{df_final.head()}")
    
else:
    logger.warning("🤷 No records matching the filter criteria were found.")

logger.info("\nProcess completed.")