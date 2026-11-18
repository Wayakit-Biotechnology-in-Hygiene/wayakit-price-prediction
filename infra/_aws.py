"""Shared AWS helpers for the `infra/` scripts.

What it provides:
- `get_session(session=None)`: the injected session (tests) or a boto3 session on the configured
  profile, always in `eu-north-1`. `client(session, service)` pins the region.
- `account_id(session)`: the account ID from STS (never stored in the repo).
- Tags: `project_tags`, `to_tag_list`, `tags_to_dict`, `tags_to_add`, `merge_tags`. AWS uses
  two shapes (`Key`/`Value` almost everywhere; `key`/`value` in Bedrock), so every helper
  accepts both and `lower=True` produces the Bedrock shape.
- Policies: `policy_json`, `policies_equal` (order-insensitive comparison for idempotent updates
  and check_pi).
- Output: `report` (dry-run aware printer), `json_diff`, `redact_account`, `mask_email`.
- Errors: `error_code(exc)`.
- CLI: `base_parser(description)` with `--dry-run`.

Importing this module does not call AWS; boto3 is imported only when no session is injected.
"""
import argparse
import difflib
import json
import re

from infra import config


# --- Session and account -----------------------------------------------------------------------
def get_session(session=None):
    """Return `session` if given (tests inject a fake), else a boto3 session on `config.profile()`
    in `config.REGION`. Refuses the profile `default`."""
    if session is not None:
        return session
    name = config.profile()
    if name == "default":
        raise SystemExit("Refusing the AWS profile 'default': use 'wayakit-vdc' (CLAUDE.md).")
    import boto3

    return boto3.Session(profile_name=name, region_name=config.REGION)


def client(session, service):
    """`session.client(service)` pinned to `config.REGION`, whatever the session's default."""
    return session.client(service, region_name=config.REGION)


def account_id(session):
    """Account ID of the caller, from STS. Validated as 12 digits so a malformed answer never
    ends up inside an ARN or a bucket name."""
    account = client(session, "sts").get_caller_identity()["Account"]
    if not re.fullmatch(r"\d{12}", str(account)):
        raise SystemExit("STS returned an unexpected account ID format.")
    return account


# --- Tags --------------------------------------------------------------------------------------
def tags_to_dict(tags):
    """`[{"Key": k, "Value": v}]` or `[{"key": k, "value": v}]` (or None) -> `{k: v}`."""
    out = {}
    for tag in tags or []:
        key = tag["Key"] if "Key" in tag else tag["key"]
        out[key] = tag["Value"] if "Value" in tag else tag.get("value", "")
    return out


def to_tag_list(tags, *, lower=False):
    """`{k: v}` -> AWS tag list, sorted by key. `lower=True` gives Bedrock's `key`/`value`."""
    k, v = ("key", "value") if lower else ("Key", "Value")
    return [{k: key, v: tags[key]} for key in sorted(tags)]


def project_tags(extra=None, *, lower=False):
    """Tag list with `project=price-intelligence` plus `extra` (a dict, e.g. {"Name": ...}).
    `extra` cannot change the project tag."""
    extra = dict(extra or {})
    if extra.get(config.PROJECT_TAG_KEY, config.PROJECT_TAG_VALUE) != config.PROJECT_TAG_VALUE:
        raise ValueError(f"The '{config.PROJECT_TAG_KEY}' tag is fixed to '{config.PROJECT_TAG_VALUE}'.")
    extra[config.PROJECT_TAG_KEY] = config.PROJECT_TAG_VALUE
    return to_tag_list(extra, lower=lower)


def _desired(desired):
    return tags_to_dict(project_tags()) if desired is None else (
        dict(desired) if isinstance(desired, dict) else tags_to_dict(desired))


def tags_to_add(current, desired=None):
    """Tags (as a dict) that are missing or have another value in `current`. `desired` is a dict
    or tag list; default: the project tag. Empty dict = nothing to do (skip the tag call). For
    additive APIs (`tag_role`, `create_tags`, `tag_resource`)."""
    have = tags_to_dict(current)
    return {k: v for k, v in _desired(desired).items() if have.get(k) != v}


def merge_tags(current, desired=None, *, lower=False):
    """Full tag list for replace-style APIs (`put_bucket_tagging` replaces the whole set): the
    current tags kept, `desired` added or overwritten. Default `desired`: the project tag."""
    merged = tags_to_dict(current)
    merged.update(_desired(desired))
    return to_tag_list(merged, lower=lower)


# --- Policies ----------------------------------------------------------------------------------
# Keys whose value IAM accepts either as one item or as a list, with no meaning in the order.
_LIST_KEYS = {"Statement", "Action", "NotAction", "Resource", "NotResource",
              "Service", "AWS", "Federated"}


def _canonical(node, key=None):
    """Normalize a policy so equivalent documents compare equal: one item == one-element list,
    and the order of statements, actions, resources and condition values is ignored."""
    # Condition values are keyed by the condition key ("aws:SourceAccount",
    # "token.actions.githubusercontent.com:sub"), which always contains ":"; other keys never do.
    if key is not None and (key in _LIST_KEYS or ":" in key) and not isinstance(node, list):
        node = [node]
    if isinstance(node, dict):
        return {k: _canonical(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return sorted((_canonical(v) for v in node), key=lambda x: json.dumps(x, sort_keys=True))
    return node


def policy_json(doc):
    """Stable JSON text of a policy (sorted keys, compact): what scripts send to AWS."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def policies_equal(a, b):
    """True if two policy documents (dicts or JSON strings) mean the same, ignoring key order,
    statement order, list order and string-vs-one-element-list."""
    a = json.loads(a) if isinstance(a, str) else a
    b = json.loads(b) if isinstance(b, str) else b
    return _canonical(a) == _canonical(b)


# --- Output ------------------------------------------------------------------------------------
def redact_account(text, account):
    """Replace the account ID with `<account>` (output may be pasted into chat or docs)."""
    return str(text).replace(account, "<account>") if account else str(text)


def mask_email(address):
    """`someone@example.com` -> `s***@example.com`. The full address is never printed."""
    local, sep, domain = str(address).partition("@")
    if not sep:
        return "***"
    return f"{local[:1]}***@{domain}"


def report(message, *, dry_run=False, account=None):
    """Print one action line. In dry-run mode it is prefixed with `[dry-run] would`; the account
    ID is always redacted. Returns the printed line (for tests)."""
    line = f"[dry-run] would {message}" if dry_run else str(message)
    line = redact_account(line, account)
    print(line)
    return line


def json_diff(current, desired, label="config"):
    """Unified diff of two JSON-serializable values (empty string if equal). For dry-run output."""
    def lines(value):
        return json.dumps(value, indent=2, sort_keys=True, default=str).splitlines()

    return "\n".join(difflib.unified_diff(lines(current), lines(desired),
                                          f"{label} (AWS)", f"{label} (desired)", lineterm=""))


# --- Errors and CLI ----------------------------------------------------------------------------
def error_code(exc):
    """AWS error code of a botocore `ClientError` (e.g. 'NoSuchEntity'), else None."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


def base_parser(description):
    """ArgumentParser with the `--dry-run` flag every script has (only read calls, prints the plan)."""
    parser = argparse.ArgumentParser(description=description,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would change; only read calls are made")
    return parser
