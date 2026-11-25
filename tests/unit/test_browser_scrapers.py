"""Unit tests of the Playwright browser layer and the B2B site scrapers (Fine, GoGreen, OfficeSupply,
AeroSense), 2026-11 migration from Selenium. Mumzworld and SACO (ticket A: parsing unchanged) are
compared with the base on saved HTML in tests/differential/test_scraper_main_differential.py.

No browser and no network: the parsers get small hand-written HTML, the scrapers run against a fake
site (`FakeSite` replaces each module's `fetch_html`: URL -> HTML or exception) and a fake browser,
and `fetch_html` / `BrowserSession` run against fake Playwright pages and launchers.

What it proves:
- every scraper keeps the scraper contract: ``scrape(keyword, mode) -> list[dict]`` with the keys
  `Product`, `Price_SAR`, `Company`, `URL`, `Unit of measurement`, `Total quantity`;
- failures are no longer swallowed: an HTTP error, a missing element, a changed page, an Arabic-only
  GoGreen, an AeroSense product missing from the shop, or every product page of a search failing
  raise (a `PageLoadError` subclass) instead of returning []; a genuine "no results" page returns [];
- one failed product page among good ones is skipped, and the product limits and the relevance AI
  still apply;
- AeroSense finds the product slug in any shop category (the old fixed category gave 404);
- `fetch_html` maps HTTP >= 400, navigation errors and selector timeouts to its own exceptions;
  `BrowserSession` falls back from Chrome to Playwright's Chromium, raises `BrowserUnavailable`
  when neither starts, and always closes the per-search context.
"""
from __future__ import annotations

import contextlib
import re
import types

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from bs4 import BeautifulSoup

from tests.conftest import load_module

pytestmark = pytest.mark.unit

m = load_module("scraper/main.py")
from scrapers import aerosense_scraper as aero, fine_scraper as fine  # noqa: E402
from scrapers import gogreen_scraper as gg, officesupply_scraper as osup  # noqa: E402
from services import browser as br  # noqa: E402

CONTRACT_KEYS = {"Product", "Price_SAR", "Company", "URL", "Unit of measurement", "Total quantity"}


# --- fakes ----------------------------------------------------------------------------------------
class FakeAgent:
    """Relevant when 'good' is in the title; records every call."""

    def __init__(self):
        self.calls = []

    def is_relevant(self, title, keyword):
        self.calls.append((title, keyword))
        return "good" in title.lower()


class FakeContext:
    def __init__(self):
        self.cookies = []

    def add_cookies(self, cookies):
        self.cookies += cookies


class FakePage:
    def __init__(self):
        self.context = FakeContext()


class FakeBrowser:
    def __init__(self):
        self.pages = 0
        self.opened = []

    @contextlib.contextmanager
    def page(self):
        self.pages += 1
        page = FakePage()
        self.opened.append(page)
        yield page


class FakeSite:
    """Replaces ``fetch_html``: URL -> HTML, or an exception to raise; unknown URLs are a 404.

    Like the real one, it raises ``ElementNotFound`` when no part of ``wait_for`` is in the page
    (a CSS part through BeautifulSoup; a ``:text('...')`` / ``:has-text('...')`` part by its text)."""

    def __init__(self, pages):
        self.pages = pages
        self.visited = []

    def fetch(self, page, url, wait_for=None, timeout_ms=None):
        self.visited.append(url)
        value = self.pages.get(url)
        if value is None:
            raise br.HttpStatusError(404, url)
        if isinstance(value, Exception):
            raise value
        if wait_for and not _any_part_present(value, wait_for):
            raise br.ElementNotFound(f"'{wait_for}' not in {url}")
        return value


def _any_part_present(html, selector):
    soup = BeautifulSoup(html, "html.parser")
    for part in (p.strip() for p in selector.split(", ")):
        match = re.search(r":(?:has-)?text\(['\"](.+?)['\"]\)", part)
        if match:
            if match.group(1) in soup.get_text(" "):
                return True
        elif soup.select_one(part) is not None:
            return True
    return False


def _site(monkeypatch, module, pages):
    site = FakeSite(pages)
    monkeypatch.setattr(module, "fetch_html", site.fetch)
    return site


def _check_contract(rows):
    for row in rows:
        assert CONTRACT_KEYS <= set(row), row
        assert row["Total quantity"] > 0


