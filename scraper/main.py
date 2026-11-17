# import pandas as pd
# import time
# import csv
# import os  
# import re
# import sys
# import config
# import argparse
# from selenium import webdriver
# from selenium.webdriver.chrome.service import Service
# from webdriver_manager.chrome import ChromeDriverManager
# from services.ai_service import RelevanceAgent
# from scrapers.amazon_scraper import AmazonScraper
# from scrapers.mumzworld_scraper import MumzworldScraper
# from scrapers.saco_scraper import SacoScraper
# from scrapers.fine_scraper import FineScraper
# from scrapers.gogreen_scraper import GoGreenScraper 
# from scrapers.officesupply_scraper import OfficeSupplyScraper
# from scrapers.aerosense_scraper import AeroSenseScraper
# from log_config import get_logger

# logger = get_logger()

# def main():
#     try:
#         df_instructions = pd.read_csv(config.INSTRUCTIONS_FILE)
#         df_instructions = df_instructions.dropna(subset=['Type of product', 'Sub industry'])
#     except FileNotFoundError:
#         logger.error(f"Error: Instructions file '{config.INSTRUCTIONS_FILE}' was not found.")
#         return

#     write_header = not os.path.exists(config.OUTPUT_CSV_FILE)
    
#     with open(config.OUTPUT_CSV_FILE, 'a', newline='', encoding='utf-8') as f:
#         writer = csv.DictWriter(f, fieldnames=config.CSV_COLUMNS)
#         if write_header:
#             writer.writeheader()

#     logger.info("Downloading and configuring ChromeDriver once...")
#     try:
#         driver_path = ChromeDriverManager().install()
#         logger.info(f"ChromeDriver installed at: {driver_path}")
#     except Exception as e:
#         logger.error(f"Fatal error: Could not install ChromeDriver.", exc_info=True)
#         return

#     for industry_to_scrape in config.TARGET_MAP.keys():
#         logger.info(f"\n=================================================")
#         logger.info(f"  STARTING PROCESS FOR SUBINDUSTRY: '{industry_to_scrape}'")
#         logger.info(f"=================================================\n")

#         df_industry_instructions = df_instructions[df_instructions['Sub industry'] == industry_to_scrape].copy()

#         if df_industry_instructions.empty:
#             logger.warning(f"  -> No products found for subindustry '{industry_to_scrape}'. Skipping to next.")
#             continue

#         ai_agent = RelevanceAgent()
#         scrapers = {
#             'amazon': AmazonScraper(driver_path=driver_path, relevance_agent=ai_agent),
#             'mumzworld': MumzworldScraper(driver_path=driver_path, relevance_agent=ai_agent),
#             'saco': SacoScraper(driver_path=driver_path, relevance_agent=ai_agent),
#             'fine': FineScraper(driver_path=driver_path, relevance_agent=ai_agent),
#             'gogreen': GoGreenScraper(driver_path=driver_path, relevance_agent=ai_agent),
#             'officesupply': OfficeSupplyScraper(driver_path=driver_path, relevance_agent=ai_agent),
#             'aerosense': AeroSenseScraper(driver_path=driver_path)
#         }
        
#         all_found_products = []

#         for index, row in df_industry_instructions.iterrows():
#             sub_industry = row['Sub industry']
#             original_type_of_product = str(row['Type of product'])
#             original_type_of_product_lower = original_type_of_product.lower() 
#             generic_type_of_product = str(row['Generic product type'])
            
#             try:
#                 base_keyword = original_type_of_product_lower.split('-', 1)[1].strip()
#             except IndexError:
#                 base_keyword = original_type_of_product_lower.strip()

#             search_modifiers_val = row.get('Search Modifiers')
#             site_specific_keywords = {}
#             general_modifiers = []

