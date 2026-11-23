"""Fakes for the Apify tests (F6). No network, no secrets. Not a test file.

- ``FakeApify``: stands in for ``urllib.request.urlopen`` inside ``ApifyClient``. Each call gets a
  ``urllib.request.Request``; the fake records ``(method, path, query, headers, body, timeout)``,
  checks that the token never appears in the URL, and answers from a script keyed by
  ``(method, path)`` (path without the ``/v2`` prefix), consumed in order. A scripted answer is a
  dict or list (JSON body), ``None`` (empty body), ``Http(code, body)`` (raises ``HTTPError``),
  ``Net(exception)`` (raises it, e.g. ``URLError``) or a callable ``(record) -> answer``.
  A request with nothing scripted fails with ``AssertionError`` (an unexpected request).
- ``script_run``: scripts one actor run (start, polls, final status, dataset pages).
- ``FakeClient``: stands in for ``ApifyClient`` in the adapter tests: ``run_actor(...)`` records
  every call and returns scripted item lists or raises scripted exceptions, in order.
- ``FakeClock``: a monotonic clock the test can move.
"""
from __future__ import annotations

import copy
import email.message
import io
import json
import urllib.error
import urllib.parse
from dataclasses import dataclass, field

FAKE_TOKEN = "apify_api_FAKE-test-token-0000"  # obviously fake
API_HOST = "api.apify.com"


@dataclass
class Http:
    code: int
    body: object = None  # dict/list -> JSON, str -> raw text, None -> empty


@dataclass
class Net:
    error: BaseException


@dataclass
class Recorded:
    method: str
    path: str
    query: dict
    headers: dict
    body: object
    timeout: object
    url: str = field(repr=False, default="")


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload
        self.status = 200

    def read(self, *args):
        return self._payload

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def _encode(body) -> bytes:
    if body is None:
        return b""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode()
    return json.dumps(body).encode()


class FakeApify:
    def __init__(self, token: str = FAKE_TOKEN):
        self.token = token
        self.routes: dict[tuple[str, str], list] = {}
        self.requests: list[Recorded] = []

    def add(self, method: str, path: str, *answers) -> "FakeApify":
        self.routes.setdefault((method, path), []).extend(answers)
        return self

    def calls(self, method: str | None = None, path: str | None = None) -> list[Recorded]:
        return [r for r in self.requests
                if (method is None or r.method == method) and (path is None or r.path == path)]

    def __call__(self, request, timeout=None, **kwargs):
        url = request.full_url
        assert self.token not in url, "the Apify token must never be in a URL"
        parts = urllib.parse.urlsplit(url)
        assert parts.scheme == "https" and parts.netloc == API_HOST, f"unexpected host in {parts.netloc!r}"
        assert parts.path.startswith("/v2/"), parts.path
        path = parts.path[len("/v2"):]
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        headers = {k.lower(): v for k, v in request.header_items()}
        body = json.loads(request.data) if request.data else None
        record = Recorded(request.get_method(), path, query, headers, body, timeout, url)
        self.requests.append(record)
        queue = self.routes.get((record.method, path))
        if not queue:
            raise AssertionError(f"unexpected Apify request {record.method} {path}")
        answer = queue.pop(0)
        if callable(answer) and not isinstance(answer, (Http, Net)):
            answer = answer(record)
        if isinstance(answer, Http):
            raise urllib.error.HTTPError(url, answer.code, "fake HTTP error", email.message.Message(),
                                         io.BytesIO(_encode(answer.body)))
        if isinstance(answer, Net):
            raise answer.error
        if isinstance(answer, BaseException):
            raise answer
        return FakeResponse(_encode(answer))


def script_run(fake: FakeApify, *, actor: str = "junglee~amazon-crawler", run_id: str = "run1",
               dataset_id: str = "ds1", status: str = "SUCCEEDED", usd=0.1, polls=("RUNNING",),
               pages=([],)) -> FakeApify:
    """Scripts one run: POST start -> GET polls -> GET final status (with usageTotalUsd unless
    ``usd`` is the string "missing") -> dataset pages (only when it SUCCEEDED)."""
    fake.add("POST", f"/acts/{actor}/runs",
             {"data": {"id": run_id, "defaultDatasetId": dataset_id, "status": "READY"}})
    for status_in_progress in polls:
        fake.add("GET", f"/actor-runs/{run_id}", {"data": {"id": run_id, "status": status_in_progress}})
    final = {"id": run_id, "status": status, "defaultDatasetId": dataset_id}
    if usd != "missing":
        final["usageTotalUsd"] = usd
    fake.add("GET", f"/actor-runs/{run_id}", {"data": final})
    if status == "SUCCEEDED":
        for page in pages:
            fake.add("GET", f"/datasets/{dataset_id}/items", list(page))
    return fake


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeClient:
    """``run_actor`` double for the adapters. Each scripted result is a list of items, an
    exception instance (raised) or a callable ``(call_dict) -> result``."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []
        self.spent_usd = 0.0

    def run_actor(self, actor, actor_input, *, max_items, max_usd, timeout_s):
        call = {"actor": actor, "input": copy.deepcopy(actor_input), "max_items": max_items,
                "max_usd": max_usd, "timeout_s": timeout_s}
        self.calls.append(call)
        if not self.results:
            raise AssertionError(f"unexpected run_actor call for {actor}")
        result = self.results.pop(0)
        if callable(result) and not isinstance(result, BaseException):
            result = result(call)
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)
