"""Unit tests of `scraper/services/apify_client.py` (F6): the Apify REST client.

Everything runs against `FakeApify` (an `urlopen` double), a fake clock and a recorded sleep: no
network, no secrets. What it proves:
- a full run: POST /acts/{actor}/runs with the per-run caps as query params and the input as the
  body, polling with waitForFinish=60, dataset pages (clean, json, limit 1000) until a short page;
- every request has the Bearer header and JSON content type; the token is never in a URL, a log,
  an exception or repr();
- HTTP retries (429/5xx and network errors on GET: sleeps 5, 10) and fatal 4xx without retry;
- a lost POST response is not retried at HTTP level and its reservation stays counted;
- FAILED / TIMED-OUT runs get one more attempt; the deadline aborts a stuck run;
- the process-wide USD budget: a run whose cap would push spend over the budget is refused
  before any request (ApifyBudgetExceeded); a missing usageTotalUsd counts as the cap;
- token loading through pi_secrets (key APIFY_API_TOKEN, names only in errors) and the cached
  shared client.
"""
from __future__ import annotations

import http.client as http_client
import logging
import sys
import urllib.error

import pytest

from tests.conftest import load_module
from tests.unit._fake_apify import FAKE_TOKEN, FakeApify, FakeClock, Http, Net, script_run

pytestmark = pytest.mark.unit

m = load_module("scraper/main.py")  # puts scraper/ and the repo root on sys.path
from services import apify_client as ac  # noqa: E402  (same module object main.py uses)

import pi_secrets  # noqa: E402

ACTOR = "junglee~amazon-crawler"
RUN_PATH = f"/acts/{ACTOR}/runs"
INPUT = {"categoryOrProductUrls": [{"url": "https://www.amazon.sa/s?k=glass+cleaner"}], "maxItemsPerStartUrl": 50}
RAW_BODY_MARK = "RAW-BODY-should-never-be-shown"


def _client(fake, *, budget=20.0, attempts=2, sleeps=None, clock=None, token=FAKE_TOKEN):
    return ac.ApifyClient(token, budget_usd=budget, urlopen=fake,
                          sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
                          clock=clock or FakeClock(), run_attempts=attempts, memory_mb=1024)


def _run(client, max_usd=0.5, max_items=50, timeout_s=1200):
    return client.run_actor(ACTOR, INPUT, max_items=max_items, max_usd=max_usd, timeout_s=timeout_s)


@pytest.fixture
def client_logs(caplog):
    """The test log_config logger does not propagate: attach caplog to it."""
    logger = sys.modules["log_config"].get_logger()
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            yield caplog
    finally:
        logger.removeHandler(caplog.handler)


# --- 1. a full run ----------------------------------------------------------------------------------
def test_full_run_start_poll_and_paginate():
    fake = FakeApify()
    page1 = [{"i": i} for i in range(1000)]
    page2 = [{"i": 1000 + i} for i in range(5)]
    script_run(fake, polls=("READY", "RUNNING"), usd=0.2, pages=(page1, page2))
    client = _client(fake)
    items = _run(client, max_usd=0.5, max_items=50, timeout_s=1200)
    assert items == page1 + page2 and len(items) == 1005

    [start] = fake.calls("POST", RUN_PATH)
    assert start.query == {"maxItems": "50", "maxTotalChargeUsd": "0.5", "timeout": "1200", "memory": "1024"}
    assert start.body == INPUT, "the actor input goes only in the body"
    polls = fake.calls("GET", "/actor-runs/run1")
    assert len(polls) == 3 and all(p.query == {"waitForFinish": "60"} for p in polls)
    pages = fake.calls("GET", "/datasets/ds1/items")
    assert [p.query.get("offset", "0") for p in pages] == ["0", "1000"]
    assert all(p.query["clean"] == "true" and p.query["format"] == "json" and p.query["limit"] == "1000"
               for p in pages)
    assert all(r.timeout == ac.HTTP_TIMEOUT_S for r in fake.requests)
    assert client.spent_usd == pytest.approx(0.2), "the reservation is replaced by the real cost"