#             if pd.notna(search_modifiers_val):
#                 search_modifiers_str = str(search_modifiers_val)
#                 modifiers = search_modifiers_str.split(';')
#                 for mod in modifiers:
#                     mod = mod.strip()
#                     if ':' in mod:
#                         site, keyword = mod.split(':', 1)
#                         site = site.strip()
#                         keyword = keyword.strip()
#                         if site not in site_specific_keywords:
#                             site_specific_keywords[site] = []
#                         site_specific_keywords[site].append(keyword)
#                     elif mod:
#                         general_modifiers.append(mod)
            
#             general_modifiers_text = " ".join(general_modifiers)
#             search_keyword = f"{base_keyword} {general_modifiers_text}".strip()
            
#             search_mode = 'units' if any(keyword in original_type_of_product_lower for keyword in ['wipes', 'rags', 'microfiber', 'brush']) else 'volume'

#             logger.info(f">> Searching '{base_keyword}' for '{sub_industry}' (Mode: {search_mode})")
#             for site, kword in site_specific_keywords.items():
#                 logger.debug(f"   -> {site.capitalize()} will use the specific term: '{kword}'")

#             sites_to_scrape = config.TARGET_MAP.get(sub_industry, []).copy()
            
#             if base_keyword in config.MUMZWORLD_EXCLUSIONS and 'mumzworld' in sites_to_scrape:
#                 sites_to_scrape.remove('mumzworld')
            
#             if base_keyword in config.SACO_EXCLUSIONS and 'saco' in sites_to_scrape:
#                 sites_to_scrape.remove('saco')

#             for site_name in sites_to_scrape:
#                 scraper = scrapers.get(site_name)
#                 if scraper:
#                     keywords_to_use = site_specific_keywords.get(site_name, [search_keyword])

#                     for keyword_to_use in keywords_to_use:
#                         if site_name in ['fine', 'gogreen', 'officesupply', 'aerosense']:
#                              if site_name not in site_specific_keywords:
#                                 logger.debug(f"   -> Skipping '{site_name}' because no specific tag was provided (e.g. '{site_name}:...')")
#                                 continue
                        
#                         logger.info(f"   -> Searching '{site_name}' with the keyword: '{keyword_to_use}'")
#                         found_products = scraper.scrape(keyword_to_use, search_mode)
                        
#                         b2c_subindustries = ['home', 'automotive', 'pets']
#                         channel_value = 'B2C' if sub_industry.lower() in b2c_subindustries else 'B2B'
                        
#                         for product in found_products:
#                             row_data = {
#                                 'date': time.strftime("%Y-%m-%d"),
#                                 'industry': industry_to_scrape,
#                                 'subindustry': sub_industry,
#                                 'type_of_product': original_type_of_product,
#                                 'generic_product_type': generic_type_of_product,
#                                 'product': product.get('Product'),
#                                 'price_sar': product.get('Price_SAR'),
#                                 'company': product.get('Company'),
#                                 'source': site_name,
#                                 'url': product.get('URL'),
#                                 'unit_of_measurement': product.get('Unit of measurement'),
#                                 'total_quantity': product.get('Total quantity'),
#                                 'channel': channel_value,
#                             }
#                             all_found_products.append(row_data)
#                             logger.info(f"    -> SAVED: {product.get('Product', 'N/A')[:60]}... (Source: {site_name})")
#                 else:
#                     logger.warning(f"   -> Warning: Scraper not found for site '{site_name}'.")

#         if all_found_products:
#             logger.info(f"\n--- Saving {len(all_found_products)} products found for industry '{industry_to_scrape}' ---")
#             try:
#                 with open(config.OUTPUT_CSV_FILE, 'a', newline='', encoding='utf-8') as f:
#                     writer = csv.DictWriter(f, fieldnames=config.CSV_COLUMNS)
#                     writer.writerows(all_found_products)
#                 logger.info(f"  -> Success! Data added to '{config.OUTPUT_CSV_FILE}'.")
#             except IOError as e:
#                 logger.error(f"  -> Error writing to CSV file", exc_info=True)
#         else:
#             logger.info(f"\n--- No products found to save for industry '{industry_to_scrape}'. ---")

