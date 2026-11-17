"""Unit tests of `pi_secrets.py` (the single module that reads `prod/wayakit-app`).

Everything uses a fake Secrets Manager client: no test reaches AWS. The secret values are
obviously fake and carry a marker (`FAKE-`) so the tests can check that no value ever ends up in
an exception message, a `repr()` or a log line.
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys

import pytest
from botocore.exceptions import ClientError

import pi_secrets as ps
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.unit

FAKE = {
    "ODOO_URL": "https://odoo.example.invalid/",
    "ODOO_DB": "FAKE-db",
    "ODOO_USERNAME": "FAKE-user",
    "ODOO_API_TOKEN": "FAKE-token-0123456789",
    "GEMINI_API_KEY": "FAKE-gemini-key",
}
SECRET_VALUES = [v for v in FAKE.values() if v.startswith("FAKE-")]


class FakeClient:
    """Fake `secretsmanager` client: records the calls and returns (or raises) what it is given."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _string(d) -> dict:
    return {"SecretString": json.dumps(d)}


def _assert_no_secret_values(text: str) -> None:
    leaked = [v for v in SECRET_VALUES if v in text]
    assert not leaked, f"secret values leaked: {leaked} in {text!r}"


# --- get_secret --------------------------------------------------------------------------------
def test_defaults_are_the_pi_secret_and_region():
    assert ps.SECRET_NAME == "prod/wayakit-app"
    assert ps.REGION_NAME == "eu-north-1"
    assert ps.ODOO_KEYS == ("ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "ODOO_API_TOKEN")


def test_get_secret_reads_the_default_secret_with_the_injected_client():
    client = FakeClient(_string(FAKE))
    assert ps.get_secret(client=client) == FAKE
    assert client.calls == [{"SecretId": "prod/wayakit-app"}]


@pytest.mark.parametrize("blob", [
    json.dumps(FAKE).encode(),                      # what boto3 returns (already decoded bytes)
    base64.b64encode(json.dumps(FAKE).encode()),    # legacy callers: base64 bytes
    base64.b64encode(json.dumps(FAKE).encode()).decode("ascii"),  # base64 text
], ids=["raw-bytes", "base64-bytes", "base64-str"])
def test_get_secret_accepts_secret_binary(blob):
    assert ps.get_secret(client=FakeClient({"SecretBinary": blob})) == FAKE


def test_secret_string_wins_over_binary():
    resp = {"SecretString": json.dumps({"a": "1"}), "SecretBinary": json.dumps({"a": "2"}).encode()}
    assert ps.parse_secret_response(resp) == {"a": "1"}


@pytest.mark.parametrize("response, match", [
    ({}, "neither SecretString nor SecretBinary"),
    ({"SecretString": None, "SecretBinary": None}, "neither SecretString nor SecretBinary"),
    ({"SecretString": "{'ODOO_API_TOKEN': 'FAKE-token-0123456789'"}, "not valid JSON"),
    ({"SecretString": json.dumps(["FAKE-token-0123456789"])}, "not a JSON object"),
    ({"SecretBinary": b"\xff\xfeFAKE-token-0123456789"}, "not valid JSON"),
])
def test_unparseable_secret_raises_secret_error_without_the_content(response, match):
    with pytest.raises(ps.SecretError, match=match) as exc:
        ps.get_secret(client=FakeClient(response))
    _assert_no_secret_values(str(exc.value))
    # `from None`: the JSONDecodeError (which carries the raw document) is not chained.
    assert exc.value.__cause__ is None
    assert "prod/wayakit-app" in str(exc.value)


@pytest.mark.parametrize("code", ["AccessDeniedException", "ResourceNotFoundException",
                                  "DecryptionFailureException"])
def test_aws_error_becomes_secret_error_with_the_code_only(code, caplog):
    error = ClientError({"Error": {"Code": code, "Message": "FAKE-token-0123456789 detail"}},
                        "GetSecretValue")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ps.SecretError, match=code) as exc:
            ps.get_secret(client=FakeClient(error=error))
    _assert_no_secret_values(str(exc.value))
    _assert_no_secret_values(caplog.text)
    assert code in caplog.text, "the failure must be logged (with the code)"


def test_non_aws_error_becomes_secret_error_with_the_class_name():
    with pytest.raises(ps.SecretError, match="ConnectionError"):
        ps.get_secret(client=FakeClient(error=ConnectionError("FAKE-token-0123456789")))


def test_without_client_it_builds_a_secretsmanager_client_in_eu_north_1(monkeypatch):
    """The default path builds a boto3 client (here a fake session: nothing reaches AWS)."""
    import boto3

    built = []

    class FakeSession:
        def client(self, **kwargs):
            built.append(kwargs)
            return FakeClient(_string(FAKE))

    monkeypatch.setattr(boto3.session, "Session", FakeSession)
    assert ps.get_secret() == FAKE
    assert built == [{"service_name": "secretsmanager", "region_name": "eu-north-1"}]


def test_importing_the_module_does_not_import_boto3():
    """Importing pi_secrets must not need AWS (dry-run paths and tests import it freely)."""
    code = "import sys, pi_secrets; print('boto3' in sys.modules)"
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                       timeout=60, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "False"


# --- get_odoo_credentials -----------------------------------------------------------------------
def test_odoo_credentials_from_an_injected_client():
    creds = ps.get_odoo_credentials(client=FakeClient(_string(FAKE)))
    assert creds == ps.OdooCredentials(url="https://odoo.example.invalid", db="FAKE-db",
                                       username="FAKE-user", api_token="FAKE-token-0123456789")


def test_odoo_credentials_from_an_already_read_secret_do_not_call_aws():
    client = FakeClient(error=AssertionError("must not be called"))
    creds = ps.get_odoo_credentials(secret=FAKE, client=client)
    assert creds.db == "FAKE-db" and client.calls == []


def test_trailing_slash_is_stripped_from_the_url():
    creds = ps.get_odoo_credentials(secret={**FAKE, "ODOO_URL": "https://odoo.example.invalid///"})
    assert creds.url == "https://odoo.example.invalid"
    assert f"{creds.url}/xmlrpc/2/object".count("//") == 1


def test_repr_of_credentials_hides_the_token():
    creds = ps.get_odoo_credentials(secret=FAKE)
    assert "FAKE-token-0123456789" not in repr(creds)
    assert "FAKE-token-0123456789" not in str(creds)


@pytest.mark.parametrize("missing", [("ODOO_API_TOKEN",), ("ODOO_URL", "ODOO_DB"), ps.ODOO_KEYS])
@pytest.mark.parametrize("how", ["absent", "none", "empty", "blank"])
def test_missing_odoo_keys_raise_listing_names_only(missing, how):
    secret = dict(FAKE)
    for k in missing:
        if how == "absent":
            del secret[k]
        else:
            secret[k] = {"none": None, "empty": "", "blank": "  \t"}[how]
    with pytest.raises(ps.MissingSecretKeysError) as exc:
        ps.get_odoo_credentials(secret=secret)
    assert exc.value.missing == missing
    assert isinstance(exc.value, ps.SecretError)
    for k in missing:
        assert k in str(exc.value)
    _assert_no_secret_values(str(exc.value))


def test_no_secret_value_is_logged(caplog):
    with caplog.at_level(logging.DEBUG):
        ps.get_odoo_credentials(client=FakeClient(_string(FAKE)))
    _assert_no_secret_values(caplog.text)
