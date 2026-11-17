import pandas as pd
import os
import sys
import argparse
# Add the root directory to the path so log_config can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_config import get_logger

logger = get_logger()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Prepares the list of Wayakit products to predict.")
    parser.add_argument('--run_mode', default='full', choices=['partial', 'full'],
                        help="Sets the run mode: 'partial' (new products only) or 'full' (all products).")
    return parser.parse_args(argv)


def build_prediction_list(run_mode='full'):
    """Writes the catalog rows whose type of product has competitor data.

    Errors propagate (missing input file, missing 'Type_of_product' column...): the `__main__`
    block logs them and exits with 1. Before F2 they were logged and the script exited with 0.
    """
    volumetric_file = 'ml_model/competitor_volumetric_processed.csv'
    unit_file = 'ml_model/competitor_unit_processed.csv'
    # Decides which product catalog to use based on the --run_mode argument
    if run_mode == 'partial':
        master_catalog = 'ml_model/wayakit_new_products_temp.csv'
        logger.info(f"PARTIAL mode detected. Using catalog of NEW products: '{master_catalog}'")
    else:
        master_catalog = 'ml_model/wayakit_products.csv'
        logger.info(f"FULL mode detected. Using FULL product catalog: '{master_catalog}'")
    output_file = 'ml_model/wayakit_products_to_predict_odoo.csv'

    required_files = [volumetric_file, unit_file, master_catalog]
    missing = [f for f in required_files if not os.path.exists(f)]
    if missing:
        raise FileNotFoundError(f"Required input files not found: {missing}. Run the previous pipeline steps first.")

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


if __name__ == "__main__":
    try:
        build_prediction_list(parse_args().run_mode)
    except Exception:
        logger.exception("Preparing the Wayakit products to predict failed.")
        sys.exit(1)