def test_query_param_names_and_constants():
    assert ac.API_BASE == "https://api.apify.com/v2"
    assert ac.TERMINAL_STATUSES == {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
    assert ac.RETRY_HTTP_CODES == {429, 500, 502, 503, 504}
    assert (ac.HTTP_ATTEMPTS, ac.POLL_WAIT_S, ac.DEADLINE_SLACK_S, ac.PAGE_LIMIT) == (3, 60, 120, 1000)
    assert ac.HTTP_TIMEOUT_S > ac.POLL_WAIT_S
    assert ac.TOKEN_KEY == "APIFY_API_TOKEN"
    assert issubclass(ac.ApifyBudgetExceeded, ac.ApifyFatalError)
    assert issubclass(ac.ApifyFatalError, ac.ApifyError) and issubclass(ac.ApifyRunFailed, ac.ApifyError)
    assert not issubclass(ac.ApifyRunFailed, ac.ApifyFatalError)


# --- 2. headers and token ---------------------------------------------------------------------------
def test_every_request_has_bearer_and_json_headers_and_no_token_in_urls():
    fake = FakeApify()
    fake.add("GET", "/users/me/limits", {"data": {"current": {"monthlyUsageUsd": 1.5}}})
    script_run(fake, pages=([{"a": 1}],))
    client = _client(fake)
    client.check_token()
    _run(client)
    assert len(fake.requests) >= 4
    for r in fake.requests:
        assert r.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
        assert r.headers["content-type"] == "application/json"
        assert FAKE_TOKEN not in r.url
        assert "token" not in r.query


# --- 3. retries -----------------------------------------------------------------------------------
@pytest.mark.parametrize("failures", [
    (Http(429), Http(500)),
    (Http(503), Net(urllib.error.URLError("fake connection reset"))),
    (Net(TimeoutError()), Http(502)),
])
def test_get_is_retried_on_429_5xx_and_network_errors(failures):
    fake = FakeApify()
    fake.add("POST", RUN_PATH, {"data": {"id": "run1", "defaultDatasetId": "ds1"}})
    fake.add("GET", "/actor-runs/run1", *failures,
             {"data": {"id": "run1", "status": "SUCCEEDED", "usageTotalUsd": 0.1}})
    fake.add("GET", "/datasets/ds1/items", [{"x": 1}])
    sleeps = []
    assert _run(_client(fake, sleeps=sleeps)) == [{"x": 1}]
    assert sleeps == [5, 10]
    assert len(fake.calls("GET", "/actor-runs/run1")) == 3


def test_retries_exhausted_raise_run_failed():
    fake = FakeApify()
    fake.add("GET", "/users/me/limits", Http(500), Http(429), Net(urllib.error.URLError("fake down")))
    sleeps = []
    with pytest.raises(ac.ApifyRunFailed) as info:
        _client(fake, sleeps=sleeps).check_token()
    assert sleeps == [5, 10]
    assert len(fake.requests) == ac.HTTP_ATTEMPTS
    assert "fake down" not in str(info.value), "the reason is a code or a class name, never str(e)"
    assert info.value.__cause__ is None and info.value.__suppress_context__


# --- 4. fatal 4xx -----------------------------------------------------------------------------------
@pytest.mark.parametrize("code", [400, 401, 402, 403, 404])
def test_4xx_is_fatal_without_retry(code):
    fake = FakeApify()
    fake.add("POST", RUN_PATH, Http(code, {"error": {"type": "fake-error-type", "message": RAW_BODY_MARK}}))
    sleeps = []
    with pytest.raises(ac.ApifyFatalError) as info:
        _run(_client(fake, sleeps=sleeps))
    assert len(fake.requests) == 1 and sleeps == []
    text = str(info.value)
    assert f"HTTP {code}" in text and "fake-error-type" in text
    assert RAW_BODY_MARK not in text and FAKE_TOKEN not in text
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_401_with_a_non_json_body_has_no_detail():
    fake = FakeApify()
    fake.add("GET", "/users/me/limits", Http(401, f"<html>{RAW_BODY_MARK}</html>"))
    with pytest.raises(ac.ApifyFatalError) as info:
        _client(fake).check_token()
    assert "HTTP 401" in str(info.value) and RAW_BODY_MARK not in str(info.value)


# --- 5. lost POST response --------------------------------------------------------------------------
def test_post_network_error_is_not_retried_and_keeps_the_reservation():
    fake = FakeApify()
    fake.add("POST", RUN_PATH, Net(urllib.error.URLError("fake reset")))
    client = _client(fake, attempts=1)
    with pytest.raises(ac.ApifyRunFailed):
        _run(client, max_usd=0.5)
    assert len(fake.requests) == 1, "a lost POST response might have started a run: no HTTP retry"
    assert client.spent_usd == pytest.approx(0.5)


def test_post_network_error_then_a_second_attempt_succeeds():
    fake = FakeApify()
    fake.add("POST", RUN_PATH, Net(urllib.error.URLError("fake reset")))
    script_run(fake, run_id="run2", dataset_id="ds2", usd=0.1, pages=([{"ok": 1}],))
    client = _client(fake, attempts=2)
    assert _run(client, max_usd=0.5) == [{"ok": 1}]
    assert len(fake.calls("POST", RUN_PATH)) == 2
    assert client.spent_usd == pytest.approx(0.5 + 0.1)


def test_post_429_is_still_retried():
    fake = FakeApify()
    fake.add("POST", RUN_PATH, Http(429))
    script_run(fake, pages=([{"ok": 1}],))
    sleeps = []
    assert _run(_client(fake, sleeps=sleeps)) == [{"ok": 1}]
    assert sleeps == [5]


# --- 6. failed runs ---------------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["FAILED", "TIMED-OUT", "ABORTED"])
def test_a_failed_run_is_started_once_more(status):
    fake = FakeApify()
    script_run(fake, run_id="run1", status=status, usd=0.1)
    script_run(fake, run_id="run2", dataset_id="ds2", usd=0.2, pages=([{"ok": 1}],))
    client = _client(fake)
    assert _run(client) == [{"ok": 1}]
    assert client.spent_usd == pytest.approx(0.3)


def test_two_failed_runs_raise_run_failed_and_count_both_costs():
    fake = FakeApify()
    script_run(fake, run_id="run1", status="FAILED", usd=0.1)
    script_run(fake, run_id="run2", status="FAILED", usd=0.2)
    client = _client(fake, attempts=2)
    with pytest.raises(ac.ApifyRunFailed) as info:
        _run(client)
    assert "FAILED" in str(info.value)
    assert len(fake.calls("POST", RUN_PATH)) == 2
    assert not fake.calls("GET", "/datasets/ds1/items")
    assert client.spent_usd == pytest.approx(0.3)


# --- 7. deadline ------------------------------------------------------------------------------------
def test_deadline_aborts_a_stuck_run_and_retries():
    fake = FakeApify()
    clock = FakeClock(0.0)
    state = {"aborted": False}

    def poll_run1(record):
        clock.now = 1200 + ac.DEADLINE_SLACK_S + 1  # the run is still going after the deadline
        status = "ABORTED" if state["aborted"] else "RUNNING"
        return {"data": {"id": "run1", "status": status, "usageTotalUsd": 0.05}}

    def abort(record):
        state["aborted"] = True
        return {"data": {"id": "run1", "status": "ABORTING"}}

    fake.add("POST", RUN_PATH, {"data": {"id": "run1", "defaultDatasetId": "ds1"}})
    fake.add("GET", "/actor-runs/run1", *([poll_run1] * 5))
    fake.add("POST", "/actor-runs/run1/abort", abort)
    script_run(fake, run_id="run2", dataset_id="ds2", usd=0.1, polls=(), pages=([{"ok": 1}],))
    client = _client(fake, clock=clock)
    assert _run(client, timeout_s=1200) == [{"ok": 1}]
    assert len(fake.calls("POST", "/actor-runs/run1/abort")) == 1
    assert not fake.calls("GET", "/datasets/ds1/items"), "an aborted run's dataset is not read"
    assert client.spent_usd == pytest.approx(0.05 + 0.1)


# --- 8. budget --------------------------------------------------------------------------------------
def test_budget_refuses_the_run_that_would_exceed_it_before_any_request():
    fake = FakeApify()
    script_run(fake, run_id="run1", usd=0.4)
    script_run(fake, run_id="run2", dataset_id="ds2", usd=0.4)
    client = _client(fake, budget=1.0)
    assert _run(client, max_usd=0.5) == []
    assert _run(client, max_usd=0.5) == []
    assert client.spent_usd == pytest.approx(0.8)
    before = len(fake.requests)
    with pytest.raises(ac.ApifyBudgetExceeded) as info:
        _run(client, max_usd=0.5)  # 0.8 + 0.5 > 1.0
    assert len(fake.requests) == before, "no request for a refused run"
    assert isinstance(info.value, ac.ApifyFatalError)
    assert "1.00" in str(info.value) and client.spent_usd == pytest.approx(0.8)


def test_a_run_exactly_at_the_budget_is_allowed():
    fake = FakeApify()
    script_run(fake, usd=0.5)
    client = _client(fake, budget=0.5)
    assert _run(client, max_usd=0.5) == []
    assert client.spent_usd == pytest.approx(0.5)


@pytest.mark.parametrize("usd", ["missing", None, -1, float("nan"), float("inf"), "abc"])
def test_missing_or_invalid_usage_counts_as_the_cap(usd):
    fake = FakeApify()
    if usd == "missing":
        script_run(fake, usd="missing")
    else:
        script_run(fake, usd=None)
        fake.routes[("GET", "/actor-runs/run1")][-1]["data"]["usageTotalUsd"] = usd
    client = _client(fake)
    _run(client, max_usd=0.5)
    assert client.spent_usd == pytest.approx(0.5)


# --- 9. check_token ---------------------------------------------------------------------------------
def test_check_token_reads_the_limits(client_logs):
    fake = FakeApify()
    fake.add("GET", "/users/me/limits", {"data": {"current": {"monthlyUsageUsd": 3.25}}})
    client = _client(fake)
    client.check_token()
    [r] = fake.requests
    assert (r.method, r.path) == ("GET", "/users/me/limits")
    assert client.spent_usd == 0
    assert "3.25" in client_logs.text


def test_check_token_without_usage_still_passes():
    fake = FakeApify()
    fake.add("GET", "/users/me/limits", {"data": {}})
    _client(fake).check_token()


@pytest.mark.parametrize("code", [401, 403])
def test_check_token_auth_error_is_fatal(code):
    fake = FakeApify()
    fake.add("GET", "/users/me/limits", Http(code, {"error": {"type": "token-not-valid"}}))
    with pytest.raises(ac.ApifyFatalError):
        _client(fake).check_token()
    assert len(fake.requests) == 1


def test_preflight_uses_the_given_client():
    calls = []

    class C:
        def check_token(self):
            calls.append(1)

    ac.preflight(C())
    assert calls == [1]


# --- 10. load_token ---------------------------------------------------------------------------------
def test_load_token_reads_the_key():
    assert ac.load_token(secret_reader=lambda: {"APIFY_API_TOKEN": "t", "ODOO_URL": "x"}) == "t"


@pytest.mark.parametrize("secret", [{}, {"APIFY_API_TOKEN": "   "}, {"APIFY_API_TOKEN": None},
                                    {"OTHER_KEY": "other-secret-value-xyz"}])
def test_load_token_missing_key_names_the_key_only(secret):
    with pytest.raises(ac.ApifyFatalError) as info:
        ac.load_token(secret_reader=lambda: secret)
    assert "APIFY_API_TOKEN" in str(info.value)
    assert "other-secret-value-xyz" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


@pytest.mark.parametrize("error", [pi_secrets.SecretError("Could not read secret 'prod/wayakit-app': AccessDenied"),
                                   RuntimeError("unexpected")])
def test_load_token_reader_failure_is_fatal(error):
    def reader():
        raise error

    with pytest.raises(ac.ApifyFatalError) as info:
        ac.load_token(secret_reader=reader)
    assert "APIFY_API_TOKEN" in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


# --- 11. shared client --------------------------------------------------------------------------------
def test_shared_client_is_built_once(monkeypatch):
    loads = []
    monkeypatch.setattr(ac, "_SHARED", None)
    monkeypatch.setattr(ac, "load_token", lambda *a, **k: loads.append(1) or FAKE_TOKEN)
    first = ac.shared_client()
    second = ac.shared_client()
    assert first is second and loads == [1]
    assert f"budget_usd={m.config.APIFY_RUN_BUDGET_USD:.2f}" in repr(first)


def test_shared_client_token_failure_is_fatal(monkeypatch):
    monkeypatch.setattr(ac, "_SHARED", None)

    def no_token(*a, **k):
        raise ac.ApifyFatalError("Apify token not available (fake)")

    monkeypatch.setattr(ac, "load_token", no_token)
    with pytest.raises(ac.ApifyFatalError):
        ac.shared_client()
    with pytest.raises(ac.ApifyFatalError):
        ac.preflight()


# --- 12. no token anywhere ----------------------------------------------------------------------------
@pytest.mark.parametrize("token", ["", None, 123])
def test_empty_or_non_string_token_is_fatal(token):
    with pytest.raises(ac.ApifyFatalError):
        ac.ApifyClient(token, budget_usd=1.0, urlopen=FakeApify())


def test_repr_and_logs_never_show_the_token(client_logs):
    fake = FakeApify()
    fake.add("GET", "/users/me/limits", {"data": {"current": {"monthlyUsageUsd": 1.0}}})
    script_run(fake, run_id="run1", status="FAILED", usd=0.1)
    script_run(fake, run_id="run2", dataset_id="ds2", usd=0.1, pages=([{"a": 1}],))
    client = _client(fake)
    client.check_token()
    _run(client)
    text = repr(client)
    assert FAKE_TOKEN not in text and text.startswith("ApifyClient(budget_usd=20.00, spent_usd=0.")
    assert client_logs.records, "the client logs its runs"
    assert FAKE_TOKEN not in client_logs.text
    assert "run1" in client_logs.text and "FAILED" in client_logs.text


# --- review round 1: token format, http.client errors, no 5xx retry on a run start ----------------
@pytest.mark.parametrize("suffix", ["\n", "\r", "\r\n"])
def test_a_trailing_newline_is_stripped_not_sent(suffix):
    fake = FakeApify()
    fake.add("GET", "/users/me/limits", {"data": {}})
    _client(fake, token=FAKE_TOKEN + suffix).check_token()
    assert fake.requests[0].headers["authorization"] == f"Bearer {FAKE_TOKEN}"


@pytest.mark.parametrize("token", ["abc def", "abc\ndef", "abc\tdef", "abc\x00def", "abc\x7fdef"])
def test_a_token_with_inner_whitespace_or_control_chars_is_fatal_without_the_value(token, client_logs):
    with pytest.raises(ac.ApifyFatalError) as info:
        ac.ApifyClient(token, budget_usd=1.0, urlopen=FakeApify())
    assert token not in str(info.value) and "abc" not in str(info.value)
    assert "abc" not in client_logs.text


def test_a_value_error_from_urlopen_never_carries_the_header(client_logs):
    """http.client raises ValueError('Invalid header value b"Bearer <token>..."') for a bad header."""
    def urlopen(request, timeout=None):
        raise ValueError(f"Invalid header value {request.get_header('Authorization')!r}")

    client = ac.ApifyClient(FAKE_TOKEN, budget_usd=1.0, urlopen=urlopen, sleep=lambda s: None)
    with pytest.raises(ac.ApifyFatalError) as info:
        client.check_token()
    assert FAKE_TOKEN not in str(info.value) and "ValueError" in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert FAKE_TOKEN not in client_logs.text


@pytest.mark.parametrize("error", [http_client.IncompleteRead(b"partial"), http_client.BadStatusLine("x")])
def test_http_client_errors_are_retried_like_network_errors(error):
    fake = FakeApify()
    fake.add("GET", "/users/me/limits", Net(error), {"data": {}})
    sleeps = []
    _client(fake, sleeps=sleeps).check_token()
    assert sleeps == [5]


def test_http_client_errors_exhausted_become_apify_run_failed():
    fake = FakeApify()
    fake.add("GET", "/users/me/limits", *[Net(http_client.IncompleteRead(b"p")) for _ in range(3)])
    with pytest.raises(ac.ApifyRunFailed) as info:
        _client(fake).check_token()
    assert "IncompleteRead" in str(info.value)


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_a_5xx_on_run_start_is_not_retried_at_http_level(code):
    """A 5xx may come after Apify created the run: a retry would start a second paid run that is
    never counted. run_actor starts again with a fresh reservation instead."""
    fake = FakeApify()
    fake.add("POST", RUN_PATH, Http(code))
    script_run(fake, run_id="run2", dataset_id="ds2", usd=0.1, pages=([{"ok": 1}],))
    sleeps = []
    client = _client(fake, attempts=2, sleeps=sleeps)
    assert _run(client, max_usd=0.5) == [{"ok": 1}]
    assert len(fake.calls("POST", RUN_PATH)) == 2
    assert sleeps == []
    assert client.spent_usd == pytest.approx(0.5 + 0.1), "the 5xx start keeps its reservation"