# --- AeroSense ------------------------------------------------------------------------------------
AERO_SHOP = """<html><body>
<a href="/en/online-shop">Shop</a><a href="/en/online-shop#precontent">skip</a>
<a href="/en/online-shop/interior-cleaning">Interior</a>
<a href="/en/online-shop/interior-cleaning/carpet-cleaner">Carpet Cleaner</a>
<a href="/en/online-shop/inflight-hygiene/avionics-wipes?x=1">Avionics wipes</a>
</body></html>"""
AERO_PRODUCT = """<html><body><h1><div class="field--name-title">Carpet Cleaner</div></h1>
<div id="edit-purchased-entity-0-attributes-attribute-volume">
 <div class="js-form-item"><span class="package">5 litre can x 4</span><span class="variationprice">€ 113.00</span></div>
 <div class="js-form-item"><span class="package">1 litre bottle</span><span class="variationprice">€ 1,020.50</span></div>
 <div class="js-form-item"><span class="package">200 litre barrel</span></div>
 <div class="js-form-item"><span class="package">Small quantity</span></div>
</div></body></html>"""
CARPET_URL = "https://www.aero-sense.com/en/online-shop/interior-cleaning/carpet-cleaner"


def test_aerosense_finds_the_slug_in_any_shop_category():
    assert aero.find_product_url(AERO_SHOP, "carpet-cleaner") == CARPET_URL
    assert aero.find_product_url(AERO_SHOP, "avionics-wipes") == \
        "https://www.aero-sense.com/en/online-shop/inflight-hygiene/avionics-wipes"
    assert aero.find_product_url(AERO_SHOP, "interior-cleaning") is None  # a category, not a product
    assert aero.find_product_url(AERO_SHOP, "airframe-cleaner") is None
    assert aero.product_slug("Carpet Cleaner") == "carpet-cleaner"


def test_aerosense_parses_every_priced_package():
    rows = aero.parse_product_page(AERO_PRODUCT, CARPET_URL, "volume")
    assert [(r["Product"], r["Price_SAR"], r["Total quantity"], r["Unit of measurement"]) for r in rows] == [
        ("Carpet Cleaner - 5 litre can x 4", round(113.00 * 4.39, 2), 20000.0, "ml"),
        ("Carpet Cleaner - 1 litre bottle", round(1020.50 * 4.39, 2), 1000.0, "ml"),
    ]
    assert all(r["Company"] == "AeroSense" and r["URL"] == CARPET_URL for r in rows)
    _check_contract(rows)


def test_aerosense_page_without_packages_is_a_changed_site():
    with pytest.raises(br.SiteLayoutChanged):
        aero.parse_product_page("<html><h1><div class='field--name-title'>X</div></h1></html>", CARPET_URL)
    with pytest.raises(br.SiteLayoutChanged):
        aero.parse_product_page("<html><body>Not found</body></html>", CARPET_URL)


def test_aerosense_scrape_reads_the_shop_index_once_and_raises_for_a_missing_product(monkeypatch):
    site = _site(monkeypatch, aero, {aero.SHOP_URL: AERO_SHOP, CARPET_URL: AERO_PRODUCT})
    scraper = aero.AeroSenseScraper(FakeBrowser())
    rows = scraper.scrape("carpet cleaner", "volume")
    assert len(rows) == 2
    _check_contract(rows)
    with pytest.raises(br.SiteLayoutChanged, match="airframe-cleaner"):
        scraper.scrape("airframe cleaner", "volume")
    assert site.visited.count(aero.SHOP_URL) == 1


def test_aerosense_http_error_propagates(monkeypatch):
    _site(monkeypatch, aero, {aero.SHOP_URL: AERO_SHOP, CARPET_URL: br.HttpStatusError(500, CARPET_URL)})
    with pytest.raises(br.HttpStatusError):
        aero.AeroSenseScraper(FakeBrowser()).scrape("carpet cleaner", "volume")


# --- OfficeSupply ---------------------------------------------------------------------------------
def _os_results(n):
    cards = "".join(f'<div class="ut2-gl__body"><a class="product_icon_lnk" href="/en/p{i}/">p</a></div>'
                    for i in range(n))
    return f"<html><body>{cards}</body></html>"


def _os_product(title, integer="12", decimals="50"):
    return (f'<html><body><div class="ut2-pb__title"><h1>{title}</h1></div>'
            f'<span class="ty-price"><bdi><span class="ty-price-num">SAR</span>'
            f'<span class="ty-price-num">{integer}<sup>{decimals}</sup></span></bdi></span></body></html>')