#         logger.info(f"\n  -> Process for '{industry_to_scrape}' completed.")
#         logger.info("  -> Resting 10 seconds before next industry...")
#         time.sleep(10)

#     logger.info("\n\n--- COMPLETE PROCESS FOR ALL INDUSTRIES ---")

# if __name__ == "__main__":
#     try:
#         try:
#             sys.stdout.reconfigure(line_buffering=True, write_through=True)
#         except Exception:
#             pass
#         main()
#     except Exception as e:
#         import traceback
#         logger.error(f"[FATAL] Unhandled exception", exc_info=True)
#         traceback.print_exc()

# scraper/main.py (Modifications + Arguments)
import pandas as pd
import time
import csv
import os  
import sys
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
     sys.path.insert(0, project_root)
import re
import config
import argparse
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from services.ai_service import RelevanceAgent
from scrapers.amazon_scraper import AmazonScraper
from scrapers.mumzworld_scraper import MumzworldScraper
from scrapers.saco_scraper import SacoScraper
from scrapers.fine_scraper import FineScraper
from scrapers.gogreen_scraper import GoGreenScraper 
from scrapers.officesupply_scraper import OfficeSupplyScraper
from scrapers.aerosense_scraper import AeroSenseScraper
from log_config import get_logger

logger = get_logger() # <-- ADDED

# --- Arguments --- ADDED ---
parser = argparse.ArgumentParser(description="Runs the scraping of competitor prices.")
parser.add_argument('--analysis_file', default=config.INSTRUCTIONS_FILE,
                    help=f"CSV file with scraping instructions (default: {config.INSTRUCTIONS_FILE})")
parser.add_argument('--output_mode', default='overwrite', choices=['overwrite', 'append'],
                    help="Write mode for the output file ('overwrite' or 'append', default: overwrite)")
parser.add_argument('--output_file', default=config.OUTPUT_CSV_FILE,
                    help=f"Output CSV file for the results (default: {config.OUTPUT_CSV_FILE})")
args = parser.parse_args()
# --- END Arguments ---

# --- Rename argument and config variables --- ADDED ---
INPUT_ANALYSIS_FILE = args.analysis_file
OUTPUT_MODE = args.output_mode
OUTPUT_SCRAPING_FILE = args.output_file
# --- END Rename ---

