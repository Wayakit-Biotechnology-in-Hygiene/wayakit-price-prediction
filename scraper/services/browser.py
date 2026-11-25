"""Shared Playwright browser for the site scrapers (replaces Selenium and ChromeDriver).

One Chromium process per scraping run, started once by ``scraper/main.py`` (``start_browser``)
and closed at the end; every ``scrape()`` call opens its own isolated context (cookies, cache)
with ``BrowserSession.page()`` and closes it when the search ends.

Browser: the installed Google Chrome (``channel='chrome'``, Chrome 146 on the instance) by
default, so the instance needs no browser download; if it cannot be launched, Playwright's own
Chromium (``python -m playwright install chromium``). ``PI_BROWSER_CHANNEL=chromium`` skips
Chrome. If neither starts, ``BrowserUnavailable`` is raised and the run exits 1.

Errors are never swallowed here: a page that answers HTTP >= 400, does not load, or never shows
the expected element raises a ``PageLoadError`` subclass, which the scraper lets propagate so
``scraper/main.py`` records its class name in the ``error`` column of the coverage report.
"""
import contextlib
import os
import time

from log_config import get_logger

logger = get_logger()

DEFAULT_CHANNEL = 'chrome'
NAVIGATION_TIMEOUT_MS = 45_000
DEFAULT_WAIT_MS = 15_000
VIEWPORT = {'width': 1920, 'height': 1080}
LOCALE = 'en-US'
# HTTP 429 / 503 (rate limit, e.g. AeroSense): wait and retry, honoring Retry-After up to the cap.
RETRY_STATUSES = (429, 503)
RETRY_WAITS_S = (15, 60)
MAX_RETRY_AFTER_S = 120
# Not needed to read a page's HTML: never downloaded (less load on the sites and their rate limits).
BLOCKED_RESOURCE_TYPES = frozenset({'image', 'media', 'font'})


class BrowserUnavailable(Exception):
    """Neither Chrome nor Playwright's Chromium could be launched (fatal for the run)."""


class PageLoadError(Exception):
    """A page could not be used for one search; the search fails, the run goes on."""


class HttpStatusError(PageLoadError):
    """The site answered HTTP >= 400 (for example 403 from a bot filter or 404 for a moved page)."""

    def __init__(self, status, url):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


class NavigationFailed(PageLoadError):
    """The page did not load (DNS, TLS, connection reset, navigation timeout)."""


class ElementNotFound(PageLoadError):
    """The page loaded but the expected element never appeared (site changed or blocked)."""


class SiteLayoutChanged(PageLoadError):
    """The page loaded but its content no longer matches what the scraper reads."""


class BrowserSession:
    """Lazily launched browser shared by every scraper of one run.

    ``launcher(channel)`` returns a Playwright ``Browser``; tests pass a fake one. Without it,
    ``sync_playwright()`` is started on the first ``start()``. ``user_agent`` overrides the
    browser's own one; leave it unset unless it matches the real browser version (a stale one is
    answered with HTTP 429 by AeroSense).
    """

    def __init__(self, channel=None, user_agent=None, launcher=None):
        self.channel = channel or os.environ.get('PI_BROWSER_CHANNEL') or DEFAULT_CHANNEL
        self.user_agent = user_agent
        self._launcher = launcher
        self._owns_launcher = launcher is None
        self._playwright = None
        self._browser = None
        self.launched_with = None

    def start(self):
        if self._browser is not None:
            return self
        if self._owns_launcher and self._playwright is None:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._launcher = self._launch_with_playwright
        errors = []
        for channel in self._channels():
            try:
                self._browser = self._launcher(channel)
                self.launched_with = channel
                logger.info(f"Browser started: {channel} {getattr(self._browser, 'version', '')}".rstrip())
                return self
            except Exception as error:  # noqa: BLE001 - each failure is collected and re-raised below
                errors.append(f"{channel}: {type(error).__name__}: {str(error).splitlines()[0] if str(error) else ''}")
                logger.warning(f"Could not launch the browser with channel '{channel}': {type(error).__name__}")
        self.close()
        raise BrowserUnavailable("; ".join(errors))

    def _drop_browser(self):
        """Forgets a crashed or disconnected browser so the next ``start()`` launches a new one
        (Playwright itself keeps running)."""
        if self._browser is not None:
            with contextlib.suppress(Exception):
                self._browser.close()
        self._browser = None

    def _new_context(self, **kwargs):
        """A new context; a browser that died mid-run (Chrome crash, out of memory) is launched
        again once, so one crash does not fail every remaining search of a 30 h run."""
        self.start()
        is_connected = getattr(self._browser, 'is_connected', None)
        if callable(is_connected) and not is_connected():
            logger.warning("The browser is no longer connected; launching it again.")
            self._drop_browser()
            self.start()
        try:
            return self._browser.new_context(**kwargs)
        except Exception as error:  # noqa: BLE001 - relaunched once; a second failure propagates
            logger.warning(f"Could not open a browser context ({type(error).__name__}); launching the browser again.")
            self._drop_browser()
            self.start()
            return self._browser.new_context(**kwargs)

    def _channels(self):
        return [self.channel, 'chromium'] if self.channel != 'chromium' else ['chromium']

    def _launch_with_playwright(self, channel):
        kwargs = {'headless': True, 'args': ['--no-sandbox', '--disable-dev-shm-usage']}
        if channel != 'chromium':
            kwargs['channel'] = channel
        return self._playwright.chromium.launch(**kwargs)

    @contextlib.contextmanager
    def page(self):
        """A fresh page in its own context; the context is closed on exit, also after an error."""
        context_kwargs = {'locale': LOCALE, 'viewport': VIEWPORT}
        if self.user_agent:
            context_kwargs['user_agent'] = self.user_agent
        context = self._new_context(**context_kwargs)
        try:
            context.route('**/*', _skip_heavy_resources)
            page = context.new_page()
            page.set_default_timeout(DEFAULT_WAIT_MS)
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
            yield page
        finally:
            with contextlib.suppress(Exception):
                context.close()

    def close(self):
        self._drop_browser()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                self._playwright.stop()
            self._playwright = None