def test_officesupply_search_results_no_results_and_changed_page():
    assert osup.parse_search_results(_os_results(3)) == [f"https://officesupply.sa/en/p{i}/" for i in range(3)]
    assert osup.parse_search_results('<div class="ty-no-items">No products found</div>') == []
    with pytest.raises(br.SiteLayoutChanged):
        osup.parse_search_results("<html><body>Access denied</body></html>")


def test_officesupply_product_price_and_size():
    details = osup.parse_product_page(_os_product("Good Air Freshener 300 ml", "1,234", "50"), "u", "volume")
    assert (details["Product"], details["Price_SAR"], details["Total quantity"], details["Unit of measurement"]) \
        == ("Good Air Freshener 300 ml", "1234.50", 300.0, "ml")
    assert osup.parse_product_page(_os_product("Good tissue 100 sheets"), "u", "units")["Total quantity"] == 100
    no_decimals = '<span class="ty-price"><bdi><span class="ty-price-num">45</span></bdi></span>'
    assert osup.extract_price(osup.BeautifulSoup(no_decimals, "html.parser")) == "45.00"


def test_officesupply_scrape_skips_a_failed_product_and_applies_relevance_and_limit(monkeypatch):
    url = osup.search_url("air freshener")
    pages = {url: _os_results(14)}
    for i in range(14):
        pages[f"https://officesupply.sa/en/p{i}/"] = _os_product(f"{'Good' if i % 2 else 'Other'} spray {i} 300 ml")
    pages["https://officesupply.sa/en/p1/"] = br.ElementNotFound("price never appeared")
    site = _site(monkeypatch, osup, pages)
    agent = FakeAgent()
    scraper = osup.OfficeSupplyScraper(FakeBrowser(), agent)
    scraper.products_to_find_limit = 3
    rows = scraper.scrape("air freshener", "volume")
    assert [r["Product"] for r in rows] == ["Good spray 3 300 ml", "Good spray 5 300 ml", "Good spray 7 300 ml"]
    _check_contract(rows)
    assert site.visited[-1] == "https://officesupply.sa/en/p7/", "stops visiting at the limit"
    assert all(kw == "air freshener" for _, kw in agent.calls)


def test_officesupply_every_product_page_failing_fails_the_search(monkeypatch):
    url = osup.search_url("x")
    pages = {url: _os_results(2), "https://officesupply.sa/en/p0/": br.HttpStatusError(403, "p0"),
             "https://officesupply.sa/en/p1/": br.HttpStatusError(403, "p1")}
    _site(monkeypatch, osup, pages)
    with pytest.raises(br.HttpStatusError):
        osup.OfficeSupplyScraper(FakeBrowser(), FakeAgent()).scrape("x", "volume")


def test_officesupply_no_results_is_an_empty_search(monkeypatch):
    _site(monkeypatch, osup, {osup.search_url("x"): '<p class="ty-no-items">No products found</p>'})
    assert osup.OfficeSupplyScraper(FakeBrowser(), FakeAgent()).scrape("x", "volume") == []


# --- Fine -----------------------------------------------------------------------------------------
def _fine_results(paths, next_href=None):
    cards = "".join(f'<div class="listing-page"><a class="display-flex" href="{p}">x</a>'
                    f'<a class="ecomz-productlisting-title" href="{p}">x</a></div>' for p in paths)
    nxt = f'<a href="{next_href}">Next</a>' if next_href else '<a href="javascript:void(0)">Next</a>'
    return f"<html><body>{cards}{nxt}</body></html>"


def _fine_product(title, price="SAR 25.00", specs=()):
    rows = "".join(f'<div class="product-tr"><div class="product-cell">{k}</div><div class="product-cell">{v}</div></div>'
                   for k, v in specs)
    return (f'<html><body><div class="ecomz-product-name-style">{title}</div>'
            f'<div class="ecomz-product-price-style">{price}</div>{rows}</body></html>')


def test_fine_search_results_next_page_and_no_results():
    page_url = fine.search_url("gel")
    urls, next_url = fine.parse_search_results(_fine_results(["/product/a/1?p=1", "/product/b/2"], "/en/products?page=2"),
                                               page_url)
    assert urls == ["https://ksa.finestore.com/product/a/1?p=1", "https://ksa.finestore.com/product/b/2"]
    assert next_url == "https://ksa.finestore.com/en/products?page=2"
    assert fine.parse_search_results(_fine_results(["/product/a/1"]), page_url)[1] is None  # javascript link
    no_results = '<div class="listing-page-item-container"><div>No results found for "zz"</div></div>'
    assert fine.parse_search_results(no_results, page_url) == ([], None)
    with pytest.raises(br.SiteLayoutChanged):
        fine.parse_search_results("<html><body>Blocked</body></html>", page_url)


