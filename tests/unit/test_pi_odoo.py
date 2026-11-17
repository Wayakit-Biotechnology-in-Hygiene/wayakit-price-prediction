"""Unit tests of `pi_odoo.py` (shared Odoo XML-RPC session) with a fake server proxy.

No test reaches Odoo: `connect()` receives a fake `server_proxy` factory and `OdooSession`
receives a fake `models` object that records each `execute_kw` call.
"""
from __future__ import annotations

import pytest

import pi_odoo as po
from pi_secrets import OdooCredentials

pytestmark = pytest.mark.unit

CREDS = OdooCredentials(url="https://odoo.example.invalid", db="FAKE-db", username="FAKE-user",
                        api_token="FAKE-token-0123456789")


class FakeModels:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def execute_kw(self, db, uid, token, model, method, args, kwargs=None):
        self.calls.append((db, uid, token, model, method, args, kwargs))
        return self.results.get((model, method))


class FakeCommon:
    def __init__(self, uid):
        self.uid = uid
        self.calls = []

    def authenticate(self, db, user, token, extra):
        self.calls.append((db, user, token, extra))
        return self.uid


def _proxy_factory(uid, models=None):
    built = []
    common = FakeCommon(uid)
    models = models or FakeModels()

    def factory(url):
        built.append(url)
        return common if url.endswith("/common") else models

    return factory, built, common, models


def test_connect_authenticates_and_returns_a_session():
    factory, built, common, models = _proxy_factory(uid=7)
    session = po.connect(CREDS, server_proxy=factory)
    assert built == ["https://odoo.example.invalid/xmlrpc/2/common",
                     "https://odoo.example.invalid/xmlrpc/2/object"]
    assert common.calls == [("FAKE-db", "FAKE-user", "FAKE-token-0123456789", {})]
    assert (session.models, session.db, session.uid) == (models, "FAKE-db", 7)
    assert "FAKE-token-0123456789" not in repr(session)


@pytest.mark.parametrize("uid", [False, None, 0])
def test_connect_without_uid_raises(uid):
    factory, built, _, _ = _proxy_factory(uid=uid)
    with pytest.raises(po.OdooError, match="authentication failed") as exc:
        po.connect(CREDS, server_proxy=factory)
    assert "FAKE-token" not in str(exc.value)
    assert built == ["https://odoo.example.invalid/xmlrpc/2/common"], "no models proxy without a uid"


def test_execute_forwards_to_execute_kw():
    models = FakeModels({("product.master", "search_read"): [{"id": 1}]})
    session = po.OdooSession(models=models, db="FAKE-db", uid=3, api_token="FAKE-token")
    out = session.execute("product.master", "search_read", [["a", "=", 1]], fields=["name"])
    assert out == [{"id": 1}]
    assert models.calls == [("FAKE-db", 3, "FAKE-token", "product.master", "search_read",
                             [[["a", "=", 1]]], {"fields": ["name"]})]


def test_single_company_is_returned():
    models = FakeModels({("res.company", "search_read"): [{"id": 1, "name": "Fake KSA Co"}]})
    session = po.OdooSession(models=models, db="FAKE-db", uid=3, api_token="FAKE-token")
    assert po.get_single_company(session) == (1, "Fake KSA Co")
    assert [c[3:5] for c in models.calls] == [("res.company", "search_read")]


@pytest.mark.parametrize("companies", [
    [],
    [{"id": 1, "name": "Fake KSA Co"}, {"id": 2, "name": "Fake MX Co"}],
], ids=["none", "two"])
def test_zero_or_several_companies_refuse_to_guess(companies):
    models = FakeModels({("res.company", "search_read"): companies})
    session = po.OdooSession(models=models, db="FAKE-db", uid=3, api_token="FAKE-token")
    with pytest.raises(po.OdooError, match="company filter is needed"):
        po.get_single_company(session)
