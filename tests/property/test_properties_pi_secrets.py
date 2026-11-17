"""Property tests of `pi_secrets.py` (pure parts: response parsing and required keys).

- Round trip: any JSON object stored as `SecretString`, raw `SecretBinary` bytes or base64
  `SecretBinary` comes back equal from `parse_secret_response`.
- Equivalence with an obvious reference: `require_keys` reports as missing exactly the keys that
  are absent, None or blank strings, in the order asked, and otherwise returns exactly them.
- Invariant: no secret value ever appears in the error message.
"""
from __future__ import annotations

import base64
import json

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import pi_secrets as ps

pytestmark = pytest.mark.property

json_scalars = st.none() | st.booleans() | st.integers() | st.text(max_size=30) | st.floats(
    allow_nan=False, allow_infinity=False)
json_values = st.recursive(json_scalars, lambda inner: st.lists(inner, max_size=4)
                           | st.dictionaries(st.text(max_size=10), inner, max_size=4), max_leaves=12)
secrets = st.dictionaries(st.text(max_size=20), json_values, max_size=8)

# Values that are clearly secrets: a marker no key name or fixed text can contain.
secret_value = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=6, max_size=20).map(
    lambda s: "FAKE~" + s)
blank = st.sampled_from([None, "", " ", "\t", "  \n "])
key_value = st.one_of(st.just("absent"), blank, secret_value)


@given(secrets)
@example({})
@example({"ODOO_API_TOKEN": "FAKE~x", "nested": {"a": [1, None, "é"]}})
def test_round_trip_through_every_encoding(d):
    raw = json.dumps(d).encode("utf-8")
    for response in ({"SecretString": json.dumps(d)},
                     {"SecretBinary": raw},
                     {"SecretBinary": base64.b64encode(raw)},
                     {"SecretBinary": base64.b64encode(raw).decode("ascii")}):
        assert ps.parse_secret_response(response) == d


@given(st.fixed_dictionaries({k: key_value for k in ps.ODOO_KEYS}),
       st.permutations(list(ps.ODOO_KEYS)))
def test_require_keys_matches_the_reference_and_never_leaks_values(spec, order):
    secret = {k: v for k, v in spec.items() if v != "absent"}
    expected_missing = [k for k in order
                        if secret.get(k) is None or (isinstance(secret[k], str) and not secret[k].strip())]
    if expected_missing:
        with pytest.raises(ps.MissingSecretKeysError) as exc:
            ps.require_keys(secret, order)
        assert list(exc.value.missing) == expected_missing
        message = str(exc.value)
        assert "FAKE~" not in message, message
    else:
        assert ps.require_keys(secret, order) == {k: secret[k] for k in order}