def test_fine_product_size_from_specs_and_title_multiplier():
    details = fine.parse_product_page(_fine_product("Good Gel 5 L x 4", specs=[("Size", "5 L")]), "u", "volume")
    assert (details["Total quantity"], details["Unit of measurement"], details["Price_SAR"]) == (20.0, "L", "25.00")
    wipes = fine.parse_product_page(_fine_product("Good wipes", specs=[("Number of wipes", "80")]), "u", "units")
    assert (wipes["Total quantity"], wipes["Unit of measurement"]) == (80, "units")
    assert fine.is_valid_product({**details, "Price_SAR": "0.00"}) == (False, "Invalid price")
    assert fine.is_valid_product(details) == (True, "Valid")


def test_fine_scrape_follows_pages_until_the_limit(monkeypatch):
    first = fine.search_url("gel")
    second = "https://ksa.finestore.com/en/products?page=2"
    pages = {first: _fine_results(["/product/a", "/product/b"], "/en/products?page=2"),
             second: _fine_results(["/product/c", "/product/d"]),
             "https://ksa.finestore.com/product/a": _fine_product("Good gel 500 ml", price="SAR 0"),  # invalid price
             "https://ksa.finestore.com/product/b": _fine_product("Good gel 1 L"),
             "https://ksa.finestore.com/product/c": _fine_product("Other gel 1 L"),
             "https://ksa.finestore.com/product/d": _fine_product("Good gel 2 L")}
    _site(monkeypatch, fine, pages)
    scraper = fine.FineScraper(FakeBrowser(), FakeAgent())
    scraper.products_to_find_limit = 2
    rows = scraper.scrape("gel", "volume")
    assert [r["Product"] for r in rows] == ["Good gel 1 L", "Good gel 2 L"]
    _check_contract(rows)


def test_fine_blocked_search_page_raises(monkeypatch):
    _site(monkeypatch, fine, {fine.search_url("gel"): br.HttpStatusError(403, "search")})
    with pytest.raises(br.HttpStatusError):
        fine.FineScraper(FakeBrowser(), FakeAgent()).scrape("gel", "volume")


# --- GoGreen --------------------------------------------------------------------------------------
GG_HOME_EN = '<html lang="en" dir="ltr"><body>home</body></html>'
GG_HOME_AR = '<html class="no-js" lang="ar" dir="rtl"><body>home</body></html>'


def _gg_results(paths):
    cards = "".join(f'<div class="product-box"><a class="tp-image-product" href="{p}">x</a></div>' for p in paths)
    return f'<html lang="en">{cards}</html>'


def _gg_product(title, price="182 SAR"):
    return f'<html lang="en"><h1 class="title-product">{title}</h1><p class="product-formatted-price">{price}</p></html>'


def test_gogreen_arabic_only_store_fails_the_search(monkeypatch):
    _site(monkeypatch, gg, {gg.BASE_URL: GG_HOME_AR})
    browser = FakeBrowser()
    with pytest.raises(gg.LanguageUnavailable, match="lang='ar'"):
        gg.GoGreenScraper(browser, FakeAgent()).scrape("oven cleaner", "volume")
    assert issubclass(gg.LanguageUnavailable, br.PageLoadError)


def test_gogreen_english_store_is_scraped(monkeypatch):
    search = "https://gogreen.com.sa/products?search=oven%20cleaner"
    _site(monkeypatch, gg, {gg.BASE_URL: GG_HOME_EN, search: _gg_results(["/products/a", "/products/b"]),
                            "https://gogreen.com.sa/products/a": _gg_product("Good Oven Cleaner 500 ml", "1,182"),
                            "https://gogreen.com.sa/products/b": _gg_product("Good Oven Cleaner")})  # no size
    rows = gg.GoGreenScraper(FakeBrowser(), FakeAgent()).scrape("oven cleaner", "volume")
    assert [(r["Product"], r["Price_SAR"], r["Total quantity"], r["Company"]) for r in rows] == [
        ("Good Oven Cleaner 500 ml", "1182", 500.0, "GoGreen")]
    _check_contract(rows)


