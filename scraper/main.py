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

# scraper/main.py: competitor scraping (Apify for Amazon and Noon since F6; Selenium for Mumzworld,
# SACO and the B2B stores). Writes to a temporary CSV and replaces the final one only when the run
# succeeds; exits with 1 on any fatal error (relevance AI or Apify) or when any Apify keyword
# failed; always writes a coverage report.
import argparse
import csv
import os
import shutil
import signal
import sys
import time

import pandas as pd

SCRAPER_DIR = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(SCRAPER_DIR, '..'))
# Same order as "python scraper/main.py" (repo root first, then scraper/), also when imported
# from the tests.
if SCRAPER_DIR not in sys.path:
    sys.path.insert(0, SCRAPER_DIR)
if project_root not in sys.path:
    sys.path.insert(0, project_root)
import config
from services.ai_service import RelevanceAgent, RelevanceAgentError, RelevanceAgentFatalError
from scrapers.amazon_apify import AmazonApifyScraper
from scrapers.noon_apify import NoonApifyScraper
from scrapers.mumzworld_scraper import MumzworldScraper
from scrapers.saco_scraper import SacoScraper
from scrapers.fine_scraper import FineScraper
from scrapers.gogreen_scraper import GoGreenScraper
from scrapers.officesupply_scraper import OfficeSupplyScraper
from scrapers.aerosense_scraper import AeroSenseScraper
from services.apify_client import ApifyError, ApifyFatalError, preflight as apify_preflight_default
from services.browser import BrowserSession
from log_config import get_logger

logger = get_logger()

DEFAULT_COVERAGE_FILE = 'scraper/coverage_report.csv'
COVERAGE_COLUMNS = [
    'subindustry', 'type_of_product', 'site', 'keyword',
    'found', 'relevant', 'rows_written', 'ai_failures', 'error',
]
B2C_SUBINDUSTRIES = ['home', 'automotive', 'pets']  # compared in lowercase
# B2B sites are only searched with a site-specific keyword or general modifiers.
KEYWORD_REQUIRED_SITES = ['fine', 'gogreen', 'officesupply', 'aerosense']
UNITS_MODE_KEYWORDS = ['wipes', 'rags', 'microfiber', 'brush']
# Scrapers that do not use the relevance agent: everything they return counts as found and relevant.
SITES_WITHOUT_AI = {'aerosense'}
# Sites scraped through Apify: failed keywords above config.APIFY_MAX_FAILED_KEYWORD_RATE make the run exit 1.
APIFY_SITES = ('amazon', 'noon')
SECONDS_BETWEEN_SUBINDUSTRIES = 5
# A site whose first searches all failed is broken (blocked, moved, changed): the run stops at
# once instead of scraping for hours without it (Mafer, 2026-11-25: fix the site, then rerun).
FAILED_SEARCHES_TO_ABORT = 3


