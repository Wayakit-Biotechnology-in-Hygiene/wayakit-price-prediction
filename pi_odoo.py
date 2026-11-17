"""Shared Odoo XML-RPC helpers for the Price Intelligence scripts.

The Odoo scripts (`ml_model/odoo_api_*.py`) used to authenticate at import time with their own
copy of the connection code. Now each script builds an `OdooSession` in `main()` with
`connect()` and passes it to functions that only call `session.execute(...)`, so tests can
build `OdooSession(models=<fake>, db=..., uid=..., api_token=...)` without network.

Nothing here reads secrets: `connect()` receives `pi_secrets.OdooCredentials`.
"""
from __future__ import annotations

import logging
import xmlrpc.client
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("wayakit_price_prediction")

# Socket timeout for every XML-RPC call. Without it a hung call (e.g. a create that never
# answers) blocks until `timeout 1h` kills the process. Creating a full batch takes well under it.
XMLRPC_TIMEOUT_S = 300


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float, **kwargs: Any):
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout  # read by http.client when it opens the socket
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float, **kwargs: Any):
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def server_proxy_with_timeout(url: str, timeout: float = XMLRPC_TIMEOUT_S) -> xmlrpc.client.ServerProxy:
    """`xmlrpc.client.ServerProxy` whose socket operations time out after `timeout` seconds."""
    transport_cls = _TimeoutSafeTransport if url.lower().startswith("https") else _TimeoutTransport
    return xmlrpc.client.ServerProxy(url, transport=transport_cls(timeout))


class OdooError(RuntimeError):
    """Odoo could not be used safely (authentication failed, ambiguous company, ...)."""


@dataclass(frozen=True)
class OdooSession:
    """An authenticated XML-RPC session. `models` is the `/xmlrpc/2/object` proxy (or a fake
    with the same `execute_kw` signature). The token is excluded from repr()."""

    models: Any
    db: str
    uid: int
    api_token: str = field(repr=False)

    def execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        """`execute_kw(db, uid, token, model, method, list(args), kwargs)`."""
        return self.models.execute_kw(self.db, self.uid, self.api_token, model, method,
                                      list(args), kwargs)


def connect(credentials: Any,
            server_proxy: Callable[[str], Any] = server_proxy_with_timeout) -> OdooSession:
    """Authenticates against Odoo and returns an `OdooSession`.

    `credentials` has `url`, `db`, `username` and `api_token` (`pi_secrets.OdooCredentials`).
    Raises `OdooError` if Odoo returns no uid (wrong user or token): before F2 the scripts
    carried on with `uid=False` and failed later with a confusing error.
    """
    common = server_proxy(f"{credentials.url}/xmlrpc/2/common")
    uid = common.authenticate(credentials.db, credentials.username, credentials.api_token, {})
    if not uid:
        raise OdooError(f"Odoo authentication failed for database '{credentials.db}' (no uid).")
    models = server_proxy(f"{credentials.url}/xmlrpc/2/object")
    logger.info("Odoo authentication successful (uid %s).", uid)
    return OdooSession(models=models, db=credentials.db, uid=uid,
                       api_token=credentials.api_token)


def get_single_company(session: OdooSession) -> tuple[int, str]:
    """Returns `(id, name)` of the only company visible to the Odoo user.

    The PI Odoo database is KSA only (Mafer, 2026-11-17), but the code does not filter by
    `company_id`. Before any write we check that the user sees exactly one company; if it sees
    more than one, a company filter is needed and we refuse to guess.
    """
    companies = session.execute("res.company", "search_read", [], fields=["name"])
    if len(companies) != 1:
        names = sorted(str(c.get("name")) for c in companies)
        raise OdooError(
            f"Expected exactly 1 company visible to the Odoo user, found {len(companies)} "
            f"({', '.join(names) or 'none'}). A company filter is needed before writing; not guessing."
        )
    return companies[0]["id"], companies[0]["name"]