def test_gogreen_parsers():
    assert gg.page_language(GG_HOME_AR) == "ar"
    assert gg.parse_search_results(_gg_results(["/products/a", "/products/a"])) == ["https://gogreen.com.sa/products/a"]
    with pytest.raises(br.SiteLayoutChanged):
        gg.parse_product_page("<html><body>nothing</body></html>", "u", "volume")


# --- browser layer --------------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status, headers=None, resource_type="document", frame="main"):
        self.status = status
        self.headers = headers or {}
        self.request = types.SimpleNamespace(resource_type=resource_type)
        self.frame = frame


class FakePlaywrightPage:
    """``status`` is one status or a list, one per ``goto`` (the last one repeats). With
    ``error_page=True`` a status >= 400 is seen only by the response listeners and ``goto`` raises,
    as Chrome does for an error status with an empty body."""
    main_frame = "main"

    def __init__(self, status=200, goto_error=None, selector_error=None, headers=None, error_page=False,
                 sub_responses=()):
        self.statuses = list(status) if isinstance(status, (list, tuple)) else [status]
        self.goto_error, self.selector_error, self.headers = goto_error, selector_error, headers
        self.error_page, self.sub_responses = error_page, sub_responses
        self.gotos = 0
        self.listeners = []

    def on(self, event, handler):
        assert event == "response"
        self.listeners.append(handler)

    def remove_listener(self, event, handler):
        self.listeners.remove(handler)

    settled = 0

    def wait_for_url(self, predicate, timeout=None):
        self.settled += 1
        assert predicate("chrome-error://chromewebdata/") and not predicate("https://example.invalid/")

    def goto(self, url, wait_until=None):
        if self.goto_error:
            raise self.goto_error
        self.gotos += 1
        response = FakeResponse(self.statuses[min(self.gotos, len(self.statuses)) - 1], self.headers)
        for seen in [*self.sub_responses, response]:
            for listener in list(self.listeners):
                listener(seen)
        if self.error_page and response.status >= 400:
            raise PlaywrightError(f"net::ERR_HTTP_RESPONSE_CODE_FAILURE at {url}")
        return response

    def wait_for_selector(self, selector, state=None, timeout=None):
        if self.selector_error:
            raise self.selector_error

    def content(self):
        return "<html>ok</html>"


@pytest.mark.parametrize("page, error", [
    (FakePlaywrightPage(status=403), br.HttpStatusError),
    (FakePlaywrightPage(status=429), br.HttpStatusError),
    (FakePlaywrightPage(status=404), br.HttpStatusError),
    (FakePlaywrightPage(goto_error=PlaywrightError("net::ERR_CONNECTION_RESET")), br.NavigationFailed),
    (FakePlaywrightPage(goto_error=PlaywrightTimeout("Timeout 45000ms exceeded")), br.NavigationFailed),
    (FakePlaywrightPage(selector_error=PlaywrightTimeout("Timeout 15000ms exceeded")), br.ElementNotFound),
])
def test_fetch_html_raises_instead_of_returning_a_broken_page(page, error):
    with pytest.raises(error):
        br.fetch_html(page, "https://example.invalid/x", wait_for="div.card", sleep=lambda s: None)
    assert issubclass(error, br.PageLoadError)


def test_an_error_page_without_body_is_an_http_status_error_not_a_navigation_error():
    page = FakePlaywrightPage(status=403, error_page=True,
                              sub_responses=[FakeResponse(500, resource_type="script"), FakeResponse(404, frame="ad")])
    with pytest.raises(br.HttpStatusError) as raised:
        br.fetch_html(page, "https://example.invalid/x", sleep=lambda s: None)
    assert raised.value.status == 403
    assert page.listeners == [], "the listener is removed"
    assert page.settled == 1, "waits for Chrome's error page so the next goto on this page works"
    sleeps = []
    page = FakePlaywrightPage(status=[429, 200], error_page=True)
    assert br.fetch_html(page, "https://example.invalid/x", sleep=sleeps.append) == "<html>ok</html>"
    assert sleeps == [br.RETRY_WAITS_S[0]], "a 429 error page is retried too"
    page = FakePlaywrightPage(goto_error=PlaywrightError("net::ERR_NAME_NOT_RESOLVED"),
                              sub_responses=[FakeResponse(500, resource_type="script")])
    with pytest.raises(br.NavigationFailed):
        br.fetch_html(page, "https://example.invalid/x")
    assert page.listeners == []