class ScrapingAborted(Exception):
    """A fatal error (relevance AI, Apify, or a site failing every search) that must stop the
    whole run."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Runs the scraping of competitor prices.")
    parser.add_argument('--analysis_file', default=config.INSTRUCTIONS_FILE,
                        help=f"CSV file with scraping instructions (default: {config.INSTRUCTIONS_FILE})")
    parser.add_argument('--output_mode', default='overwrite', choices=['overwrite', 'append'],
                        help="Write mode for the output file ('overwrite' or 'append', default: overwrite)")
    parser.add_argument('--output_file', default=config.OUTPUT_CSV_FILE,
                        help=f"Output CSV file for the results (default: {config.OUTPUT_CSV_FILE})")
    parser.add_argument('--coverage_file', default=DEFAULT_COVERAGE_FILE,
                        help=f"Coverage report per product, site and keyword (default: {DEFAULT_COVERAGE_FILE})")
    return parser.parse_args(argv)


# --- Pure helpers (no I/O) -----------------------------------------------------------------

def parse_instruction(row):
    """Search plan for one instructions row (a pandas Series or a dict). Same rules as before:
    base keyword after the first '-', 'site:keyword' modifiers per site, the rest appended to
    the keyword, and units mode for wipes, rags, microfiber and brushes."""
    original_type_of_product = str(row['Type of product'])
    original_type_of_product_lower = original_type_of_product.lower()

    try:
        base_keyword = original_type_of_product_lower.split('-', 1)[1].strip()
    except IndexError:
        base_keyword = original_type_of_product_lower.strip()

    search_modifiers_val = row.get('Search Modifiers')
    site_specific_keywords = {}
    general_modifiers = []

    if pd.notna(search_modifiers_val):
        modifiers = str(search_modifiers_val).split(';')
        for mod in modifiers:
            mod = mod.strip()
            if ':' in mod:
                site, keyword = mod.split(':', 1)
                site = site.strip().lower()
                keyword = keyword.strip()
                site_specific_keywords.setdefault(site, []).append(keyword)
            elif mod:
                general_modifiers.append(mod)

    general_modifiers_text = " ".join(general_modifiers)
    search_keyword = f"{base_keyword} {general_modifiers_text}".strip()
    search_mode = 'units' if any(kw in original_type_of_product_lower for kw in UNITS_MODE_KEYWORDS) else 'volume'
    return {
        'type_of_product': original_type_of_product,
        'generic_product_type': str(row['Generic product type']),
        'base_keyword': base_keyword,
        'search_keyword': search_keyword,
        'site_specific_keywords': site_specific_keywords,
        'general_modifiers': general_modifiers,
        'search_mode': search_mode,
    }


def sites_for(sub_industry, base_keyword):
    """Sites to scrape for a subindustry, minus the Mumzworld and SACO exclusions.
    The TARGET_MAP lookup is case-sensitive on purpose (C06 is a separate decision)."""
    sites_to_scrape = config.TARGET_MAP.get(sub_industry, []).copy()
    if base_keyword in config.MUMZWORLD_EXCLUSIONS and 'mumzworld' in sites_to_scrape:
        sites_to_scrape.remove('mumzworld')
    if base_keyword in config.SACO_EXCLUSIONS and 'saco' in sites_to_scrape:
        sites_to_scrape.remove('saco')
    return sites_to_scrape


def channel_for(sub_industry):
    return 'B2C' if sub_industry.lower() in B2C_SUBINDUSTRIES else 'B2B'


def build_row_data(product, industry, sub_industry, type_of_product, generic_type_of_product,
                   site_name, channel, date):
    """One output row; keys are exactly ``config.CSV_COLUMNS`` (DictWriter raises on extras).

    NUL characters are removed from text values: the csv module cannot write them and one
    scraped title with a NUL would otherwise abort the whole run. Any other value is unchanged.
    """
    row = {
        'date': date,
        'industry': industry,
        'subindustry': sub_industry,
        'type_of_product': type_of_product,
        'generic_product_type': generic_type_of_product,
        'product': product.get('Product'),
        'price_sar': product.get('Price_SAR'),
        'company': product.get('Company'),
        'source': site_name,
        'url': product.get('URL'),
        'unit_of_measurement': product.get('Unit of measurement'),
        'total_quantity': product.get('Total quantity'),
        'channel': channel,
        # F6: only the Apify adapters return them; other sites and older rows get the defaults.
        'seller': product.get('Seller') or '',
        'origin': product.get('Origin') or 'unknown',
    }
    return {key: strip_nul(value) for key, value in row.items()}


def strip_nul(value):
    """``value`` without '\\x00' if it is a string; any other type is returned as is."""
    return value.replace('\x00', '') if isinstance(value, str) else value


def stats_delta(before, after):
    """Counter increments between two ``RelevanceAgent.stats()`` snapshots."""
    return {key: after.get(key, 0) - before.get(key, 0) for key in after}


def coverage_entry(subindustry, type_of_product, site, keyword, delta, rows_written, error='',
                   uses_ai=True):
    """Coverage of one ``scrape()`` call. ``found`` and ``relevant`` are the titles the AI
    evaluated and accepted during the call; for a site without AI both equal ``rows_written``.
    ``ai_failures`` adds relevance and wipes failures."""
    if uses_ai:
        found = delta.get('evaluated', 0)
        relevant = delta.get('relevant', 0)
    else:
        found = relevant = rows_written
    return {
        'subindustry': subindustry,
        'type_of_product': type_of_product,
        'site': site,
        'keyword': keyword,
        'found': found,
        'relevant': relevant,
        'rows_written': rows_written,
        'ai_failures': delta.get('failures', 0) + delta.get('wipes_failures', 0),
        'error': error or '',
    }


def build_coverage_report(entries):
    """One row per (subindustry, type_of_product, site, keyword), in first-seen order: numeric
    columns are summed and distinct errors joined with ';'."""
    numeric = ('found', 'relevant', 'rows_written', 'ai_failures')
    report = {}
    for entry in entries:
        key = (entry['subindustry'], entry['type_of_product'], entry['site'], entry['keyword'])
        if key not in report:
            report[key] = {column: entry[column] for column in COVERAGE_COLUMNS}
            report[key]['error'] = entry['error'] or ''
            continue
        row = report[key]
        for column in numeric:
            row[column] += entry[column]
        errors = [e for e in row['error'].split(';') if e]
        if entry['error'] and entry['error'] not in errors:
            errors.append(entry['error'])
        row['error'] = ';'.join(errors)
    return list(report.values())


def apify_failed_keywords(coverage_entries):
    """Coverage entries of an Apify site that ended with an error (one per failed scrape call)."""
    return [entry for entry in coverage_entries if entry['site'] in APIFY_SITES and entry['error']]


def apify_failures_exceeded(coverage_entries, threshold):
    """``(failed entries, attempted Apify searches, exceeded)``: exceeded when failed / attempted
    is strictly above ``threshold`` (same rule as the relevance-AI failure rate). No Apify search
    means nothing failed."""
    attempted = sum(1 for entry in coverage_entries if entry['site'] in APIFY_SITES)
    failed = apify_failed_keywords(coverage_entries)
    return failed, attempted, bool(attempted) and len(failed) / attempted > threshold


def zero_relevant_pairs(report):
    """Report rows with no relevant result: candidates for a better keyword on that site."""
    return [row for row in report if row['relevant'] == 0]


def temp_path_for(output_path):
    """Temporary file next to the output ('x.csv' -> 'x.tmp.csv'), so ``os.replace`` stays on
    the same filesystem and the '*.csv' gitignore rule also covers a leftover temp file."""
    root, ext = os.path.splitext(output_path)
    return f"{root}.tmp{ext}" if ext else f"{output_path}.tmp"


# --- I/O -----------------------------------------------------------------------------------

def write_coverage_report(report, path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=COVERAGE_COLUMNS)
        writer.writeheader()
        writer.writerows(report)


def start_browser():
    """The run's shared Playwright browser (installed Chrome, else Playwright's Chromium), with the
    browser's own user agent: ``config.USER_AGENT`` (Chrome 128) contradicts the real Chrome's
    version headers and AeroSense answers it with HTTP 429. Raises ``BrowserUnavailable`` if
    neither browser can be launched."""
    return BrowserSession().start()


def make_scrapers(browser, ai_agent):
    return {
        # Apify adapters: construction reads no secret; the shared client is built on first scrape().
        'amazon': AmazonApifyScraper(driver_path=None, relevance_agent=ai_agent),
        'noon': NoonApifyScraper(driver_path=None, relevance_agent=ai_agent),
        # Browser scrapers (Playwright): one context per scrape() call on the shared browser.
        'mumzworld': MumzworldScraper(browser, relevance_agent=ai_agent),
        'saco': SacoScraper(browser, relevance_agent=ai_agent),
        'fine': FineScraper(browser, relevance_agent=ai_agent),
        'gogreen': GoGreenScraper(browser, relevance_agent=ai_agent),
        'officesupply': OfficeSupplyScraper(browser, relevance_agent=ai_agent),
        'aerosense': AeroSenseScraper(browser),  # does not use the relevance agent
    }


def sites_failing_every_search(coverage_entries):
    """``{site: searches}`` for each site whose every attempted search ended with an error
    (for example a site that blocks the browser: 403 on every page)."""
    attempted, failed = {}, {}
    for entry in coverage_entries:
        attempted[entry['site']] = attempted.get(entry['site'], 0) + 1
        if entry['error']:
            failed[entry['site']] = failed.get(entry['site'], 0) + 1
    return {site: n for site, n in attempted.items() if failed.get(site, 0) == n}


def site_failing_from_the_start(coverage_entries, site, limit=FAILED_SEARCHES_TO_ABORT):
    """A message when ``site`` has had ``limit`` or more searches and every one failed, else ''."""
    entries = [e for e in coverage_entries if e['site'] == site]
    if len(entries) < limit or any(not e['error'] for e in entries):
        return ''
    errors = sorted({e['error'] for e in entries})
    return (f"site '{site}' failed its first {len(entries)} searches ({', '.join(errors)}); "
            f"fix the site or remove it from TARGET_MAP, then run again")


def log_site_failures(coverage_entries):
    """Per-site error summary at the end of the run: a site that failed every search is an
    ERROR (it returned no rows because of the failures, not because nothing matched)."""
    for site, searches in sites_failing_every_search(coverage_entries).items():
        errors = sorted({e['error'] for e in coverage_entries if e['site'] == site and e['error']})
        logger.error(f"Site '{site}' failed every search ({searches} of {searches}): {', '.join(errors)}. "
                     f"See the 'error' column of the coverage report.")
    totals = {}
    for entry in coverage_entries:
        total, failed = totals.get(entry['site'], (0, 0))
        totals[entry['site']] = (total + 1, failed + (1 if entry['error'] else 0))
    for site, (total, failed) in totals.items():
        if 0 < failed < total:
            logger.warning(f"Site '{site}': {failed} of {total} searches failed.")


def run_scraping(df_instructions, writer, ai_agent, scraper_factory, browser, coverage_entries,
                 sleep=time.sleep, file_handle=None):
    """Scrapes every instruction and writes the rows; returns how many rows were written.

    A failing ``scrape()`` call is logged and recorded in ``coverage_entries`` (one site error
    must not throw away a 30 h run); a fatal relevance-AI or Apify error (token, HTTP 4xx,
    budget) raises ``ScrapingAborted``.
    """
    rows_total = 0
    for sub_industry_group, df_industry_instructions in df_instructions.groupby('Sub industry'):
        logger.info(f"\n=================================================")
        logger.info(f"  PROCESSING SUBINDUSTRY: '{sub_industry_group}'")
        logger.info(f"=================================================\n")

        if df_industry_instructions.empty:
            logger.warning(f"There are no instructions for '{sub_industry_group}'.")
            continue

        scrapers = scraper_factory(browser, ai_agent)
        all_found_products_for_group = []

        for index, row in df_industry_instructions.iterrows():
            sub_industry = row['Sub industry']
            plan = parse_instruction(row)
            base_keyword = plan['base_keyword']
            search_mode = plan['search_mode']
            site_specific_keywords = plan['site_specific_keywords']

            logger.info(f">> Searching '{base_keyword}' for '{sub_industry}' (Mode: {search_mode})")
            for site_name in sites_for(sub_industry, base_keyword):
                scraper = scrapers.get(site_name)
                if not scraper:
                    logger.warning(f"   -> No scraper found for '{site_name}'.")
                    continue
                keywords_to_use = site_specific_keywords.get(site_name, [plan['search_keyword']])

                for keyword_to_use in keywords_to_use:
                    if site_name in KEYWORD_REQUIRED_SITES:
                        if site_name not in site_specific_keywords and not plan['general_modifiers']:
                            logger.warning(f"Skipping '{site_name}' for '{base_keyword}' because no specific keyword or general modifier was provided.")
                            continue

                    logger.info(f"   -> Searching '{site_name}' with keyword: '{keyword_to_use}'")
                    before = ai_agent.stats()
                    error = ''
                    raised_fatal = None
                    try:
                        found_products = scraper.scrape(keyword_to_use, search_mode)
                    except (RelevanceAgentFatalError, ApifyFatalError) as fatal_error:
                        raised_fatal = fatal_error
                        found_products = []
                    except Exception as scrape_error:
                        logger.error(f"Error while scraping '{site_name}' with keyword '{keyword_to_use}'.", exc_info=True)
                        error = type(scrape_error).__name__
                        found_products = []  # Continue with the next site/keyword
                    uses_ai = site_name not in SITES_WITHOUT_AI
                    # The browser scrapers call is_relevant(), which records a fatal AI error in the
                    # agent and returns False instead of raising, so a fatal AI error is detected
                    # through the agent, not only through the call above. Every scraper lets the
                    # other errors propagate (page and parsing errors are caught above).
                    fatal = ai_agent.fatal_error or raised_fatal
                    if fatal is not None:
                        coverage_entries.append(coverage_entry(
                            sub_industry, plan['type_of_product'], site_name, keyword_to_use,
                            stats_delta(before, ai_agent.stats()), 0,
                            type(fatal).__name__, uses_ai=uses_ai,
                        ))
                        raise ScrapingAborted(str(fatal))

                    channel_value = channel_for(sub_industry)
                    for product in found_products:
                        all_found_products_for_group.append(build_row_data(
                            product,
                            industry=df_industry_instructions.iloc[0]['Industry'],
                            sub_industry=sub_industry,
                            type_of_product=plan['type_of_product'],
                            generic_type_of_product=plan['generic_product_type'],
                            site_name=site_name,
                            channel=channel_value,
                            date=time.strftime("%Y-%m-%d"),
                        ))
                        logger.debug(f"    -> Product added: {product.get('Product', 'N/A')[:60]}...")

                    coverage_entries.append(coverage_entry(
                        sub_industry, plan['type_of_product'], site_name, keyword_to_use,
                        stats_delta(before, ai_agent.stats()), len(found_products), error,
                        uses_ai=uses_ai,
                    ))
                    broken = site_failing_from_the_start(coverage_entries, site_name)
                    if broken:
                        raise ScrapingAborted(broken)

        if all_found_products_for_group:
            logger.info(f"Writing {len(all_found_products_for_group)} products found for '{sub_industry_group}' to the temporary file...")
            writer.writerows(all_found_products_for_group)
            if file_handle is not None:
                file_handle.flush()
            rows_total += len(all_found_products_for_group)
        else:
            logger.info(f"No products found to save for '{sub_industry_group}'.")

        logger.info(f"Process for '{sub_industry_group}' completed.")
        logger.info(f"Resting {SECONDS_BETWEEN_SUBINDUSTRIES} seconds before the next subindustry...")
        sleep(SECONDS_BETWEEN_SUBINDUSTRIES)
    return rows_total


def _run(args, coverage_entries, agent_factory, scraper_factory, browser_factory, sleep,
         apify_preflight=apify_preflight_default):
    """The scraping run; returns the exit code. The final CSV is only replaced on success."""
    try:
        df_instructions = pd.read_csv(args.analysis_file)
        df_instructions = df_instructions.dropna(subset=['Type of product', 'Sub industry'])
        logger.info(f"Instructions loaded from '{args.analysis_file}'. {len(df_instructions)} tasks found.")
    except FileNotFoundError:
        logger.error(f"Instructions file '{args.analysis_file}' not found.")
        return 1
    except Exception:
        logger.error(f"Error reading '{args.analysis_file}'.", exc_info=True)
        return 1

    logger.info("Starting the browser (Playwright)...")
    try:
        browser = browser_factory()
    except Exception:
        logger.critical("Could not start the browser (installed Chrome or Playwright's Chromium).", exc_info=True)
        return 1
    try:
        return _scrape(args, df_instructions, coverage_entries, agent_factory, scraper_factory, browser,
                       sleep, apify_preflight)
    finally:
        close = getattr(browser, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.warning("Could not close the browser cleanly.", exc_info=True)


def _scrape(args, df_instructions, coverage_entries, agent_factory, scraper_factory, browser, sleep,
            apify_preflight):
    """Preflights, scraping and the final checks, with the browser already started."""
    try:
        ai_agent = agent_factory()
        ai_agent.preflight()
    except RelevanceAgentError as error:
        logger.critical(f"Relevance AI is not available: {error}")
        return 1

    try:
        apify_preflight()
    except ApifyError as error:
        logger.critical(f"Apify is not available: {error}")
        return 1

    output_file = args.output_file
    temp_file = temp_path_for(output_file)
    file_exists = os.path.exists(output_file)
    if args.output_mode == 'append' and file_exists:
        # DictWriter would append 15-column rows under an old 13-column header.
        try:
            with open(output_file, newline='', encoding='utf-8') as existing:
                header = next(csv.reader(existing), [])
        except (OSError, UnicodeDecodeError, csv.Error):
            logger.error(f"Could not read the header of '{output_file}'.", exc_info=True)
            return 1
        if header != config.CSV_COLUMNS:
            logger.error(f"'{output_file}' has a different header (pre-F6 file?); use overwrite. "
                         f"Nothing was written.")
            return 1
    write_header = (args.output_mode == 'overwrite') or (not file_exists)
    try:
        if args.output_mode == 'append' and file_exists:
            shutil.copyfile(output_file, temp_file)  # append to a copy; the original stays intact
        write_mode = 'w' if args.output_mode == 'overwrite' else 'a'
        with open(temp_file, write_mode, newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=config.CSV_COLUMNS)
            if write_header:
                writer.writeheader()
            rows_written = run_scraping(df_instructions, writer, ai_agent, scraper_factory,
                                        browser, coverage_entries, sleep=sleep, file_handle=f)
    except ScrapingAborted as aborted:
        logger.critical(f"Scraping aborted: {aborted}. '{output_file}' was not modified.")
        return 1
    except Exception:
        logger.critical(f"Unexpected error in the scraping process. '{output_file}' was not modified.",
                        exc_info=True)
        return 1

    if rows_written == 0:
        logger.error(f"No competitor rows were scraped. '{output_file}' was not modified.")
        return 1

    broken_sites = sites_failing_every_search(coverage_entries)
    if broken_sites:
        listed = ", ".join(f"'{site}' ({n} of {n})" for site, n in broken_sites.items())
        logger.error(f"Sites that failed every search: {listed}. '{output_file}' was not modified; "
                     f"partial rows are in '{temp_file}'. Fix the site or remove it from TARGET_MAP, "
                     f"then run again.")
        return 1

    stats = ai_agent.stats()
    logger.info(
        f"Relevance AI: {stats.get('evaluated', 0)} titles evaluated, {stats.get('relevant', 0)} relevant, "
        f"{stats.get('failures', 0) + stats.get('wipes_failures', 0)} failures "
        f"(rate {ai_agent.failure_rate():.1%})."
    )
    try:
        ai_agent.check_failure_rate()
    except RelevanceAgentError as error:
        logger.error(f"{error}. '{output_file}' was not modified; partial rows are in '{temp_file}'.")
        return 1

    threshold = config.APIFY_MAX_FAILED_KEYWORD_RATE
    failed, attempted, exceeded = apify_failures_exceeded(coverage_entries, threshold)
    details = "; ".join(f"{e['site']} '{e['keyword']}' {e['error']}" for e in failed[:20])
    if exceeded:
        logger.error(f"{len(failed)} of {attempted} Apify keyword searches failed "
                     f"({len(failed) / attempted:.1%}, max {threshold:.0%}): {details}. "
                     f"'{output_file}' was not modified; partial rows are in '{temp_file}'.")
        return 1
    if failed:
        logger.warning(f"{len(failed)} of {attempted} Apify keyword searches failed "
                       f"({len(failed) / attempted:.1%}, within the {threshold:.0%} limit): {details}.")

    os.replace(temp_file, output_file)
    logger.info(f"{rows_written} rows written. '{output_file}' replaced.")
    return 0


def main(argv=None, *, agent_factory=RelevanceAgent, scraper_factory=make_scrapers,
         browser_factory=start_browser, sleep=time.sleep,
         apify_preflight=apify_preflight_default):
    """Entry point; returns the exit code (0 only if the output CSV was replaced).

    Tests inject ``agent_factory`` (no Bedrock), ``scraper_factory(browser, agent)``,
    ``browser_factory`` (no browser; its result is closed at the end if it has ``close()``),
    ``sleep`` and ``apify_preflight`` (no secret, no network).
    """
    args = parse_args(argv)
    logger.info(f"Starting scraping. Output mode: {args.output_mode}")
    logger.info(f"Using instructions file: {args.analysis_file}")
    logger.info(f"Output file: {args.output_file}")

    coverage_entries = []
    exit_code = 1
    try:
        exit_code = _run(args, coverage_entries, agent_factory, scraper_factory, browser_factory, sleep,
                         apify_preflight=apify_preflight)
    finally:
        # Best effort, also after an error or SIGTERM from "timeout": the report says how far it got.
        try:
            report = build_coverage_report(coverage_entries)
            write_coverage_report(report, args.coverage_file)
            empty = zero_relevant_pairs(report)
            logger.info(f"Coverage report: {len(report)} product x site x keyword rows in '{args.coverage_file}'.")
            log_site_failures(coverage_entries)
            if empty:
                logger.warning(f"{len(empty)} product x site x keyword pairs with 0 relevant results:")
                for row in empty:
                    detail = f" (error: {row['error']})" if row['error'] else ""
                    logger.warning(f"   - {row['subindustry']} | {row['type_of_product']} | "
                                   f"{row['site']} | '{row['keyword']}'{detail}")
        except Exception:
            logger.error(f"Could not write the coverage report '{args.coverage_file}'.", exc_info=True)
    logger.info(f"\n\n--- SCRAPING PROCESS FINISHED (exit code {exit_code}) ---")
    return exit_code


def _exit_on_sigterm(signum, frame):
    # "timeout 30h" sends SIGTERM: turn it into SystemExit so the browser is closed and the
    # coverage report is still written. The previous CSV is untouched.
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    sys.exit(main())
