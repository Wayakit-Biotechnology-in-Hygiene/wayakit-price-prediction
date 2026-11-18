"""Creates or updates the Price Intelligence S3 bucket `price-intelligence-<account>`. Safe to run
more than once: it reads every setting first and only writes the ones that differ.

What it leaves configured (eu-north-1):
- Object Ownership `BucketOwnerEnforced` (ACLs disabled).
- Block Public Access: all four options on.
- Default encryption SSE-S3 (AES256) with `BucketKeyEnabled` (S3 Bucket Keys only act on SSE-KMS,
  so for SSE-S3 the flag is inert; it is set so a later switch to SSE-KMS keeps the low-cost
  path), and uploads with customer-provided keys (SSE-C) blocked.
- Versioning enabled.
- Bucket policy (replaced as a whole): deny every request without TLS
  (`aws:SecureTransport = false`). Nothing else: access is granted by the IAM roles.
- Lifecycle (replaced as a whole):
  - noncurrent versions expire 30 days after they stop being current (whole bucket);
  - objects under `logs/` expire after 180 days;
  - delete markers with no versions left are removed, incomplete multipart uploads are
    aborted after 7 days (whole bucket);
  - `releases/` has no expiration rule: the F4 deploy workflow prunes it to the last 5.
- Tag `project=price-intelligence` (other tags already on the bucket are kept).

Layout (prefixes are created by whoever writes the first object; this script creates no
placeholder objects): `data/`, `models/`, `state/`, `logs/`, `releases/` (`releases/current` is
the pointer to the active release, F4).

Every call except `create_bucket` passes `ExpectedBucketOwner`, so the script never configures
a bucket that belongs to another account. If the bucket exists in another region or another
account, it stops with exit 1.

Cost: S3 Standard storage and requests only (SSE-S3, versioning, Block Public Access and the
lifecycle rules have no fee of their own). Old versions are billed as storage until they expire
(30 days). For the expected volume (CSVs, models, logs and release tarballs: megabytes to a few
gigabytes) this is cents per month.

Usage (from the repo root, profile `wayakit-vdc` or `AWS_PROFILE`):
    python -m infra.bucket --dry-run   # only read calls; prints what it would change
    python -m infra.bucket             # creates or updates the bucket
"""
import json
import sys

from infra import _aws, config

OBJECT_OWNERSHIP = "BucketOwnerEnforced"
PUBLIC_ACCESS_BLOCK = {
    "BlockPublicAcls": True,
    "IgnorePublicAcls": True,
    "BlockPublicPolicy": True,
    "RestrictPublicBuckets": True,
}
SSE_ALGORITHM = "AES256"
BLOCKED_ENCRYPTION_TYPES = ["SSE-C"]
VERSIONING_STATUS = "Enabled"

# Order in which settings are read, compared and written. Ownership comes first (it decides
# whether ACLs count); tags last.
SETTINGS = ("ownership", "public_access_block", "encryption", "versioning", "policy",
            "lifecycle", "tags")

# Error codes AWS uses for "this setting was never configured" (the get_* call fails instead of
# returning an empty value).
_NOT_CONFIGURED = {
    "ownership": "OwnershipControlsNotFoundError",
    "public_access_block": "NoSuchPublicAccessBlockConfiguration",
    "encryption": "ServerSideEncryptionConfigurationNotFoundError",
    "policy": "NoSuchBucketPolicy",
    "lifecycle": "NoSuchLifecycleConfiguration",
    "tags": "NoSuchTagSet",
}


# --- Desired state (pure) ----------------------------------------------------------------------
def encryption_rules():
    return [{
        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": SSE_ALGORITHM},
        "BucketKeyEnabled": True,
        "BlockedEncryptionTypes": {"EncryptionType": list(BLOCKED_ENCRYPTION_TYPES)},
    }]


