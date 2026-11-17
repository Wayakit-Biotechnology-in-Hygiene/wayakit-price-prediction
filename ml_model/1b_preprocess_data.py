    
import pandas as pd
import os
import sys
import argparse
# Add the root directory to the path so log_config can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_config import get_logger

logger = get_logger()

# --- 2. WE ADD THE ARGUMENT PARSER ---
parser = argparse.ArgumentParser(description="Prepares the list of Wayakit products to predict.")
parser.add_argument('--run_mode', default='full', choices=['partial', 'full'],
                    help="Sets the run mode: 'partial' (new products only) or 'full' (all products).")
args = parser.parse_args()
# --- END OF CHANGES 2 ---

def build_prediction_list():
    volumetric_file = 'ml_model/competitor_volumetric_processed.csv'
    unit_file = 'ml_model/competitor_unit_processed.csv'
    # Decides which product catalog to use based on the --run_mode argument
    if args.run_mode == 'partial':
        master_catalog = 'ml_model/wayakit_new_products_temp.csv'
        logger.info(f"PARTIAL mode detected. Using catalog of NEW products: '{master_catalog}'")
    else:
        master_catalog = 'ml_model/wayakit_products.csv'
        logger.info(f"FULL mode detected. Using FULL product catalog: '{master_catalog}'")
    # --- END OF CHANGES 3 ---
    output_file = 'ml_model/wayakit_products_to_predict_odoo.csv'

    required_files = [volumetric_file, unit_file, master_catalog]
    for file_path in required_files:
        if not os.path.exists(file_path):
            logger.error(f"File '{file_path}' not found.")
            logger.warning("Solution: Make sure you have executed the main notebook first.")
            return

    try:
        logger.info("STEP 1: Identifying products the model knows...")
        df_vol_comp = pd.read_csv(volumetric_file)
        df_unit_comp = pd.read_csv(unit_file)
        known_product_types = set(list(df_vol_comp['type_of_product'].unique()) + list(df_unit_comp['type_of_product'].unique()))
        logger.info(f"Identified {len(known_product_types)} product types that the model can predict.")

        logger.info("STEP 2: Loading master product catalog...")
        df_catalogue = pd.read_csv(master_catalog, encoding='utf-8-sig')
        logger.info(f"Loaded {len(df_catalogue)} products from catalog.")

        logger.info("STEP 3: Filtering catalog to find compatible products...")
        df_catalogue['Type_of_product'] = df_catalogue['Type_of_product'].str.strip()
        df_predictable = df_catalogue[df_catalogue['Type_of_product'].isin(known_product_types)].copy()
        logger.info(f"Found {len(df_predictable)} compatible products.")
        
        logger.info(f"STEP 4: Saving new file as '{output_file}'...")

        df_predictable.to_csv(output_file, index=False)
        
        logger.info("Process completed!")
        logger.info(f"File '{output_file}' is ready with filtered products and all original columns.")

    except FileNotFoundError:
        logger.error("One of the base files does not exist. Re-run the main notebook.", exc_info=True)
    except KeyError as e:
        logger.error(f"Column 'Type_of_product' not found: {e}", exc_info=True)
        logger.warning("Solution: Check that this column exists in your CSV files.")
    except Exception as e:
        logger.error("Unexpected error occurred during process", exc_info=True)

if __name__ == "__main__":
    build_prediction_list()