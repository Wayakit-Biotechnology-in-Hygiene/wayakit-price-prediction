"""A stand-in for the `selenium` and `webdriver_manager` packages, for running the BASE version's
Selenium scrapers in the differential tests after the migration to Playwright (2026-11-25), when
the environment no longer installs Selenium.

`install(site)` puts fake modules in `sys.modules` (call it inside `isolated_scraper_tree`, which
restores `sys.modules`). `webdriver.Chrome(...)` returns a `FakeDriver` that serves the saved HTML
of `site` (URL -> HTML): `get`, `page_source`, `current_url`, `find_elements` (CSS, via
BeautifulSoup; `get_attribute('href')` resolves against the current URL like a real browser) and
`WebDriverWait(...).until(condition)` raising `TimeoutException` when the element is absent.
Nothing opens a browser or the network.
"""
from __future__ import annotations

import sys
import types
from urllib.parse import urljoin

from bs4 import BeautifulSoup


class TimeoutException(Exception):
    pass


class InvalidSessionIdException(Exception):
    pass


class By:
    CSS_SELECTOR = "css selector"
    XPATH = "xpath"
    TAG_NAME = "tag name"


class FakeElement:
    def __init__(self, tag, base_url):
        self.tag = tag
        self.base_url = base_url

    def get_attribute(self, name):
        value = self.tag.get(name)
        if name == "href" and value is not None:
            return urljoin(self.base_url, value)
        return value

    def click(self):
        pass


class FakeDriver:
    def __init__(self, site):
        self.site = site
        self.current_url = "about:blank"
        self.page_source = "<html></html>"
        self.visited = []

    def get(self, url):
        self.visited.append(url)
        self.current_url = url
        self.page_source = self.site.get(url, "<html><body>404</body></html>")

    def _soup(self):
        return BeautifulSoup(self.page_source, "html.parser")

    def find_elements(self, by, selector):
        soup = self._soup()
        if by == By.CSS_SELECTOR:
            tags = soup.select(selector)
        elif by == By.TAG_NAME:
            tags = soup.find_all(selector)
        else:  # XPath: only used for cookie banners and "Next" links, absent from the saved pages
            tags = []
        return [FakeElement(tag, self.current_url) for tag in tags]

    def find_element(self, by, selector):
        found = self.find_elements(by, selector)
        if not found:
            raise TimeoutException(f"no {selector}")
        return found[0]

    def execute_script(self, *args, **kwargs):
        return None

    def quit(self):
        pass


def _present(locator):
    by, selector = locator

    def condition(driver):
        found = driver.find_elements(by, selector)
        return found[0] if found else False
    return condition


class WebDriverWait:
    def __init__(self, driver, timeout):
        self.driver = driver

    def until(self, condition):
        result = condition(self.driver)
        if not result:
            raise TimeoutException("element not found in the saved page")
        return result


def install(site):
    """Registers the fake modules; `webdriver.Chrome()` serves ``site`` (URL -> HTML)."""
    def chrome(*args, **kwargs):
        return FakeDriver(site)

    class ChromeOptions:
        def add_argument(self, *a):
            pass

        def add_experimental_option(self, *a):
            pass

    ec = types.SimpleNamespace(presence_of_element_located=_present, element_to_be_clickable=_present,
                               presence_of_all_elements_located=_present)
    modules = {
        "selenium": types.SimpleNamespace(),
        "selenium.webdriver": types.SimpleNamespace(Chrome=chrome, ChromeOptions=ChromeOptions),
        "selenium.webdriver.common": types.SimpleNamespace(),
        "selenium.webdriver.common.by": types.SimpleNamespace(By=By),
        "selenium.webdriver.support": types.SimpleNamespace(expected_conditions=ec),
        "selenium.webdriver.support.ui": types.SimpleNamespace(WebDriverWait=WebDriverWait),
        "selenium.webdriver.support.expected_conditions": ec,
        "selenium.webdriver.chrome": types.SimpleNamespace(),
        "selenium.webdriver.chrome.service": types.SimpleNamespace(Service=lambda *a, **k: None),
        "selenium.common": types.SimpleNamespace(),
        "selenium.common.exceptions": types.SimpleNamespace(
            TimeoutException=TimeoutException, InvalidSessionIdException=InvalidSessionIdException,
            NoSuchElementException=TimeoutException, StaleElementReferenceException=TimeoutException,
            ElementClickInterceptedException=TimeoutException),
        "webdriver_manager": types.SimpleNamespace(),
        "webdriver_manager.chrome": types.SimpleNamespace(
            ChromeDriverManager=lambda: types.SimpleNamespace(install=lambda: "/fake/chromedriver")),
    }
    modules["selenium"].webdriver = modules["selenium.webdriver"]
    for name, module in modules.items():
        sys.modules[name] = module
    return list(modules)
