"""Single place where Price Intelligence reads its secrets.

Everything PI needs lives in the AWS Secrets Manager secret `prod/wayakit-app` (region
`eu-north-1`). Before F2 there were six copies of `get_secret()`; this module replaces them.
Do not add another one: import `get_secret()` or `get_odoo_credentials()` from here.

Rules:
- Importing this module does not touch AWS (boto3 is imported lazily, only when no client is
  injected), so tests and `--dry-run` paths can import it freely.
- No secret value is ever logged or put in an exception message: errors name the secret and
  the missing keys, never their values. `OdooCredentials` hides the token from `repr()`.
- Tests inject a fake client (`client=`) with a `get_secret_value(SecretId=...)` method.

Usage:
    from pi_secrets import get_odoo_credentials
    creds = get_odoo_credentials()          # reads prod/wayakit-app once
    creds.url, creds.db, creds.username, creds.api_token
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

SECRET_NAME = "prod/wayakit-app"
REGION_NAME = "eu-north-1"
ODOO_KEYS = ("ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "ODOO_API_TOKEN")

# Same logger name as log_config, without importing it: importing log_config creates the logs/
# directory and file handlers, which a library module must not do as a side effect.
logger = logging.getLogger("wayakit_price_prediction")


class SecretError(RuntimeError):
    """The secret could not be read or parsed. The message never contains secret values."""


class MissingSecretKeysError(SecretError):
    """The secret was read but some required keys are missing or empty."""

    def __init__(self, secret_name: str, missing: Iterable[str]):
        self.secret_name = secret_name
        self.missing = tuple(missing)
        super().__init__(
            f"Secret '{secret_name}' is missing required keys (or they are empty): "
            f"{', '.join(self.missing)}"
        )


@dataclass(frozen=True)
class OdooCredentials:
    """Odoo XML-RPC credentials. The token is excluded from repr() so it never reaches a log."""

    url: str
    db: str
    username: str
    api_token: str = field(repr=False)


def parse_secret_response(response: Mapping[str, Any], secret_name: str = SECRET_NAME) -> dict:
    """Turns a `get_secret_value` response into a dict. Pure: no I/O.

    Accepts `SecretString` (JSON text) or `SecretBinary` (base64 of JSON, or raw bytes as
    boto3 returns them). Raises `SecretError` if there is neither, or if the content is not a
    JSON object. The error never includes the content.
    """
    if response.get("SecretString") is not None:
        raw = response["SecretString"]
    elif response.get("SecretBinary") is not None:
        blob = response["SecretBinary"]
        if isinstance(blob, str):
            blob = blob.encode("ascii", errors="strict")
        try:
            raw = base64.b64decode(blob, validate=True)
        except (binascii.Error, ValueError):
            # boto3 already returns the decoded bytes; only legacy callers pass base64.
            raw = blob
    else:
        raise SecretError(f"Secret '{secret_name}' has neither SecretString nor SecretBinary.")

    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        # `from None`: the JSONDecodeError carries the raw document in its attributes.
        raise SecretError(f"Secret '{secret_name}' is not valid JSON.") from None
    if not isinstance(data, dict):
        raise SecretError(f"Secret '{secret_name}' is not a JSON object.")
    return data


def require_keys(secret: Mapping[str, Any], keys: Iterable[str],
                 secret_name: str = SECRET_NAME) -> dict:
    """Returns `{key: secret[key]}` for `keys`. Pure: no I/O.

    Raises `MissingSecretKeysError` (listing key names only, in the order given) if any key is
    absent, None or an empty/blank string.
    """
    keys = tuple(keys)
    missing = [k for k in keys
               if secret.get(k) is None or (isinstance(secret.get(k), str) and not secret[k].strip())]
    if missing:
        raise MissingSecretKeysError(secret_name, missing)
    return {k: secret[k] for k in keys}


def get_secret(secret_name: str = SECRET_NAME, region_name: str = REGION_NAME,
               client: Any = None) -> dict:
    """Reads `secret_name` from Secrets Manager and returns it as a dict.

    `client` is any object with `get_secret_value(SecretId=...)`; when None, a boto3
    `secretsmanager` client is created for `region_name` (credentials from the environment or
    the instance role). AWS errors are re-raised as `SecretError` with the error code only.
    """
    if client is None:
        import boto3  # lazy: importing this module must not require AWS
        client = boto3.session.Session().client(service_name="secretsmanager",
                                                region_name=region_name)
    try:
        response = client.get_secret_value(SecretId=secret_name)
    except Exception as e:
        resp = getattr(e, "response", None)
        code = resp.get("Error", {}).get("Code") if isinstance(resp, dict) else None
        logger.error("Could not read secret '%s' (%s).", secret_name, code or type(e).__name__)
        raise SecretError(
            f"Could not read secret '{secret_name}': {code or type(e).__name__}"
        ) from e
    return parse_secret_response(response, secret_name)


def get_odoo_credentials(secret: Mapping[str, Any] | None = None, client: Any = None,
                         secret_name: str = SECRET_NAME,
                         region_name: str = REGION_NAME) -> OdooCredentials:
    """Returns the four Odoo keys as `OdooCredentials`.

    Pass `secret` (an already-read dict) to avoid another Secrets Manager call, or `client` to
    inject a fake. Raises `MissingSecretKeysError` if any of `ODOO_KEYS` is missing or empty.
    A trailing '/' is stripped from the URL so `f"{url}/xmlrpc/2/object"` never has '//'.
    """
    if secret is None:
        secret = get_secret(secret_name, region_name, client=client)
    values = require_keys(secret, ODOO_KEYS, secret_name)
    logger.info("Odoo credentials loaded from '%s'.", secret_name)
    return OdooCredentials(
        url=str(values["ODOO_URL"]).rstrip("/"),
        db=str(values["ODOO_DB"]),
        username=str(values["ODOO_USERNAME"]),
        api_token=str(values["ODOO_API_TOKEN"]),
    )