def test_fetch_html_retries_rate_limits_then_gives_up():
    sleeps = []
    page = FakePlaywrightPage(status=[429, 503, 200])
    assert br.fetch_html(page, "https://example.invalid/x", sleep=sleeps.append) == "<html>ok</html>"
    assert (page.gotos, sleeps) == (3, list(br.RETRY_WAITS_S))
    sleeps.clear()
    page = FakePlaywrightPage(status=429)
    with pytest.raises(br.HttpStatusError, match="429"):
        br.fetch_html(page, "https://example.invalid/x", sleep=sleeps.append)
    assert (page.gotos, sleeps) == (len(br.RETRY_WAITS_S) + 1, list(br.RETRY_WAITS_S))
    page = FakePlaywrightPage(status=404)
    with pytest.raises(br.HttpStatusError):
        br.fetch_html(page, "https://example.invalid/x", sleep=sleeps.append)
    assert page.gotos == 1, "a 404 is not retried"


@pytest.mark.parametrize("header, expected", [("30", 30), ("999", br.MAX_RETRY_AFTER_S), ("", br.RETRY_WAITS_S[1]),
                                              ("Wed, 21 Oct 2026 07:28:00 GMT", br.RETRY_WAITS_S[1])])
def test_retry_wait_honors_a_numeric_retry_after(header, expected):
    assert br.retry_wait(FakeResponse(429, {"retry-after": header}), 1) == expected


def test_fetch_html_returns_the_rendered_html():
    assert br.fetch_html(FakePlaywrightPage(), "https://example.invalid/x", wait_for="div") == "<html>ok</html>"
    assert br.HttpStatusError(403, "u").status == 403


class FakeLaunchedContext:
    def __init__(self, log):
        self.log = log

    def route(self, pattern, handler):
        self.log.append(f"route {pattern}")

    def new_page(self):
        return types.SimpleNamespace(set_default_timeout=lambda ms: None,
                                     set_default_navigation_timeout=lambda ms: None)

    def close(self):
        self.log.append("context closed")


class FakeLaunchedBrowser:
    version = "146.0"

    def __init__(self, log):
        self.log = log
        self.context_kwargs = None

    def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return FakeLaunchedContext(self.log)

    def close(self):
        self.log.append("browser closed")


def test_browser_session_falls_back_to_playwright_chromium(monkeypatch):
    monkeypatch.delenv("PI_BROWSER_CHANNEL", raising=False)
    log, tried = [], []

    def launcher(channel):
        tried.append(channel)
        if channel == "chrome":
            raise PlaywrightError("Chromium distribution 'chrome' is not found")
        return FakeLaunchedBrowser(log)

    session = br.BrowserSession(user_agent="UA", launcher=launcher).start()
    assert (tried, session.launched_with) == (["chrome", "chromium"], "chromium")
    with pytest.raises(RuntimeError):
        with session.page():
            raise RuntimeError("scrape failed")
    assert log == ["route **/*", "context closed"], "the context is closed after an error"
    assert session._browser.context_kwargs["user_agent"] == "UA"
    session.close()
    session.close()
    assert log == ["route **/*", "context closed", "browser closed"]


@pytest.mark.parametrize("resource_type, aborted", [("image", True), ("font", True), ("media", True),
                                                    ("document", False), ("script", False), ("xhr", False)])
def test_images_fonts_and_media_are_never_downloaded(resource_type, aborted):
    calls = []
    route = types.SimpleNamespace(request=types.SimpleNamespace(resource_type=resource_type),
                                  abort=lambda: calls.append("abort"), continue_=lambda: calls.append("continue"))
    br._skip_heavy_resources(route)
    assert calls == ["abort" if aborted else "continue"]


def test_browser_session_without_any_browser_is_unavailable(monkeypatch):
    monkeypatch.setenv("PI_BROWSER_CHANNEL", "chromium")
    tried = []

    def launcher(channel):
        tried.append(channel)
        raise PlaywrightError("Executable doesn't exist")

    with pytest.raises(br.BrowserUnavailable, match="chromium"):
        br.BrowserSession(launcher=launcher).start()
    assert tried == ["chromium"]


def test_collect_products_raises_only_when_every_page_failed():
    ok = {"a": {"Product": "good a", "Total quantity": 1}}

    def fetch(url):
        if url in ok:
            return ok[url]
        raise br.ElementNotFound(url)

    keep = br.keep_if_relevant(FakeAgent(), "k")
    assert br.collect_products(["x", "a", "y"], fetch, keep, 5, "T") == [ok["a"]]
    with pytest.raises(br.ElementNotFound, match="y"):
        br.collect_products(["x", "y"], fetch, keep, 5, "T")
    assert br.collect_products([], fetch, keep, 5, "T") == []