def fetch_html(page, url, wait_for=None, timeout_ms=DEFAULT_WAIT_MS, sleep=time.sleep):
    """Loads ``url`` and returns the rendered HTML.

    HTTP 429 and 503 are retried ``len(RETRY_WAITS_S)`` times after a wait (``Retry-After`` when
    it is a number of seconds, capped). Raises ``HttpStatusError`` for HTTP >= 400 (after the
    retries), ``NavigationFailed`` when the page does not load and ``ElementNotFound`` when
    ``wait_for`` (a Playwright CSS selector; comma lists, ``:text()`` and ``:has-text()`` allowed)
    is not attached within ``timeout_ms``.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    for attempt in range(len(RETRY_WAITS_S) + 1):
        response, status = _goto(page, url, PlaywrightError)
        if status in RETRY_STATUSES and attempt < len(RETRY_WAITS_S):
            wait = retry_wait(response, attempt)
            logger.warning(f"      ! HTTP {status} for {url}; retrying in {wait} s.")
            sleep(wait)
            continue
        if status >= 400:
            raise HttpStatusError(status, url)
        break
    if wait_for:
        try:
            page.wait_for_selector(wait_for, state='attached', timeout=timeout_ms)
        except PlaywrightTimeout as error:
            raise ElementNotFound(f"'{wait_for}' not found in {timeout_ms} ms at {url}") from error
        except PlaywrightError as error:
            raise NavigationFailed(f"{url}: {_first_line(error)}") from error
    return read_html(page, url)


def read_html(page, url):
    """``page.content()``, with a page that navigated away or crashed while being read raised as
    ``NavigationFailed`` (a ``PageLoadError``) instead of a raw Playwright error."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        return page.content()
    except PlaywrightError as error:
        raise NavigationFailed(f"{url}: {_first_line(error)}") from error


def _goto(page, url, playwright_error):
    """``(response, status)`` of the main document. An error status with an empty body makes
    Chrome show its own error page and ``goto`` raise ``net::ERR_HTTP_RESPONSE_CODE_FAILURE``
    (Mumzworld's 403 does that); the status is then read from the document response seen by a
    listener, so it is still an ``HttpStatusError`` (429/503 retried) and not a navigation error."""
    statuses = []

    def on_response(response):
        if response.request.resource_type == 'document' and response.frame == page.main_frame:
            statuses.append((response, response.status))

    page.on('response', on_response)
    try:
        response = page.goto(url, wait_until='domcontentloaded')
    except playwright_error as error:
        _settle_error_page(page)
        if statuses and statuses[-1][1] >= 400:
            return statuses[-1]
        raise NavigationFailed(f"{url}: {_first_line(error)}") from error
    finally:
        page.remove_listener('response', on_response)
    return response, (response.status if response is not None else 200)


def _settle_error_page(page):
    """Waits until Chrome has shown its own error page after a failed ``goto``: until then the
    next ``goto`` on the same page (a retry, or the next product) is "interrupted by another
    navigation to chrome-error://" (seen live on 2026-11-25). Best effort, at most 5 s."""
    with contextlib.suppress(Exception):
        page.wait_for_url(lambda current: current.startswith('chrome-error://'), timeout=5_000)


def retry_wait(response, attempt):
    """Seconds to wait before retry ``attempt`` (0-based): a numeric ``Retry-After`` header,
    capped at ``MAX_RETRY_AFTER_S``, else ``RETRY_WAITS_S[attempt]``."""
    headers = getattr(response, 'headers', None) or {}
    value = str(headers.get('retry-after', '')).strip()
    if value.isdigit():
        return min(int(value), MAX_RETRY_AFTER_S)
    return RETRY_WAITS_S[attempt]


def collect_products(urls, fetch_details, keep, limit, label):
    """Visits product pages until ``limit`` products pass ``keep``.

    ``fetch_details(url)`` returns the product dict or raises ``PageLoadError``; a failed page is
    logged and skipped, but if **every** visited page failed the last error is raised, so a site
    that serves its search page and blocks every product page is still a failed search.
    """
    found, attempted, failed, last_error = [], 0, 0, None
    for url in urls:
        if len(found) >= limit:
            logger.info(f"    > Limit of {limit} products reached.")
            break
        attempted += 1
        try:
            details = fetch_details(url)
        except PageLoadError as error:
            failed += 1
            last_error = error
            logger.warning(f"      ! {label}: product page failed ({type(error).__name__}): {error}")
            continue
        if keep(details):
            found.append(details)
    if attempted and failed == attempted:
        raise last_error
    return found


def keep_if_relevant(relevance_agent, keyword):
    """The usual filter: a positive quantity, then the relevance AI (one title per call)."""
    def keep(details):
        title = (details.get('Product') or '')[:60]
        if not details.get('Total quantity', 0) > 0:
            logger.info(f"      -> DISCARDED (no quantity): {title}...")
            return False
        if relevance_agent.is_relevant(details.get('Product'), keyword):
            logger.info(f"      -> VALID: {title}...")
            return True
        logger.info(f"      -> DISCARDED (not relevant by AI): {title}...")
        return False
    return keep


def _skip_heavy_resources(route):
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def _first_line(error):
    text = str(error)
    return text.splitlines()[0] if text else type(error).__name__