def main():
    logger.info(f"Starting scraping. Output mode: {OUTPUT_MODE}")
    logger.info(f"Using instructions file: {INPUT_ANALYSIS_FILE}")
    logger.info(f"Output file: {OUTPUT_SCRAPING_FILE}")

    try:
        # Use the analysis file passed as an argument
        df_instructions = pd.read_csv(INPUT_ANALYSIS_FILE)
        df_instructions = df_instructions.dropna(subset=['Type of product', 'Sub industry'])
        logger.info(f"Instructions loaded from '{INPUT_ANALYSIS_FILE}'. {len(df_instructions)} tasks found.")
    except FileNotFoundError:
        logger.error(f"Instructions file '{INPUT_ANALYSIS_FILE}' not found.")
        return
    except Exception as e:
        logger.error(f"Error reading '{INPUT_ANALYSIS_FILE}'.", exc_info=True)
        return

    # --- Determine write mode and header --- MODIFIED ---
    file_exists = os.path.exists(OUTPUT_SCRAPING_FILE)
    write_mode = 'w' if OUTPUT_MODE == 'overwrite' else 'a'
    write_header = (OUTPUT_MODE == 'overwrite') or (not file_exists) # Write the header when overwriting or when the file does not exist in append mode

    # Use 'with open' outside the loop for better file handling
    try:
        with open(OUTPUT_SCRAPING_FILE, write_mode, newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=config.CSV_COLUMNS)
            if write_header:
                writer.writeheader()
                logger.info(f"Writing header to '{OUTPUT_SCRAPING_FILE}'.")
            # --- END Modification ---

            logger.info("Downloading and configuring ChromeDriver...")
            try:
                driver_path = ChromeDriverManager().install()
                logger.info(f"ChromeDriver ready at: {driver_path}")
            except Exception as e:
                logger.critical("Could not install ChromeDriver.", exc_info=True)
                return # Exit if there is no driver

            ai_agent = RelevanceAgent() # Assumes RelevanceAgent already uses Secrets Manager internally

            # Group by subindustry to process the way you did
            for sub_industry_group, df_industry_instructions in df_instructions.groupby('Sub industry'):
                logger.info(f"\n=================================================")
                logger.info(f"  PROCESSING SUBINDUSTRY: '{sub_industry_group}'")
                logger.info(f"=================================================\n")

                if df_industry_instructions.empty:
                    logger.warning(f"There are no instructions for '{sub_industry_group}'.")
                    continue

                # Create scrapers (you can move this outside the loop if you prefer)
                scrapers = {
                    'amazon': AmazonScraper(driver_path=driver_path, relevance_agent=ai_agent),
                    'mumzworld': MumzworldScraper(driver_path=driver_path, relevance_agent=ai_agent),
                    'saco': SacoScraper(driver_path=driver_path, relevance_agent=ai_agent),
                    'fine': FineScraper(driver_path=driver_path, relevance_agent=ai_agent),
                    'gogreen': GoGreenScraper(driver_path=driver_path, relevance_agent=ai_agent),
                    'officesupply': OfficeSupplyScraper(driver_path=driver_path, relevance_agent=ai_agent),
                    'aerosense': AeroSenseScraper(driver_path=driver_path) # Aerosense does not use the AI agent? Verify
                }

                all_found_products_for_group = [] # Accumulate products per group

                for index, row in df_industry_instructions.iterrows():
                    # ... (logic to extract base_keyword, modifiers, search_keyword, search_mode - unchanged) ...
                    sub_industry = row['Sub industry'] # Make sure sub_industry is defined here
                    original_type_of_product = str(row['Type of product'])
                    original_type_of_product_lower = original_type_of_product.lower()
                    generic_type_of_product = str(row['Generic product type'])

                    try:
                        base_keyword = original_type_of_product_lower.split('-', 1)[1].strip()
                    except IndexError:
                        base_keyword = original_type_of_product_lower.strip()

                    search_modifiers_val = row.get('Search Modifiers')
                    site_specific_keywords = {}
                    general_modifiers = []

                    if pd.notna(search_modifiers_val):
                        search_modifiers_str = str(search_modifiers_val)
                        modifiers = search_modifiers_str.split(';')
                        for mod in modifiers:
                            mod = mod.strip()
                            if ':' in mod:
                                site, keyword = mod.split(':', 1)
                                site = site.strip().lower() # ensure lowercase
                                keyword = keyword.strip()
                                if site not in site_specific_keywords:
                                    site_specific_keywords[site] = []
                                site_specific_keywords[site].append(keyword)
                            elif mod:
                                general_modifiers.append(mod)

                    general_modifiers_text = " ".join(general_modifiers)
                    search_keyword = f"{base_keyword} {general_modifiers_text}".strip()

                    search_mode = 'units' if any(kw in original_type_of_product_lower for kw in ['wipes', 'rags', 'microfiber', 'brush']) else 'volume'
                    # ... (end of extraction logic) ...

                    logger.info(f">> Searching '{base_keyword}' for '{sub_industry}' (Mode: {search_mode})")
                    # ... (logic for sites to scrape, exclusions - unchanged) ...
                    sites_to_scrape = config.TARGET_MAP.get(sub_industry, []).copy()

                    if base_keyword in config.MUMZWORLD_EXCLUSIONS and 'mumzworld' in sites_to_scrape:
                        sites_to_scrape.remove('mumzworld')
                        logger.debug(f"Excluding Mumzworld for '{base_keyword}'")

                    if base_keyword in config.SACO_EXCLUSIONS and 'saco' in sites_to_scrape:
                        sites_to_scrape.remove('saco')
                        logger.debug(f"Excluding Saco for '{base_keyword}'")


                    for site_name in sites_to_scrape:
                        scraper = scrapers.get(site_name)
                        if scraper:
                            # Decide which keyword to use: the site-specific one or the general one
                            keywords_to_use = site_specific_keywords.get(site_name, [search_keyword])

                            for keyword_to_use in keywords_to_use:
                                # Logic for B2B sites that require a specific keyword
                                if site_name in ['fine', 'gogreen', 'officesupply', 'aerosense']:
                                     if site_name not in site_specific_keywords and not general_modifiers: # Skip if there is no specific keyword AND NO general modifiers either
                                        logger.warning(f"Skipping '{site_name}' for '{base_keyword}' because no specific keyword or general modifier was provided.")
                                        continue

                                logger.info(f"   -> Searching '{site_name}' with keyword: '{keyword_to_use}'")
                                try:
                                    found_products = scraper.scrape(keyword_to_use, search_mode)
                                except Exception as scrape_error:
                                     logger.error(f"Error while scraping '{site_name}' with keyword '{keyword_to_use}'.", exc_info=True)
                                     found_products = [] # Continue with the next site/keyword

                                b2c_subindustries = ['home', 'automotive', 'pets'] # Use lowercase for comparison
                                channel_value = 'B2C' if sub_industry.lower() in b2c_subindustries else 'B2B'

                                for product in found_products:
                                    row_data = {
                                        # ... (data mapping - unchanged) ...
                                        'date': time.strftime("%Y-%m-%d"),
                                        'industry': df_industry_instructions.iloc[0]['Industry'], # Get it from the first row of the group
                                        'subindustry': sub_industry,
                                        'type_of_product': original_type_of_product,
                                        'generic_product_type': generic_type_of_product,
                                        'product': product.get('Product'),
                                        'price_sar': product.get('Price_SAR'),
                                        'company': product.get('Company'),
                                        'source': site_name,
                                        'url': product.get('URL'),
                                        'unit_of_measurement': product.get('Unit of measurement'),
                                        'total_quantity': product.get('Total quantity'),
                                        'channel': channel_value,
                                    }
                                    all_found_products_for_group.append(row_data)
                                    logger.debug(f"    -> Product added: {product.get('Product', 'N/A')[:60]}...") # Changed to DEBUG for less verbosity
                        else:
                            logger.warning(f"   -> No scraper found for '{site_name}'.")

                # Write the results of the current group to the CSV file
                if all_found_products_for_group:
                    logger.info(f"Writing {len(all_found_products_for_group)} products found for '{sub_industry_group}' to '{OUTPUT_SCRAPING_FILE}'...")
                    writer.writerows(all_found_products_for_group)
                else:
                    logger.info(f"No products found to save for '{sub_industry_group}'.")

                logger.info(f"Process for '{sub_industry_group}' completed.")
                logger.info("Resting 5 seconds before the next subindustry...") # Reduced rest time?
                time.sleep(5)

    except IOError as e:
        logger.critical(f"Fatal error opening/writing '{OUTPUT_SCRAPING_FILE}'.", exc_info=True)
    except Exception as e:
        logger.critical("Unexpected error in the main scraping process.", exc_info=True)

    logger.info("\n\n--- SCRAPING PROCESS COMPLETED ---")


if __name__ == "__main__":
    try:
        # sys.stdout.reconfigure is no longer needed with properly configured logging
        main()
    except Exception as e:
        # Final catch in case something fails outside main()
        # logger may not be initialized here if it fails very early
        print(f"[FATAL] Unhandled exception: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)