def test_make_scrapers_gives_every_browser_site_the_shared_browser():
    browser, agent = object(), FakeAgent()
    scrapers = m.make_scrapers(browser, agent)
    for site in ("mumzworld", "saco", "fine", "gogreen", "officesupply", "aerosense"):
        assert scrapers[site].browser is browser
    for site in ("mumzworld", "saco", "fine", "gogreen", "officesupply"):
        assert scrapers[site].relevance_agent is agent


# --- review findings (2026-11-25) -----------------------------------------------------------------
def _fine_pages(first_paths, next_href, second_paths=None, second=None):
    first = fine.search_url("gel")
    pages = {first: _fine_results(first_paths, next_href)}
    if second is not None:
        pages[second] = second_paths if isinstance(second_paths, Exception) else _fine_results(second_paths or [])
    for path in set(first_paths) | set(second_paths if isinstance(second_paths, list) else []):
        pages[f"https://ksa.finestore.com{path}"] = _fine_product(f"Good gel {path[-1]} 1 L")
    return pages


@pytest.mark.parametrize("next_href", ["#", "#top", "/en/products?keyword=gel", "javascript:void(0)"])
def test_fine_next_link_back_to_the_same_page_gives_no_duplicates(monkeypatch, next_href):
    site = _site(monkeypatch, fine, _fine_pages(["/product/a", "/product/b"], next_href))
    rows = fine.FineScraper(FakeBrowser(), FakeAgent()).scrape("gel", "volume")
    assert [r["URL"] for r in rows] == ["https://ksa.finestore.com/product/a", "https://ksa.finestore.com/product/b"]
    assert site.visited.count(fine.search_url("gel")) == 1


def test_fine_next_page_is_only_an_anchor_whose_own_text_is_next():
    html = ('<div class="listing-page"><a class="display-flex" href="/p/ng"><div>NextGen Gel 1 L</div></a></div>'
            '<a href="/en/products?page=2"><span>Next</span> page</a>')
    assert fine.parse_search_results(html, fine.search_url("gel")) == (["https://ksa.finestore.com/p/ng"], None)
    html += '<ul class="pagination"><a href="/en/products?page=2">Next</a></ul>'
    assert fine.parse_search_results(html, fine.search_url("gel"))[1] == "https://ksa.finestore.com/en/products?page=2"


def test_fine_a_later_page_failure_keeps_the_rows_already_found(monkeypatch):
    second = "https://ksa.finestore.com/en/products?page=2"
    _site(monkeypatch, fine, _fine_pages(["/product/a"], "/en/products?page=2", br.HttpStatusError(503, second), second))
    rows = fine.FineScraper(FakeBrowser(), FakeAgent()).scrape("gel", "volume")
    assert [r["URL"] for r in rows] == ["https://ksa.finestore.com/product/a"]


def test_fine_a_later_page_failure_with_nothing_found_fails_the_search(monkeypatch):
    second = "https://ksa.finestore.com/en/products?page=2"
    pages = _fine_pages(["/product/a"], "/en/products?page=2", br.HttpStatusError(503, second), second)
    pages["https://ksa.finestore.com/product/a"] = _fine_product("Other gel 1 L")  # not relevant
    _site(monkeypatch, fine, pages)
    with pytest.raises(br.HttpStatusError):
        fine.FineScraper(FakeBrowser(), FakeAgent()).scrape("gel", "volume")


def test_fine_limit_holds_across_pages_and_products_are_not_repeated(monkeypatch):
    second = "https://ksa.finestore.com/en/products?page=2"
    pages = _fine_pages(["/product/a", "/product/b"], "/en/products?page=2", ["/product/b", "/product/c", "/product/d"], second)
    site = _site(monkeypatch, fine, pages)
    scraper = fine.FineScraper(FakeBrowser(), FakeAgent())
    scraper.products_to_find_limit = 3
    rows = scraper.scrape("gel", "volume")
    assert [r["URL"][-1] for r in rows] == ["a", "b", "c"]
    assert "https://ksa.finestore.com/product/d" not in site.visited
    assert site.visited.count("https://ksa.finestore.com/product/b") == 1


