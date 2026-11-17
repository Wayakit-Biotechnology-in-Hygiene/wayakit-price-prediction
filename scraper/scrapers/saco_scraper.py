import json
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, InvalidSessionIdException
from bs4 import BeautifulSoup
from urllib.parse import quote
from utils import parse_volume_string, parse_count_string, parse_saco_count_string
import config
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from log_config import get_logger

logger = get_logger()

class SacoScraper:
    def __init__(self, driver_path, relevance_agent):
        self.driver_path = driver_path
        self.relevance_agent = relevance_agent
        self.base_url = "https://www.saco.sa/en/"

    def _log(self, msg):
        logger.info(msg)

    def _handle_overlays(self, driver):
        try:
            cookie_accept_button = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Accept')]"))
            )
            logger.info("    > Cookie banner detected. Clicking 'Accept'.")
            driver.execute_script("arguments[0].click();", cookie_accept_button)
            time.sleep(2)
        except TimeoutException:
            logger.debug("    > No cookie banner detected. Continuing.")
            pass

    def _extract_product_details(self, driver, product_url, search_mode):
        logger.debug(f"        -> Extracting details from: {product_url}")
        driver.get(product_url)
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, "h1"))
        )
        soup = BeautifulSoup(driver.page_source, 'html.parser')

        details = {
            'Product': 'Not found', 'Price_SAR': '0.00', 'Company': 'Brand not found',
            'URL': product_url, 'Unit of measurement': 'units', 'Total quantity': 0
        }

        # The new SACO site (Next.js/Tailwind) has no stable CSS classes;
        # the reliable data is the JSON-LD (schema.org/Product) with name and price.
        product_data = None
        for tag in soup.select("script[type='application/ld+json']"):
            try:
                data = json.loads(tag.string or '')
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and data.get('@type') == 'Product':
                product_data = data
                break

        if product_data:
            details['Product'] = product_data.get('name') or 'Not found'
            offers = product_data.get('offers') or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if offers.get('price'):
                details['Price_SAR'] = str(offers['price'])
            brand = product_data.get('brand') or {}
            if isinstance(brand, dict) and brand.get('name'):
                details['Company'] = brand['name']
        else:
            title_tag = soup.find('h1')
            if title_tag:
                details['Product'] = title_tag.get_text(strip=True)

        product_name = details['Product']
        if product_name != "Not found":
            if search_mode == 'units':
                parsed_data = parse_saco_count_string(product_name)
                if not parsed_data:
                    parsed_data = parse_count_string(product_name)
            else:
                parsed_data = parse_volume_string(product_name)

            if parsed_data:
                details['Total quantity'] = parsed_data['quantity']
                details['Unit of measurement'] = parsed_data['unit']
                logger.debug(f"        -> Extracted amount: {details['Total quantity']} {details['Unit of measurement']}")

        return details

    def scrape(self, keyword, search_mode):
        self._log(f"  [Saco Scraper] Searching: '{keyword}'")
        search_keyword = quote(keyword)
        search_url = f"{self.base_url}products-search?q={search_keyword}"

        all_found_products = []
        products_to_find_limit = 6

        service = ChromeService(executable_path=self.driver_path)
        options = webdriver.ChromeOptions()
        options.add_experimental_option('excludeSwitches', ['enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument('--disable-notifications')
        options.add_argument('--headless')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-webgl')
        options.add_argument('--disable-3d-apis')
        options.add_argument(f"user-agent={config.USER_AGENT}")
        options.add_argument('--log-level=3')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-extensions ')
        options.add_argument('--disable-browser-side-navigation')
        options.add_experimental_option('excludeSwitches', ['enable-logging'])
        
        driver = webdriver.Chrome(service=service, options=options)

        driver.get(search_url)
        self._handle_overlays(driver)

        try:
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/en/product/']"))
            )
        except TimeoutException:
            logger.warning("    > No product links found on the initial page. Skipping keyword.")
            if driver:
                driver.quit()
            return []

        time.sleep(3)

        # The new site repeats the same href in several <a> per card (image and
        # title): we collect unique hrefs and visit them directly, without the
        # old click-and-go-back flow. With such a low product limit the first
        # results page is always enough (no pagination).
        product_hrefs = []
        for link in driver.find_elements(By.CSS_SELECTOR, "a[href*='/en/product/']"):
            href = link.get_attribute('href')
            if href and href not in product_hrefs:
                product_hrefs.append(href)

        logger.debug(f"    > Found {len(product_hrefs)} unique products on results page.")

        for idx, product_url in enumerate(product_hrefs, 1):
            if len(all_found_products) >= products_to_find_limit:
                logger.info(f"    > Target of {products_to_find_limit} products reached.")
                break
            try:
                logger.debug(f"      -> Processing product {idx}/{len(product_hrefs)}...")
                product_details = self._extract_product_details(driver, product_url, search_mode)

                if product_details and product_details.get('Total quantity', 0) > 0:
                    is_relevant = self.relevance_agent.is_relevant(product_details.get('Product'), keyword)
                    if is_relevant:
                        all_found_products.append(product_details)
                        logger.info(f"      -> AI VALIDATED. Product saved: {product_details['Product'][:60]}...")
                    else:
                        logger.info(f"      -> DISCARDED BY AI (Not relevant): {product_details['Product'][:60]}...")
                else:
                    logger.info(f"      -> DISCARDED (no quantity): {product_details.get('Product', 'N/A')[:60]}...")

            except TimeoutException:
                logger.warning(f"      -> WARNING: Could not process product {idx}. Skipping.")
                continue
            except InvalidSessionIdException:
                logger.error(f"      -> FATAL ERROR: Browser session lost. Aborting scrape for '{keyword}'.")
                if driver:
                    driver.quit()
                return all_found_products

        logger.info(f"\n  [Saco Scraper] Finished scraping. Found data for {len(all_found_products)} products.")
        if driver:
            driver.quit()
        return all_found_products