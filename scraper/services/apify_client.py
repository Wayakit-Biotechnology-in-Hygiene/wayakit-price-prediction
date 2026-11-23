"""Minimal Apify REST client for the F6 adapters (Amazon KSA and Noon KSA).

What it does:
- Starts one actor run, polls it until it ends, and pages through its default dataset.
- Puts a cap on each run (`maxTotalChargeUsd`, `maxItems`, `timeout`) and keeps a USD budget
  shared by every run of the process (one scraper process = one pipeline run). A run is only
  started if the spend so far plus that run's cap fits in the budget; otherwise
  `ApifyBudgetExceeded` stops the scraping (exit 1).
- Retries HTTP 429/5xx and network errors with backoff (a run start only retries HTTP 429,
  because a 5xx or a lost response may have started a paid run), and re-starts a run that ends
  FAILED, TIMED-OUT or ABORTED or whose start failed (`run_attempts`, each with its own
  reservation).

Errors:
- `ApifyFatalError`: stops the whole scraping run (token missing or rejected, any other HTTP
  4xx, budget exceeded). `scraper/main.py` turns it into `ScrapingAborted`.
- `ApifyRunFailed`: only this keyword failed (actor failed twice, network exhausted).
  `scraper/main.py` records it in the coverage report and exits 1 at the end.

Secrets: the token is the key `APIFY_API_TOKEN` of `prod/wayakit-app`, read only through
`pi_secrets` and only when the first client is built (`shared_client()`); importing this module
touches neither AWS nor the network. The token only travels in the `Authorization: Bearer`
header: it is never put in a URL, a log line, an exception message or `repr()`. Exceptions
never carry response bodies and are raised `from None`, so `exc_info=True` cannot print a
chained urllib traceback.

Usage (tests inject `urlopen`, `sleep` and `clock`):
    from services.apify_client import shared_client
    items = shared_client().run_actor("junglee~amazon-crawler", actor_input, max_items=50,
                                      max_usd=0.5, timeout_s=1200)
"""
import http.client
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

from log_config import get_logger

logger = get_logger()

API_BASE = "https://api.apify.com/v2"
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})
RETRY_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
HTTP_ATTEMPTS = 3            # backoff sleep(5 * attempt): 5 s, 10 s
HTTP_TIMEOUT_S = 90          # > waitForFinish
POLL_WAIT_S = 60             # GET /actor-runs/{id}?waitForFinish=60
DEADLINE_SLACK_S = 120
PAGE_LIMIT = 1000
TOKEN_KEY = "APIFY_API_TOKEN"

_SHARED = None


class ApifyError(Exception):
    """Base class. Never carries the token, a URL with a token, or a response body."""


class ApifyFatalError(ApifyError):
    """Stops the whole scraping run (exit 1)."""


class ApifyBudgetExceeded(ApifyFatalError):
    """The next run would push the spend of this pipeline run over the budget."""


class ApifyRunFailed(ApifyError):
    """This keyword's Apify work failed; the scraping continues and ends with exit 1."""


def load_token(secret_reader=None) -> str:
    """The Apify token from `prod/wayakit-app` (key `APIFY_API_TOKEN`), via `pi_secrets` only."""
    from pi_secrets import get_secret, require_keys, SecretError  # lazy: no AWS on import

    reader = secret_reader or get_secret
    try:
        return require_keys(reader(), (TOKEN_KEY,))[TOKEN_KEY]
    except SecretError as e:
        raise ApifyFatalError(
            f"Apify token not available ({type(e).__name__}); key {TOKEN_KEY} in prod/wayakit-app"
        ) from None
    except Exception as e:
        # Same fatal error for anything else (e.g. no AWS credentials); only the class name is
        # kept because the message of an AWS error can carry ARNs.
        raise ApifyFatalError(
            f"Apify token not available ({type(e).__name__}); key {TOKEN_KEY} in prod/wayakit-app"
        ) from None


def shared_client() -> "ApifyClient":
    """The process-wide client: reads the secret once and tracks the budget of the whole run,
    although `make_scrapers` is called once per subindustry."""
    global _SHARED
    if _SHARED is None:
        import config  # lazy: scraper/config.py, on sys.path when run from scraper/main.py
        _SHARED = ApifyClient(load_token(), budget_usd=config.APIFY_RUN_BUDGET_USD,
                              run_attempts=config.APIFY_RUN_ATTEMPTS,
                              memory_mb=config.APIFY_MEMORY_MB)
    return _SHARED