def test_gogreen_sets_the_english_cookie_and_a_page_without_boxes_fails(monkeypatch):
    search = "https://gogreen.com.sa/products?search=x"
    _site(monkeypatch, gg, {gg.BASE_URL: GG_HOME_EN, search: '<html lang="en"><body>nothing</body></html>'})
    browser = FakeBrowser()
    with pytest.raises(br.ElementNotFound):
        gg.GoGreenScraper(browser, FakeAgent()).scrape("x", "volume")
    assert browser.opened[0].context.cookies == [gg.LOCALE_COOKIE]
    assert gg.LOCALE_COOKIE["name"] == "locale" and gg.LOCALE_COOKIE["value"] == "en-sa"


@pytest.mark.parametrize("module, html", [
    (gg, '<div class="product-box"><a class="other" href="/p">x</a></div>'),
    (osup, '<div class="ut2-gl__body"><a class="other" href="/p">x</a></div>'),
])
def test_result_cards_without_the_product_link_are_a_changed_site(module, html):
    with pytest.raises(br.SiteLayoutChanged):
        module.parse_search_results(f"<html><body>{html}</body></html>")


def test_collect_products_with_a_single_failed_page_fails_the_search():
    def fetch(url):
        raise br.HttpStatusError(403, url)
    with pytest.raises(br.HttpStatusError):
        br.collect_products(["only"], fetch, lambda d: True, 5, "T")


def test_aerosense_units_mode_counts_pieces():
    product = AERO_PRODUCT.replace("5 litre can x 4", "1 carton (400 pcs)")
    rows = aero.parse_product_page(product, CARPET_URL, "units")
    assert (rows[0]["Total quantity"], rows[0]["Unit of measurement"]) == (400, "units")
    assert all(r["Unit of measurement"] == "units" for r in rows)


class FakeChromium:
    def __init__(self):
        self.calls = []

    def launch(self, **kwargs):
        self.calls.append(kwargs)
        return "browser"


@pytest.mark.parametrize("channel, expected", [("chrome", "chrome"), ("chromium", None), ("msedge", "msedge")])
def test_launch_passes_the_channel_except_for_playwright_chromium(channel, expected):
    session = br.BrowserSession(launcher=lambda c: None)
    session._playwright = types.SimpleNamespace(chromium=FakeChromium())
    session._launch_with_playwright(channel)
    kwargs = session._playwright.chromium.calls[0]
    assert kwargs.get("channel") == expected and kwargs["headless"] is True


def test_start_browser_keeps_the_browsers_own_user_agent(monkeypatch):
    created = []

    class Recorder:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def start(self):
            return self

    monkeypatch.setattr(m, "BrowserSession", Recorder)
    m.start_browser()
    assert created == [{}], "a fixed user agent (config.USER_AGENT, Chrome 128) gets HTTP 429 from AeroSense"


class CrashingBrowser(FakeLaunchedBrowser):
    """``connected`` False: Chrome died; ``fail_contexts``: new_context raises that many times."""

    def __init__(self, log, connected=True, fail_contexts=0):
        super().__init__(log)
        self.connected, self.fail_contexts = connected, fail_contexts

    def is_connected(self):
        return self.connected

    def new_context(self, **kwargs):
        if self.fail_contexts:
            self.fail_contexts -= 1
            raise PlaywrightError("Target page, context or browser has been closed")
        return super().new_context(**kwargs)


@pytest.mark.parametrize("first", [dict(connected=False), dict(fail_contexts=1)], ids=["disconnected", "context-error"])
def test_a_dead_browser_is_launched_again_for_the_next_search(first):
    log, launched = [], []

    def launcher(channel):
        browser = CrashingBrowser(log, **(first if not launched else {}))
        launched.append(browser)
        return browser

    session = br.BrowserSession(channel="chromium", launcher=launcher).start()
    with session.page():
        pass
    assert len(launched) == 2 and session._browser is launched[1]


def test_a_browser_that_keeps_failing_raises_instead_of_looping():
    log = []
    session = br.BrowserSession(channel="chromium", launcher=lambda c: CrashingBrowser(log, fail_contexts=5)).start()
    with pytest.raises(PlaywrightError):
        with session.page():
            pass


def test_read_html_turns_a_playwright_error_into_a_page_load_error():
    def content():
        raise PlaywrightError("Execution context was destroyed")
    with pytest.raises(br.NavigationFailed):
        br.read_html(types.SimpleNamespace(content=content), "https://example.invalid/x")