def bucket_policy(account):
    """Full bucket policy: TLS only. `Principal: *` and `s3:*` are required here because it is
    a Deny that must cover every caller and every action."""
    bucket = config.bucket_arn(account)
    return {
        "Version": config.POLICY_VERSION,
        "Statement": [{
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [bucket, f"{bucket}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        }],
    }


def lifecycle_rules():
    """Full lifecycle configuration. `releases/` deliberately has no rule (F4 prunes it)."""
    return [
        {
            "ID": "noncurrent-versions-expire",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "NoncurrentVersionExpiration": {"NoncurrentDays": config.NONCURRENT_EXPIRATION_DAYS},
        },
        {
            # S3 does not allow ExpiredObjectDeleteMarker and Days in the same rule, so this
            # cleanup is its own bucket-wide rule. It only removes markers with nothing behind.
            "ID": "cleanup-markers-and-uploads",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "Expiration": {"ExpiredObjectDeleteMarker": True},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": config.ABORT_MULTIPART_DAYS},
        },
        {
            # With versioning on, this puts a delete marker at 180 days; the data itself goes
            # 30 days later through the noncurrent-versions rule.
            "ID": "logs-expire",
            "Status": "Enabled",
            "Filter": {"Prefix": config.LOGS_PREFIX},
            "Expiration": {"Days": config.LOGS_EXPIRATION_DAYS},
        },
    ]


def desired_state(account):
    """Every managed setting, in the shape `read_state` returns. `tags` is the full tag list
    for a bucket that has no other tags (see `diff_settings` for how foreign tags are kept)."""
    return {
        "ownership": OBJECT_OWNERSHIP,
        "public_access_block": dict(PUBLIC_ACCESS_BLOCK),
        "encryption": encryption_rules(),
        "versioning": VERSIONING_STATUS,
        "policy": bucket_policy(account),
        "lifecycle": lifecycle_rules(),
        "tags": _aws.project_tags(),
    }


# --- Comparison (pure) -------------------------------------------------------------------------
def normalize_encryption(rules):
    """Encryption rules in a comparable shape: AWS omits `BucketKeyEnabled` when false and
    `BlockedEncryptionTypes` when nothing is blocked. Idempotent: the normalized
    `BlockedEncryptionTypes` is a plain list, and a list is accepted as input too."""
    out = []
    for rule in rules or []:
        default = dict(rule.get("ApplyServerSideEncryptionByDefault") or {})
        blocked = rule.get("BlockedEncryptionTypes") or []
        if isinstance(blocked, dict):  # AWS shape; the normalized shape is already a list
            blocked = blocked.get("EncryptionType") or []
        out.append({
            "ApplyServerSideEncryptionByDefault": default,
            "BucketKeyEnabled": bool(rule.get("BucketKeyEnabled", False)),
            "BlockedEncryptionTypes": sorted(t for t in blocked if t != "NONE"),
        })
    return sorted(out, key=lambda r: json.dumps(r, sort_keys=True))


def normalize_lifecycle(rules):
    """Lifecycle rules sorted by ID, with the legacy top-level `Prefix` and an empty `Filter`
    both read as `Filter: {Prefix: ""}` (AWS may answer either way for a bucket-wide rule)."""
    out = []
    for rule in rules or []:
        rule = {k: v for k, v in rule.items() if v is not None}
        legacy_prefix = rule.pop("Prefix", None)
        filt = rule.get("Filter")
        if not filt:
            rule["Filter"] = {"Prefix": legacy_prefix or ""}
        out.append(rule)
    return sorted(out, key=lambda r: r.get("ID", ""))


def setting_matches(setting, current, desired):
    """True if the value read from AWS (`current`, None when not configured) already meets
    `desired` for that setting."""
    if setting == "policy":
        return current is not None and _aws.policies_equal(current, desired)
    if setting == "encryption":
        return normalize_encryption(current) == normalize_encryption(desired)
    if setting == "lifecycle":
        return normalize_lifecycle(current) == normalize_lifecycle(desired)
    if setting == "tags":
        return not _aws.tags_to_add(current or [], desired)
    return current == desired


def diff_settings(current, desired):
    """Settings (in `SETTINGS` order) whose current value differs from the desired one.
    `current` may lack keys (= not configured)."""
    return [s for s in SETTINGS if not setting_matches(s, current.get(s), desired[s])]


def write_value(setting, current, desired):
    """Value to send for `setting`: the desired one, except tags, where the bucket's other tags
    are kept (`put_bucket_tagging` replaces the whole set)."""
    if setting == "tags":
        return _aws.merge_tags(current or [], desired)
    return desired


# --- AWS (reads) -------------------------------------------------------------------------------
def bucket_exists(s3, name, account):
    """True if the bucket exists in this account and region, False if it does not exist.
    Stops (exit 1) if it belongs to another account or lives in another region."""
    try:
        resp = s3.head_bucket(Bucket=name, ExpectedBucketOwner=account)
    except Exception as exc:  # botocore ClientError; botocore is not imported at module level
        code = _aws.error_code(exc)
        if code in ("404", "NoSuchBucket", "NotFound"):
            return False
        if code in ("403", "Forbidden", "AccessDenied"):
            raise SystemExit("The bucket name exists but is not accessible with this account "
                             "(owned by another account, or missing permissions).")
        raise
    region = resp.get("BucketRegion")
    if region and region != config.REGION:
        raise SystemExit(f"The bucket exists in {region}, not in {config.REGION}.")
    return True


def _get(s3, setting, name, account):
    kw = {"Bucket": name, "ExpectedBucketOwner": account}
    try:
        if setting == "ownership":
            rules = s3.get_bucket_ownership_controls(**kw)["OwnershipControls"]["Rules"]
            return rules[0]["ObjectOwnership"] if rules else None
        if setting == "public_access_block":
            return s3.get_public_access_block(**kw)["PublicAccessBlockConfiguration"]
        if setting == "encryption":
            return s3.get_bucket_encryption(**kw)["ServerSideEncryptionConfiguration"]["Rules"]
        if setting == "versioning":
            return s3.get_bucket_versioning(**kw).get("Status")
        if setting == "policy":
            return json.loads(s3.get_bucket_policy(**kw)["Policy"])
        if setting == "lifecycle":
            return s3.get_bucket_lifecycle_configuration(**kw)["Rules"]
        if setting == "tags":
            return s3.get_bucket_tagging(**kw)["TagSet"]
    except Exception as exc:
        # Versioning has no "not configured" code, and a non-AWS error (network) has no code
        # either: both being None must not turn an error into "not configured".
        code = _NOT_CONFIGURED.get(setting)
        if code is not None and _aws.error_code(exc) == code:
            return None
        raise
    raise ValueError(f"Unknown setting: {setting}")


def read_state(s3, name, account):
    """Current value of every setting in `SETTINGS` (None = not configured). Read calls only."""
    return {setting: _get(s3, setting, name, account) for setting in SETTINGS}


# --- AWS (writes) ------------------------------------------------------------------------------
def _put(s3, setting, value, name, account):
    kw = {"Bucket": name, "ExpectedBucketOwner": account}
    if setting == "ownership":
        s3.put_bucket_ownership_controls(**kw, OwnershipControls={"Rules": [{"ObjectOwnership": value}]})
    elif setting == "public_access_block":
        s3.put_public_access_block(**kw, PublicAccessBlockConfiguration=value)
    elif setting == "encryption":
        s3.put_bucket_encryption(**kw, ServerSideEncryptionConfiguration={"Rules": value})
    elif setting == "versioning":
        s3.put_bucket_versioning(**kw, VersioningConfiguration={"Status": value})
    elif setting == "policy":
        s3.put_bucket_policy(**kw, Policy=_aws.policy_json(value))
    elif setting == "lifecycle":
        s3.put_bucket_lifecycle_configuration(**kw, LifecycleConfiguration={"Rules": value})
    elif setting == "tags":
        s3.put_bucket_tagging(**kw, Tagging={"TagSet": value})
    else:
        raise ValueError(f"Unknown setting: {setting}")


def create_bucket(s3, name, account):
    # Ownership is set at creation, so ACLs are never enabled, not even for a moment.
    s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": config.REGION},
                     ObjectOwnership=OBJECT_OWNERSHIP)
    s3.get_waiter("bucket_exists").wait(Bucket=name, ExpectedBucketOwner=account)


def ensure_bucket(s3, account, *, dry_run=False):
    """Bring the bucket to `desired_state(account)`. Returns the settings it changed (or would
    change in dry-run), with "create" first when the bucket is missing. In dry-run it makes read
    calls only (`head_bucket` and, if the bucket exists, the `get_*` calls)."""
    name = config.bucket_name(account)
    desired = desired_state(account)
    exists = bucket_exists(s3, name, account)
    _aws.report(f"Bucket {name} ({config.REGION}): {'exists' if exists else 'does not exist'}.",
                account=account)

    if exists:
        current = read_state(s3, name, account)
    else:
        # A new bucket is created with BucketOwnerEnforced; every other setting is written.
        current = {"ownership": OBJECT_OWNERSHIP}
    changes = diff_settings(current, desired)

    for setting in changes:
        if setting in ("policy", "lifecycle", "encryption", "public_access_block"):
            diff = _aws.json_diff(current.get(setting), desired[setting], label=setting)
            if diff:
                print(_aws.redact_account(diff, account))

    actions = ([] if exists else ["create"]) + changes
    if not exists:
        _aws.report(f"create bucket {name} in {config.REGION} (BucketOwnerEnforced)",
                    dry_run=dry_run, account=account)
        if not dry_run:
            create_bucket(s3, name, account)
    for setting in changes:
        _aws.report(f"set {setting}", dry_run=dry_run, account=account)
        if not dry_run:
            _put(s3, setting, write_value(setting, current.get(setting), desired[setting]),
                 name, account)

    if not actions:
        _aws.report("No changes: the bucket already matches the configuration.", account=account)
    elif dry_run:
        _aws.report("Dry run: nothing was changed.", account=account)
    else:
        _aws.report(f"Applied: {', '.join(actions)}.", account=account)
    return actions


def main(argv=None, session=None):
    args = _aws.base_parser(__doc__).parse_args(argv)
    sess = _aws.get_session(session)
    account = _aws.account_id(sess)
    s3 = _aws.client(sess, "s3")
    try:
        ensure_bucket(s3, account, dry_run=args.dry_run)
    except Exception as exc:
        code = _aws.error_code(exc)
        if code is None:
            raise
        # Only the operation and the error code: AWS messages can carry ARNs with the account.
        operation = getattr(exc, "operation_name", "AWS call")
        print(f"AWS error in {operation}: {code}", file=sys.stderr)
        raise SystemExit(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
