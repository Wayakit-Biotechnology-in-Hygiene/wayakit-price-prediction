"""Unit tests of `infra/bucket.py` (the Price Intelligence S3 bucket).

No test reaches AWS. Two kinds of double:
- `botocore.stub.Stubber` on real clients built with fake credentials in eu-north-1: checks the
  exact operations, their order and their parameters, and validates every request and response
  against the S3 service model (an invalid shape fails here, not in the account).
- `FakeS3`, a small stateful S3 that records every call: used for invariants that need several
  runs over the same bucket (a second apply makes no writes, dry-run never writes, tags not owned
  by PI survive).

The fake account is `000000000000`.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

from infra import _aws, bucket, config
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.unit

ACCOUNT = "000000000000"
NAME = config.bucket_name(ACCOUNT)
KW = {"Bucket": NAME, "ExpectedBucketOwner": ACCOUNT}
FOREIGN_TAG = {"Key": "owner", "Value": "someone-else"}

GET_OPS = {
    "ownership": "get_bucket_ownership_controls",
    "public_access_block": "get_public_access_block",
    "encryption": "get_bucket_encryption",
    "versioning": "get_bucket_versioning",
    "policy": "get_bucket_policy",
    "lifecycle": "get_bucket_lifecycle_configuration",
    "tags": "get_bucket_tagging",
}
PUT_OPS = {
    "ownership": "put_bucket_ownership_controls",
    "public_access_block": "put_public_access_block",
    "encryption": "put_bucket_encryption",
    "versioning": "put_bucket_versioning",
    "policy": "put_bucket_policy",
    "lifecycle": "put_bucket_lifecycle_configuration",
    "tags": "put_bucket_tagging",
}
READ_ONLY_OPS = {"head_bucket", *GET_OPS.values()}


def desired():
    return bucket.desired_state(ACCOUNT)


# --- Stubber helpers ---------------------------------------------------------------------------
def _fake_boto_session():
    return boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                         region_name="us-east-1")


class FakeSession:
    def __init__(self, clients):
        self.clients = clients

    def client(self, service, **kwargs):
        assert kwargs.get("region_name") == config.REGION
        return self.clients[service]


def stubbed():
    """(session, sts stubber, s3 client, s3 stubber), both stubbers active; STS answers once."""
    boto = _fake_boto_session()
    sts = boto.client("sts", region_name=config.REGION)
    s3 = boto.client("s3", region_name=config.REGION)
    sts_stub, s3_stub = Stubber(sts), Stubber(s3)
    sts_stub.add_response("get_caller_identity",
                          {"Account": ACCOUNT, "Arn": "arn:aws:iam::000000000000:user/x",
                           "UserId": "X"}, {})
    sts_stub.activate()
    s3_stub.activate()
    return FakeSession({"sts": sts, "s3": s3}), sts_stub, s3, s3_stub


def aws_response(setting, value):
    """What the S3 `get_*` call returns when `setting` holds `value` (AWS shapes)."""
    return {
        "ownership": lambda: {"OwnershipControls": {"Rules": [{"ObjectOwnership": value}]}},
        "public_access_block": lambda: {"PublicAccessBlockConfiguration": value},
        "encryption": lambda: {"ServerSideEncryptionConfiguration": {"Rules": value}},
        "versioning": lambda: {"Status": value} if value else {},
        "policy": lambda: {"Policy": json.dumps(value)},
        "lifecycle": lambda: {"Rules": value},
        "tags": lambda: {"TagSet": value},
    }[setting]()


def put_params(setting, value):
    """Exact parameters of the `put_*` call that writes `value` for `setting`."""
    return {**KW, **{
        "ownership": lambda: {"OwnershipControls": {"Rules": [{"ObjectOwnership": value}]}},
        "public_access_block": lambda: {"PublicAccessBlockConfiguration": value},
        "encryption": lambda: {"ServerSideEncryptionConfiguration": {"Rules": value}},
        "versioning": lambda: {"VersioningConfiguration": {"Status": value}},
        "policy": lambda: {"Policy": _aws.policy_json(value)},
        "lifecycle": lambda: {"LifecycleConfiguration": {"Rules": value}},
        "tags": lambda: {"Tagging": {"TagSet": value}},
    }[setting]()}


def stub_reads(stub, state):
    """head_bucket (exists, eu-north-1) + the 7 get calls answering `state` (None = not set)."""
    stub.add_response("head_bucket", {"BucketRegion": config.REGION}, KW)
    for setting in bucket.SETTINGS:
        value = state.get(setting)
        if value is None and setting != "versioning":
            stub.add_client_error(GET_OPS[setting], service_error_code=bucket._NOT_CONFIGURED[setting],
                                  http_status_code=404, expected_params=KW)
        else:
            stub.add_response(GET_OPS[setting], aws_response(setting, value), KW)


def stub_missing(stub):
    stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404,
                          expected_params=KW)


def aws_like_desired():
    """The desired state as AWS may answer it: rule and statement order changed, empty Filter
    for bucket-wide lifecycle rules, Resource reversed, one foreign tag."""
    state = copy.deepcopy(desired())
    state["lifecycle"] = [dict(r, Filter={}) if r["Filter"] == {"Prefix": ""} else r
                          for r in reversed(state["lifecycle"])]
    state["policy"]["Statement"][0]["Resource"].reverse()
    state["tags"] = [FOREIGN_TAG, config.PROJECT_TAG]
    return state


FRESH_BUCKET = {
    # What a bucket created by hand today looks like: S3 defaults (BPA on, SSE-S3 without
    # bucket key), nothing else, one tag that is not PI's.
    "ownership": "BucketOwnerEnforced",
    "public_access_block": dict(bucket.PUBLIC_ACCESS_BLOCK),
    "encryption": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                    "BucketKeyEnabled": False}],
    "versioning": None,
    "policy": None,
    "lifecycle": None,
    "tags": [FOREIGN_TAG],
}


# --- Stateful fake -----------------------------------------------------------------------------
def client_error(code, operation, status=404, message="fake message"):
    return ClientError({"Error": {"Code": code, "Message": message},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class _Waiter:
    def __init__(self, s3):
        self.s3 = s3

    def wait(self, **kwargs):
        self.s3.head_bucket(**kwargs)


class FakeS3:
    """Minimal stateful S3 for one bucket. Stores values in the shape the get calls return.
    Any operation it does not implement raises AttributeError (so a delete or ACL call fails)."""

    def __init__(self, state=None, *, exists=True, empty_filter=False):
        self.exists = exists
        self.state = {s: None for s in bucket.SETTINGS}
        self.state.update(copy.deepcopy(state or {}))
        self.empty_filter = empty_filter  # answer bucket-wide lifecycle rules with Filter {}
        self.calls = []

    def _record(self, op, kwargs):
        self.calls.append((op, kwargs))
        if op != "create_bucket":
            assert kwargs.get("ExpectedBucketOwner") == ACCOUNT, op

    def mutating_calls(self):
        return [op for op, _ in self.calls if op not in READ_ONLY_OPS]

    # reads
    def head_bucket(self, **kw):
        self._record("head_bucket", kw)
        if not self.exists:
            raise client_error("404", "HeadBucket")
        return {"BucketRegion": config.REGION}

    def get_waiter(self, name):
        assert name == "bucket_exists"
        return _Waiter(self)

    def _get(self, setting, kw):
        self._record(GET_OPS[setting], kw)
        assert self.exists
        value = copy.deepcopy(self.state[setting])
        if value is None and setting != "versioning":
            raise client_error(bucket._NOT_CONFIGURED[setting], GET_OPS[setting])
        if setting == "lifecycle" and self.empty_filter:
            value = [dict(r, Filter={}) if r.get("Filter") == {"Prefix": ""} else r for r in value]
        if setting == "encryption":
            # AWS omits BucketKeyEnabled when false.
            value = [{k: v for k, v in r.items() if not (k == "BucketKeyEnabled" and v is False)}
                     for r in value]
        return aws_response(setting, value)

    def get_bucket_ownership_controls(self, **kw):
        return self._get("ownership", kw)

    def get_public_access_block(self, **kw):
        return self._get("public_access_block", kw)

    def get_bucket_encryption(self, **kw):
        return self._get("encryption", kw)

    def get_bucket_versioning(self, **kw):
        return self._get("versioning", kw)

    def get_bucket_policy(self, **kw):
        return self._get("policy", kw)

    def get_bucket_lifecycle_configuration(self, **kw):
        return self._get("lifecycle", kw)

    def get_bucket_tagging(self, **kw):
        return self._get("tags", kw)

    # writes
    def create_bucket(self, **kw):
        self._record("create_bucket", kw)
        assert not self.exists
        self.exists = True
        self.state["ownership"] = kw["ObjectOwnership"]

    def _put(self, setting, value, kw):
        self._record(PUT_OPS[setting], kw)
        assert self.exists
        self.state[setting] = copy.deepcopy(value)

    def put_bucket_ownership_controls(self, **kw):
        self._put("ownership", kw["OwnershipControls"]["Rules"][0]["ObjectOwnership"], kw)

    def put_public_access_block(self, **kw):
        self._put("public_access_block", kw["PublicAccessBlockConfiguration"], kw)

    def put_bucket_encryption(self, **kw):
        self._put("encryption", kw["ServerSideEncryptionConfiguration"]["Rules"], kw)

    def put_bucket_versioning(self, **kw):
        self._put("versioning", kw["VersioningConfiguration"]["Status"], kw)

    def put_bucket_policy(self, **kw):
        self._put("policy", json.loads(kw["Policy"]), kw)

    def put_bucket_lifecycle_configuration(self, **kw):
        self._put("lifecycle", kw["LifecycleConfiguration"]["Rules"], kw)

    def put_bucket_tagging(self, **kw):
        self._put("tags", kw["Tagging"]["TagSet"], kw)


# --- Constants and pure builders ---------------------------------------------------------------
def test_constants():
    assert bucket.OBJECT_OWNERSHIP == "BucketOwnerEnforced"
    assert bucket.PUBLIC_ACCESS_BLOCK == {"BlockPublicAcls": True, "IgnorePublicAcls": True,
                                          "BlockPublicPolicy": True, "RestrictPublicBuckets": True}
    assert bucket.SSE_ALGORITHM == "AES256"
    assert bucket.VERSIONING_STATUS == "Enabled"
    assert bucket.SETTINGS == ("ownership", "public_access_block", "encryption", "versioning",
                               "policy", "lifecycle", "tags")
    assert set(bucket._NOT_CONFIGURED) == set(bucket.SETTINGS) - {"versioning"}


def test_encryption_rules():
    assert bucket.encryption_rules() == [{
        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
        "BucketKeyEnabled": True,
        "BlockedEncryptionTypes": {"EncryptionType": ["SSE-C"]},
    }]
    # A fresh list each time: a caller mutating it cannot change the constant.
    bucket.encryption_rules()[0]["BlockedEncryptionTypes"]["EncryptionType"].append("X")
    assert bucket.BLOCKED_ENCRYPTION_TYPES == ["SSE-C"]


def test_bucket_policy_is_tls_only_and_grants_nothing():
    policy = bucket.bucket_policy(ACCOUNT)
    arn = f"arn:aws:s3:::price-intelligence-{ACCOUNT}"
    assert policy == {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [arn, f"{arn}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        }],
    }
    # Least privilege: the bucket policy never allows anything (access comes from IAM roles).
    assert all(s["Effect"] == "Deny" for s in policy["Statement"])


def test_lifecycle_rules():
    rules = bucket.lifecycle_rules()
    assert [r["ID"] for r in rules] == ["noncurrent-versions-expire", "cleanup-markers-and-uploads",
                                        "logs-expire"]
    assert all(r["Status"] == "Enabled" for r in rules)
    by_id = {r["ID"]: r for r in rules}
    assert by_id["noncurrent-versions-expire"] == {
        "ID": "noncurrent-versions-expire", "Status": "Enabled", "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}
    assert by_id["cleanup-markers-and-uploads"] == {
        "ID": "cleanup-markers-and-uploads", "Status": "Enabled", "Filter": {"Prefix": ""},
        "Expiration": {"ExpiredObjectDeleteMarker": True},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}
    assert by_id["logs-expire"] == {
        "ID": "logs-expire", "Status": "Enabled", "Filter": {"Prefix": "logs/"},
        "Expiration": {"Days": 180}}


def test_lifecycle_never_expires_current_data_outside_logs():
    """Invariant 6: releases/ is kept (F4 prunes it); data/, models/, state/ never expire."""
    for rule in bucket.lifecycle_rules():
        prefix = rule["Filter"].get("Prefix", "")
        assert not prefix.startswith("releases"), rule
        assert "releases/" not in json.dumps(rule)
        if "Days" in rule.get("Expiration", {}):
            assert rule["ID"] == "logs-expire" and prefix == config.LOGS_PREFIX
        assert "Transitions" not in rule  # no storage-class moves (retrieval fees)


def test_desired_state():
    state = desired()
    assert tuple(state) == bucket.SETTINGS
    assert state["ownership"] == "BucketOwnerEnforced"
    assert state["versioning"] == "Enabled"
    assert state["public_access_block"] == bucket.PUBLIC_ACCESS_BLOCK
    assert state["public_access_block"] is not bucket.PUBLIC_ACCESS_BLOCK
    assert state["encryption"] == bucket.encryption_rules()
    assert state["policy"] == bucket.bucket_policy(ACCOUNT)
    assert state["lifecycle"] == bucket.lifecycle_rules()
    assert state["tags"] == [{"Key": "project", "Value": "price-intelligence"}]


# --- Comparison --------------------------------------------------------------------------------
def test_every_setting_matches_itself_and_no_diff():
    state = desired()
    for setting in bucket.SETTINGS:
        assert bucket.setting_matches(setting, state[setting], state[setting]), setting
    assert bucket.diff_settings(state, state) == []


def test_diff_of_an_empty_state_is_every_setting_in_order():
    assert bucket.diff_settings({}, desired()) == list(bucket.SETTINGS)


def test_aws_shapes_of_the_desired_state_cause_no_diff():
    """Invariants 7 and 8: statement/resource order, string vs one-element list, lifecycle
    order, empty Filter and foreign tags do not count as a difference."""
    state = aws_like_desired()
    state["policy"]["Statement"][0]["Action"] = ["s3:*"]
    state["policy"]["Statement"][0]["Principal"] = "*"
    assert bucket.diff_settings(state, desired()) == []


@pytest.mark.parametrize("variant", [
    {},                                  # empty Filter
    None,                                # no Filter at all
    "legacy",                            # top-level Prefix "" (legacy API)
])
def test_lifecycle_bucket_wide_rule_variants_match(variant):
    rules = copy.deepcopy(bucket.lifecycle_rules())
    for rule in rules:
        if rule["Filter"] == {"Prefix": ""}:
            if variant == "legacy":
                del rule["Filter"]
                rule["Prefix"] = ""
            elif variant is None:
                del rule["Filter"]
            else:
                rule["Filter"] = variant
    assert bucket.setting_matches("lifecycle", rules, bucket.lifecycle_rules())


def test_lifecycle_legacy_prefix_logs_matches():
    rules = copy.deepcopy(bucket.lifecycle_rules())
    logs = next(r for r in rules if r["ID"] == "logs-expire")
    del logs["Filter"]
    logs["Prefix"] = "logs/"
    assert bucket.setting_matches("lifecycle", rules, bucket.lifecycle_rules())


@pytest.mark.parametrize("change", [
    lambda rules: rules.pop(),                                                 # rule removed
    lambda rules: rules[0].update(Status="Disabled"),
    lambda rules: rules[0]["NoncurrentVersionExpiration"].update(NoncurrentDays=31),
    lambda rules: rules[2]["Expiration"].update(Days=1),
    lambda rules: rules[2].update(Filter={"Prefix": "data/"}),
    lambda rules: rules.append({"ID": "x", "Status": "Enabled", "Filter": {"Prefix": "releases/"},
                                "Expiration": {"Days": 1}}),
])
def test_lifecycle_real_changes_are_detected(change):
    rules = copy.deepcopy(bucket.lifecycle_rules())
    change(rules)
    assert not bucket.setting_matches("lifecycle", rules, bucket.lifecycle_rules())


def test_encryption_aws_omissions_match():
    """AWS omits BucketKeyEnabled when false and may send NONE in BlockedEncryptionTypes."""
    rule = {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
    assert bucket.normalize_encryption([rule]) == bucket.normalize_encryption(
        [dict(rule, BucketKeyEnabled=False, BlockedEncryptionTypes={"EncryptionType": ["NONE"]})])
    assert bucket.setting_matches("encryption", [{
        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}, "BucketKeyEnabled": True,
        "BlockedEncryptionTypes": {"EncryptionType": ["SSE-C"]}}], bucket.encryption_rules())


@pytest.mark.parametrize("current", [
    None,
    FRESH_BUCKET["encryption"],                                          # no bucket key, SSE-C open
    [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}, "BucketKeyEnabled": True}],
    [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}, "BucketKeyEnabled": True,
      "BlockedEncryptionTypes": {"EncryptionType": ["SSE-C"]}}],
])
def test_encryption_real_changes_are_detected(current):
    assert not bucket.setting_matches("encryption", current, bucket.encryption_rules())


@pytest.mark.parametrize("current", [
    None,
    {**bucket.bucket_policy(ACCOUNT), "Statement": [
        {**bucket.bucket_policy(ACCOUNT)["Statement"][0], "Effect": "Allow"}]},
    {**bucket.bucket_policy(ACCOUNT), "Statement": [
        {**bucket.bucket_policy(ACCOUNT)["Statement"][0],
         "Resource": f"arn:aws:s3:::price-intelligence-{ACCOUNT}"}]},
])
def test_policy_real_changes_are_detected(current):
    assert not bucket.setting_matches("policy", current, bucket.bucket_policy(ACCOUNT))


def test_tags_match_needs_only_the_project_tag():
    want = desired()["tags"]
    assert bucket.setting_matches("tags", [FOREIGN_TAG, config.PROJECT_TAG], want)
    assert not bucket.setting_matches("tags", None, want)
    assert not bucket.setting_matches("tags", [FOREIGN_TAG], want)
    assert not bucket.setting_matches("tags", [{"Key": "project", "Value": "other"}], want)


def test_simple_settings_compare_exactly():
    assert not bucket.setting_matches("versioning", "Suspended", "Enabled")
    assert not bucket.setting_matches("versioning", None, "Enabled")
    assert not bucket.setting_matches("ownership", "ObjectWriter", "BucketOwnerEnforced")
    assert not bucket.setting_matches("public_access_block",
                                      dict(bucket.PUBLIC_ACCESS_BLOCK, RestrictPublicBuckets=False),
                                      bucket.PUBLIC_ACCESS_BLOCK)


def test_write_value_keeps_foreign_tags():
    want = desired()
    assert bucket.write_value("tags", [FOREIGN_TAG], want["tags"]) == [FOREIGN_TAG, config.PROJECT_TAG]
    assert bucket.write_value("tags", None, want["tags"]) == [config.PROJECT_TAG]
    assert bucket.write_value("tags", [{"Key": "project", "Value": "old"}], want["tags"]) == [
        config.PROJECT_TAG]
    for setting in bucket.SETTINGS[:-1]:
        assert bucket.write_value(setting, "anything", want[setting]) == want[setting]


def test_normalize_encryption_idempotent_on_blocked_types():
    """Regression (fixed): the normalized BlockedEncryptionTypes is a list and must be accepted
    as input again (it used to raise AttributeError: 'list' object has no attribute 'get')."""
    once = bucket.normalize_encryption(bucket.encryption_rules())
    assert bucket.normalize_encryption(once) == once


# --- bucket_exists / read_state ----------------------------------------------------------------
def test_bucket_exists_true_false():
    s3 = _fake_boto_session().client("s3", region_name=config.REGION)
    with Stubber(s3) as stub:
        stub.add_response("head_bucket", {"BucketRegion": config.REGION}, KW)
        stub.add_response("head_bucket", {}, KW)  # no region header: accepted
        stub_missing(stub)
        stub.add_client_error("head_bucket", service_error_code="NoSuchBucket",
                              http_status_code=404, expected_params=KW)
        assert bucket.bucket_exists(s3, NAME, ACCOUNT) is True
        assert bucket.bucket_exists(s3, NAME, ACCOUNT) is True
        assert bucket.bucket_exists(s3, NAME, ACCOUNT) is False
        assert bucket.bucket_exists(s3, NAME, ACCOUNT) is False
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("code", ["403", "Forbidden", "AccessDenied"])
def test_bucket_of_another_account_stops(code):
    s3 = _fake_boto_session().client("s3", region_name=config.REGION)
    with Stubber(s3) as stub:
        stub.add_client_error("head_bucket", service_error_code=code, http_status_code=403,
                              expected_params=KW)
        with pytest.raises(SystemExit) as exc:
            bucket.bucket_exists(s3, NAME, ACCOUNT)
    assert exc.value.code and ACCOUNT not in str(exc.value.code)


def test_bucket_in_another_region_stops():
    s3 = _fake_boto_session().client("s3", region_name=config.REGION)
    with Stubber(s3) as stub:
        stub.add_response("head_bucket", {"BucketRegion": "us-east-1"}, KW)
        with pytest.raises(SystemExit) as exc:
            bucket.bucket_exists(s3, NAME, ACCOUNT)
    assert "us-east-1" in str(exc.value.code)


def test_bucket_exists_reraises_other_errors():
    s3 = _fake_boto_session().client("s3", region_name=config.REGION)
    with Stubber(s3) as stub:
        stub.add_client_error("head_bucket", service_error_code="InternalError",
                              http_status_code=500, expected_params=KW)
        with pytest.raises(ClientError):
            bucket.bucket_exists(s3, NAME, ACCOUNT)


def test_read_state_exact_calls_and_parsing():
    s3 = _fake_boto_session().client("s3", region_name=config.REGION)
    want = desired()
    with Stubber(s3) as stub:
        for setting in bucket.SETTINGS:
            stub.add_response(GET_OPS[setting], aws_response(setting, want[setting]), KW)
        assert bucket.read_state(s3, NAME, ACCOUNT) == want
        stub.assert_no_pending_responses()


def test_read_state_not_configured_is_none():
    s3 = _fake_boto_session().client("s3", region_name=config.REGION)
    with Stubber(s3) as stub:
        for setting in bucket.SETTINGS:
            if setting == "versioning":
                stub.add_response(GET_OPS[setting], {}, KW)
            else:
                stub.add_client_error(GET_OPS[setting],
                                      service_error_code=bucket._NOT_CONFIGURED[setting],
                                      http_status_code=404, expected_params=KW)
        assert bucket.read_state(s3, NAME, ACCOUNT) == {s: None for s in bucket.SETTINGS}
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("setting", [s for s in bucket.SETTINGS if s != "versioning"])
def test_read_state_reraises_other_errors(setting):
    """AccessDenied (or another setting's not-configured code) is never read as 'not set'."""
    other = next(c for s, c in bucket._NOT_CONFIGURED.items() if s != setting)
    for code in ("AccessDenied", other):
        s3 = _fake_boto_session().client("s3", region_name=config.REGION)
        with Stubber(s3) as stub:
            for before in bucket.SETTINGS[:bucket.SETTINGS.index(setting)]:
                stub.add_response(GET_OPS[before], aws_response(before, desired()[before]), KW)
            stub.add_client_error(GET_OPS[setting], service_error_code=code, http_status_code=403,
                                  expected_params=KW)
            with pytest.raises(ClientError):
                bucket.read_state(s3, NAME, ACCOUNT)


def test_read_state_versioning_access_denied_is_raised():
    s3 = _fake_boto_session().client("s3", region_name=config.REGION)
    with Stubber(s3) as stub:
        for before in ("ownership", "public_access_block", "encryption"):
            stub.add_response(GET_OPS[before], aws_response(before, desired()[before]), KW)
        stub.add_client_error("get_bucket_versioning", service_error_code="AccessDenied",
                              http_status_code=403, expected_params=KW)
        with pytest.raises(ClientError):
            bucket.read_state(s3, NAME, ACCOUNT)


def test_read_state_versioning_network_error_is_raised():
    """Regression (fixed): versioning has no not-configured code and a network error has no AWS
    code; None == None used to swallow the error and read it as 'versioning not set'."""
    class S3:
        def __getattr__(self, op):
            def call(**kw):
                if op == "get_bucket_versioning":
                    raise EndpointConnectionError(endpoint_url="https://s3.example.invalid")
                setting = next(s for s, o in GET_OPS.items() if o == op)
                return aws_response(setting, desired()[setting])
            return call

    with pytest.raises(EndpointConnectionError):
        bucket.read_state(S3(), NAME, ACCOUNT)


@pytest.mark.parametrize("setting", [s for s in bucket.SETTINGS if s != "versioning"])
def test_read_state_non_aws_errors_are_raised(setting):
    """The same kind of error on every other get call does propagate."""
    class S3:
        def __getattr__(self, op):
            def call(**kw):
                if op == GET_OPS[setting]:
                    raise EndpointConnectionError(endpoint_url="https://s3.example.invalid")
                s = next(s for s, o in GET_OPS.items() if o == op)
                return aws_response(s, desired()[s])
            return call

    with pytest.raises(EndpointConnectionError):
        bucket.read_state(S3(), NAME, ACCOUNT)


# --- ensure_bucket / main with Stubber (exact calls) -------------------------------------------
def test_missing_dry_run_only_head_bucket(capsys):
    session, sts_stub, _, stub = stubbed()
    stub_missing(stub)
    assert bucket.main(["--dry-run"], session=session) == 0
    stub.assert_no_pending_responses()
    sts_stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert "[dry-run] would create bucket price-intelligence-<account>" in out
    assert "Dry run: nothing was changed." in out
    assert ACCOUNT not in out


def test_missing_dry_run_actions():
    s3 = FakeS3(exists=False)
    actions = bucket.ensure_bucket(s3, ACCOUNT, dry_run=True)
    assert actions == ["create", "public_access_block", "encryption", "versioning", "policy",
                       "lifecycle", "tags"]
    assert [op for op, _ in s3.calls] == ["head_bucket"]


def test_missing_apply_exact_calls(capsys):
    session, _, _, stub = stubbed()
    want = desired()
    stub_missing(stub)
    stub.add_response("create_bucket", {}, {
        "Bucket": NAME, "CreateBucketConfiguration": {"LocationConstraint": "eu-north-1"},
        "ObjectOwnership": "BucketOwnerEnforced"})
    stub.add_response("head_bucket", {"ResponseMetadata": {"HTTPStatusCode": 200}}, KW)  # waiter
    for setting in ("public_access_block", "encryption", "versioning", "policy", "lifecycle"):
        stub.add_response(PUT_OPS[setting], {}, put_params(setting, want[setting]))
    stub.add_response("put_bucket_tagging", {}, put_params("tags", [config.PROJECT_TAG]))
    assert bucket.main([], session=session) == 0
    stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert "Applied: create, public_access_block, encryption, versioning, policy, lifecycle, tags." in out
    assert ACCOUNT not in out


def test_exists_and_matches_makes_no_writes(capsys):
    session, _, _, stub = stubbed()
    stub_reads(stub, aws_like_desired())
    assert bucket.main([], session=session) == 0
    stub.assert_no_pending_responses()  # any extra (put) call would raise UnStubbedResponseError
    out = capsys.readouterr().out
    assert "No changes" in out
    assert ACCOUNT not in out


def test_exists_and_matches_returns_no_actions():
    s3 = FakeS3(aws_like_desired())
    assert bucket.ensure_bucket(s3, ACCOUNT) == []
    assert s3.mutating_calls() == []


def test_fresh_bucket_apply_exact_puts(capsys):
    session, _, _, stub = stubbed()
    want = desired()
    stub_reads(stub, FRESH_BUCKET)
    for setting in ("encryption", "versioning", "policy", "lifecycle"):
        stub.add_response(PUT_OPS[setting], {}, put_params(setting, want[setting]))
    stub.add_response("put_bucket_tagging", {}, put_params("tags", [FOREIGN_TAG, config.PROJECT_TAG]))
    assert bucket.main([], session=session) == 0
    stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert ACCOUNT not in out  # the policy diff carries the bucket ARN: it must be redacted
    assert "<account>" in out


def test_fresh_bucket_dry_run_reads_only(capsys):
    session, _, _, stub = stubbed()
    stub_reads(stub, FRESH_BUCKET)
    assert bucket.main(["--dry-run"], session=session) == 0
    stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    for setting in ("encryption", "versioning", "policy", "lifecycle", "tags"):
        assert f"[dry-run] would set {setting}" in out
    assert "would set ownership" not in out and "would set public_access_block" not in out
    assert ACCOUNT not in out


@pytest.mark.parametrize("setting", bucket.SETTINGS)
def test_one_wrong_setting_writes_only_that_setting(setting):
    broken = {
        "ownership": "ObjectWriter",
        "public_access_block": dict(bucket.PUBLIC_ACCESS_BLOCK, BlockPublicPolicy=False),
        "encryption": FRESH_BUCKET["encryption"],
        "versioning": "Suspended",
        "policy": {"Version": "2012-10-17", "Statement": [{
            "Sid": "Old", "Effect": "Deny", "Principal": "*", "Action": "s3:DeleteBucket",
            "Resource": config.bucket_arn(ACCOUNT)}]},
        "lifecycle": [{"ID": "old", "Status": "Enabled", "Filter": {"Prefix": ""},
                       "Expiration": {"Days": 1}}],
        "tags": [FOREIGN_TAG, {"Key": "project", "Value": "old"}],
    }[setting]
    state = copy.deepcopy(desired())
    state[setting] = broken
    session, _, _, stub = stubbed()
    stub_reads(stub, state)
    value = ([FOREIGN_TAG, config.PROJECT_TAG] if setting == "tags" else desired()[setting])
    stub.add_response(PUT_OPS[setting], {}, put_params(setting, value))
    assert bucket.main([], session=session) == 0
    stub.assert_no_pending_responses()


def test_access_denied_on_a_read_exits_1_without_the_aws_message(capsys):
    session, _, _, stub = stubbed()
    stub.add_response("head_bucket", {"BucketRegion": config.REGION}, KW)
    stub.add_client_error("get_bucket_ownership_controls", service_error_code="AccessDenied",
                          service_message=f"User arn:aws:iam::{ACCOUNT}:user/x is not authorized",
                          http_status_code=403, expected_params=KW)
    with pytest.raises(SystemExit) as exc:
        bucket.main([], session=session)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "AWS error in GetBucketOwnershipControls: AccessDenied" in err
    assert ACCOUNT not in err and "not authorized" not in err


def test_failed_put_exits_1(capsys):
    session, _, _, stub = stubbed()
    stub_reads(stub, dict(desired(), versioning="Suspended"))
    stub.add_client_error("put_bucket_versioning", service_error_code="AccessDenied",
                          service_message=f"arn:aws:s3:::price-intelligence-{ACCOUNT}",
                          http_status_code=403,
                          expected_params=put_params("versioning", "Enabled"))
    with pytest.raises(SystemExit) as exc:
        bucket.main([], session=session)
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "AWS error in PutBucketVersioning: AccessDenied" in captured.err
    assert ACCOUNT not in captured.out + captured.err


def test_head_bucket_403_stops_before_any_other_call():
    session, _, _, stub = stubbed()
    stub.add_client_error("head_bucket", service_error_code="403", http_status_code=403,
                          expected_params=KW)
    with pytest.raises(SystemExit) as exc:
        bucket.main([], session=session)
    assert exc.value.code not in (0, None)
    stub.assert_no_pending_responses()


def test_non_aws_errors_propagate_from_main():
    class Boom(Exception):
        pass

    class S3:
        def head_bucket(self, **kw):
            raise Boom()

    session, sts_stub, _, _ = stubbed()
    session.clients["s3"] = S3()
    with pytest.raises(Boom):
        bucket.main([], session=session)


def test_main_uses_sts_account_and_pinned_region():
    calls = []

    class Session(FakeSession):
        def client(self, service, **kwargs):
            calls.append((service, kwargs))
            return super().client(service, **kwargs)

    session, _, _, stub = stubbed()
    session = Session(session.clients)
    stub_reads(stub, desired())
    assert bucket.main(["--dry-run"], session=session) == 0
    assert [c[0] for c in calls] == ["sts", "s3"]
    assert all(kw == {"region_name": "eu-north-1"} for _, kw in calls)


def test_help_exits_0(capsys):
    with pytest.raises(SystemExit) as exc:
        bucket.main(["--help"])
    assert exc.value.code == 0
    assert "--dry-run" in capsys.readouterr().out


# --- Invariants with the stateful fake ---------------------------------------------------------
@pytest.mark.parametrize("start", [
    pytest.param(None, id="missing"),
    pytest.param(FRESH_BUCKET, id="fresh"),
    pytest.param({}, id="nothing-configured"),
    pytest.param(aws_like_desired(), id="already-matching"),
])
@pytest.mark.parametrize("empty_filter", [False, True])
def test_second_apply_makes_no_writes(start, empty_filter):
    """Invariant 2 (idempotence), and dry-run (invariant 1) before and after."""
    s3 = FakeS3(start, exists=start is not None, empty_filter=empty_filter)
    planned = bucket.ensure_bucket(s3, ACCOUNT, dry_run=True)
    assert s3.mutating_calls() == []
    s3.calls.clear()

    applied = bucket.ensure_bucket(s3, ACCOUNT)
    assert applied == planned
    ops = s3.mutating_calls()
    assert ops.count("create_bucket") == (1 if start is None else 0)
    assert len(ops) == len(set(ops))  # nothing written twice
    assert bucket.diff_settings(bucket.read_state(s3, NAME, ACCOUNT), desired()) == []

    s3.calls.clear()
    assert bucket.ensure_bucket(s3, ACCOUNT) == []
    assert s3.mutating_calls() == []
    assert bucket.ensure_bucket(s3, ACCOUNT, dry_run=True) == []


def test_every_call_but_create_carries_expected_bucket_owner():
    """Invariant 3 (FakeS3._record asserts it on every call)."""
    s3 = FakeS3(exists=False)
    bucket.ensure_bucket(s3, ACCOUNT)
    create = [kw for op, kw in s3.calls if op == "create_bucket"]
    assert create == [{"Bucket": NAME, "CreateBucketConfiguration": {"LocationConstraint": "eu-north-1"},
                       "ObjectOwnership": "BucketOwnerEnforced"}]
    assert all(kw["Bucket"] == NAME for _, kw in s3.calls)


def test_foreign_tags_survive():
    """Invariant 5."""
    s3 = FakeS3(dict(FRESH_BUCKET, tags=[FOREIGN_TAG, {"Key": "Name", "Value": "x"}]))
    bucket.ensure_bucket(s3, ACCOUNT)
    assert _aws.tags_to_dict(s3.state["tags"]) == {"owner": "someone-else", "Name": "x",
                                                   "project": "price-intelligence"}


def test_resulting_bucket_is_private_encrypted_and_versioned():
    """Least privilege on the bucket itself: after an apply from nothing, no public access, no
    ACLs, no Allow in the bucket policy, encryption at rest, versioning on."""
    s3 = FakeS3(exists=False)
    bucket.ensure_bucket(s3, ACCOUNT)
    state = s3.state
    assert state["ownership"] == "BucketOwnerEnforced"
    assert all(state["public_access_block"].values()) and len(state["public_access_block"]) == 4
    assert {s["Effect"] for s in state["policy"]["Statement"]} == {"Deny"}
    assert state["encryption"][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    assert state["versioning"] == "Enabled"
    # The script only creates, reads and puts configuration: no delete, no ACL, no object calls.
    assert set(op for op, _ in s3.calls) <= {"create_bucket", *READ_ONLY_OPS, *PUT_OPS.values()}
    assert "put_bucket_ownership_controls" not in [op for op, _ in s3.calls]


# --- Source hygiene ----------------------------------------------------------------------------
def test_source_holds_no_account_ids_or_emails():
    text = (REPO_ROOT / "infra" / "bucket.py").read_text()
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", text)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)


def test_docstring_says_what_cost_and_usage():
    doc = bucket.__doc__
    assert "What it leaves configured" in doc and "Cost" in doc and "Usage" in doc
    assert "python -m infra.bucket --dry-run" in doc


def test_importing_bucket_does_not_load_boto3():
    code = "import sys, infra.bucket; print('boto3' in sys.modules, 'botocore' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip() == "False False"