def preflight(client=None) -> None:
    """Checks the token before scraping starts (raises `ApifyFatalError` if it is missing or
    rejected)."""
    (client or shared_client()).check_token()


def _is_cost(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


class ApifyClient:
    def __init__(self, token: str, *, budget_usd: float, urlopen=urllib.request.urlopen,
                 sleep=time.sleep, clock=time.monotonic, run_attempts: int = 2,
                 memory_mb: int = 1024):
        if not isinstance(token, str) or not token.strip():
            raise ApifyFatalError("empty Apify token") from None
        token = token.strip()
        # A pasted token can keep a newline or a stray space: http.client would then raise a
        # ValueError whose message contains the whole header, i.e. the token. Refuse it here,
        # without the value.
        if any(ch.isspace() or not ch.isprintable() for ch in token):
            raise ApifyFatalError(f"invalid Apify token format (key {TOKEN_KEY})") from None
        self._token = token
        self._budget = float(budget_usd)
        self._urlopen = urlopen
        self._sleep = sleep
        self._clock = clock
        self._run_attempts = max(1, int(run_attempts))
        self._memory_mb = int(memory_mb)
        self._settled = 0.0   # real (or capped) cost of every finished or lost run
        self._reserved = 0.0  # cap of the run in flight

    @property
    def spent_usd(self) -> float:
        """Cumulative USD of every run this client started (reservations while in flight)."""
        return self._settled + self._reserved

    def __repr__(self) -> str:
        return f"ApifyClient(budget_usd={self._budget:.2f}, spent_usd={self.spent_usd:.2f})"

    # --- HTTP ------------------------------------------------------------------------------

    def _request(self, method, path, params=None, body=None, retry_network=True):
        """One API call with retries. `retry_network=False` is for a run start: a lost response
        or an HTTP 5xx may still have started a (paid) run, so only HTTP 429 (never started) is
        retried here; `run_actor` starts again with a fresh reservation."""
        url = API_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        data = json.dumps(body).encode() if body is not None else None
        reason = ""
        for attempt in range(1, HTTP_ATTEMPTS + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
                    raw = response.read()
            except urllib.error.HTTPError as e:
                code = e.code
                if code in RETRY_HTTP_CODES:
                    reason = f"HTTP {code}"
                    if not retry_network and code != 429:
                        raise ApifyRunFailed(f"Apify {method} {path}: {reason}") from None
                elif 400 <= code < 500:
                    raise ApifyFatalError(
                        f"Apify {method} {path}: HTTP {code}{_error_detail(e)}") from None
                else:
                    raise ApifyRunFailed(f"Apify {method} {path}: HTTP {code}") from None
            except (OSError, http.client.HTTPException) as e:
                # URLError, TimeoutError, ConnectionError; IncompleteRead, BadStatusLine (not
                # OSErrors) are transient network errors too.
                reason = type(e).__name__
                if not retry_network:
                    raise ApifyRunFailed(f"Apify {method} {path}: {reason}") from None
            except ValueError as e:
                # Raised by http.client for an invalid header or URL; its message can contain the
                # Authorization header, so only the class name is kept.
                raise ApifyFatalError(
                    f"Apify {method} {path}: invalid request ({type(e).__name__})") from None
            else:
                if not raw:
                    return None
                try:
                    return json.loads(raw)
                except ValueError:
                    raise ApifyRunFailed(f"Apify {method} {path}: invalid JSON response") from None
            if attempt < HTTP_ATTEMPTS:
                self._sleep(5 * attempt)
        raise ApifyRunFailed(f"Apify {method} {path}: {reason}") from None

    # --- API -------------------------------------------------------------------------------

    def check_token(self) -> None:
        data = self._request("GET", "/users/me/limits") or {}
        usage = ((data.get("data") or {}).get("current") or {}).get("monthlyUsageUsd") \
            if isinstance(data, dict) else None
        if _is_cost(usage):
            logger.info(f"Apify token OK; account monthly usage USD {usage:.2f}")
        else:
            logger.info("Apify token OK")

    def run_actor(self, actor: str, actor_input: dict, *, max_items: int, max_usd: float,
                  timeout_s: int) -> list:
        """Runs `actor` once (one more time if it ends FAILED/TIMED-OUT/ABORTED) and returns its
        dataset items. Raises `ApifyBudgetExceeded` before starting a run that does not fit."""
        status = "NOT-STARTED"
        for attempt in range(1, self._run_attempts + 1):
            if self.spent_usd + max_usd > self._budget + 1e-9:
                raise ApifyBudgetExceeded(
                    f"Apify budget USD {self._budget:.2f} would be exceeded: spent "
                    f"{self.spent_usd:.2f}, next run cap {max_usd:.2f}") from None
            # Reserve the run's cap: it is what the run may cost while it is in flight.
            self._reserved = max_usd
            params = {"maxItems": max_items, "maxTotalChargeUsd": max_usd,
                      "timeout": timeout_s, "memory": self._memory_mb}
            try:
                # No network or 5xx retry: a lost POST response may still have started a (paid)
                # run, so the reservation stays counted. Only HTTP 429 is retried.
                started = self._request("POST", f"/acts/{actor}/runs", params=params,
                                        body=actor_input, retry_network=False)
            except ApifyRunFailed as error:
                # The lost start may have been a paid run: its cap stays counted.
                self._settled += max_usd
                self._reserved = 0.0
                status = "START-FAILED"
                logger.warning(f"Apify {actor} start failed (attempt {attempt}/"
                               f"{self._run_attempts}): {error}")
                continue
            try:
                try:
                    run_id = started["data"]["id"]
                    dataset_id = started["data"]["defaultDatasetId"]
                except (TypeError, KeyError):
                    raise ApifyRunFailed(f"Apify {actor}: unexpected run start response") from None
                run, status = self._wait(run_id, timeout_s)
            except BaseException:
                # The run's cost is unknown: keep its cap counted, then let the error propagate.
                self._settled += self._reserved
                self._reserved = 0.0
                raise
            usd = run.get("usageTotalUsd")
            if not _is_cost(usd):
                usd = max_usd  # unknown cost: count the cap (conservative)
            # Replace the reservation with the real cost.
            self._settled += usd
            self._reserved = 0.0
            logger.info(f"Apify {actor} run {run_id}: {status}, USD {usd:.4f} "
                        f"(spent {self.spent_usd:.2f} of {self._budget:.2f})")
            if status == "SUCCEEDED":
                items = self._dataset_items(dataset_id)
                logger.info(f"Apify {actor} run {run_id}: {len(items)} items")
                return items
            logger.warning(f"Apify {actor} run {run_id} ended {status} "
                           f"(attempt {attempt}/{self._run_attempts})")
        raise ApifyRunFailed(
            f"Apify {actor} failed after {self._run_attempts} attempts (last status {status})") from None

    def _wait(self, run_id, timeout_s):
        """Polls the run until it ends; aborts it past our own deadline. Returns (run, status)."""
        deadline = self._clock() + timeout_s + DEADLINE_SLACK_S
        while True:
            run = self._get_run(run_id)
            status = run.get("status")
            if status in TERMINAL_STATUSES:
                return run, status
            if self._clock() > deadline:
                logger.warning(f"Apify run {run_id} passed its deadline; aborting it")
                self._request("POST", f"/actor-runs/{run_id}/abort")
                run = self._get_run(run_id)
                status = run.get("status")
                if status not in TERMINAL_STATUSES:
                    status = "ABORTED"
                return run, status

    def _get_run(self, run_id):
        response = self._request("GET", f"/actor-runs/{run_id}",
                                 params={"waitForFinish": POLL_WAIT_S})
        run = response.get("data") if isinstance(response, dict) else None
        if not isinstance(run, dict):
            raise ApifyRunFailed(f"Apify run {run_id}: unexpected run response") from None
        return run

    def _dataset_items(self, dataset_id):
        items = []
        offset = 0
        while True:
            page = self._request("GET", f"/datasets/{dataset_id}/items",
                                 params={"clean": "true", "format": "json",
                                         "offset": offset, "limit": PAGE_LIMIT})
            if not isinstance(page, list):
                raise ApifyRunFailed("unexpected dataset page") from None
            items.extend(page)
            if len(page) < PAGE_LIMIT:
                return items
            offset += len(page)


def _error_detail(error):
    """' (<error type>)' from an Apify error body, never the body itself."""
    try:
        body = json.loads(error.read() or b"")
        error_type = body["error"]["type"]
    except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError, AttributeError):
        return ""  # optional detail only; the HTTP code is already in the message
    return f" ({error_type})" if isinstance(error_type, str) and error_type else ""
