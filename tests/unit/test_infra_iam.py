"""Unit tests of the F3 IAM scripts: `infra/instance_role.py`, `infra/scheduler_role.py` and
`infra/deploy_role.py`.

No test reaches AWS. Two kinds of double are used:
- `botocore.stub.Stubber` on real clients built with fake credentials in eu-north-1: every call
  must be queued in order with its exact parameters, and botocore validates parameters and
  responses against the service model. An unexpected call (a write in dry-run, a call on the old
  role) fails the test.
- `FakeAws`: a small stateful fake of STS, EC2, IAM, Bedrock and CloudTrail for the "apply twice"
  and "dry-run on a drifted account" flows. It validates every request against the botocore model
  and refuses any write on the old roles.

The fake account is `000000000000`; instance IDs and profile IDs are made up. Expected policy
documents are written out literally, so a wrong builder fails here.
"""
from __future__ import annotations

import copy
import datetime as dt
import functools
import json
import re
import urllib.parse

import boto3
import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from botocore.validate import validate_parameters

from infra import _aws, config
from infra import deploy_role, instance_role, scheduler_role

pytestmark = pytest.mark.unit

ACCOUNT = "000000000000"
INSTANCE = "i-0abc1234def567890"
OTHER_INSTANCE = "i-0fedcba9876543210"
SERVICES = ("sts", "ec2", "iam", "bedrock", "cloudtrail")
WHEN = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
PROJECT_TAG = {"Key": "project", "Value": "price-intelligence"}

BUCKET = f"arn:aws:s3:::price-intelligence-{ACCOUNT}"
INSTANCE_ARN = f"arn:aws:ec2:eu-north-1:{ACCOUNT}:instance/{INSTANCE}"
DEEPSEEK_MODEL = "arn:aws:bedrock:eu-north-1::foundation-model/deepseek.v3.2"
HAIKU_SYSTEM_ID = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
HAIKU_SYSTEM = f"arn:aws:bedrock:eu-north-1:{ACCOUNT}:inference-profile/{HAIKU_SYSTEM_ID}"
APP_PREFIX = f"arn:aws:bedrock:eu-north-1:{ACCOUNT}:application-inference-profile/"
DEEPSEEK_APP = APP_PREFIX + "abcd1234efgh"
HAIKU_APP = APP_PREFIX + "ijkl5678mnop"
HAIKU_MODELS = [f"arn:aws:bedrock:{r}::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"
                for r in ("eu-central-1", "eu-north-1", "eu-west-1", "eu-west-3")]
OIDC_ARN = f"arn:aws:iam::{ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"
MAIN_SUBJECT = "repo:Wayakit-Biotechnology-in-Hygiene/wayakit-price-prediction:ref:refs/heads/main"
OLD_ROLES = ("Wayakit-EC2-Secrets-Role", "Wayakit-EventBridge-Role")
OLD_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/Wayakit-EC2-Secrets-Role"
OLD_PROFILE_ARN = f"arn:aws:iam::{ACCOUNT}:instance-profile/Wayakit-EC2-Secrets-Role"
DESCRIBE_FILTERS = [
    {"Name": "tag:Name", "Values": ["wayakit-app-server"]},
    {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
]
READ_PREFIXES = ("Get", "List", "Describe", "Lookup")


# --- Expected documents, written out by hand ---------------------------------------------------
def expected_instance_trust():
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


def expected_instance_policy(profiles=(DEEPSEEK_APP, HAIKU_APP), haiku_app=True):
    haiku_profiles = sorted([HAIKU_SYSTEM] + ([HAIKU_APP] if haiku_app else []))
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ReadPiSecret", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
         "Resource": f"arn:aws:secretsmanager:eu-north-1:{ACCOUNT}:secret:prod/wayakit-app-??????"},
        {"Sid": "ListPiBucket", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": BUCKET},
        {"Sid": "ReadWritePiObjects", "Effect": "Allow",
         "Action": ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"],
         "Resource": [f"{BUCKET}/data/*", f"{BUCKET}/models/*", f"{BUCKET}/state/*", f"{BUCKET}/logs/*"]},
        # F3 review: releases (and the `current` pointer) are written only by the deploy role.
        {"Sid": "ReadPiReleases", "Effect": "Allow", "Action": "s3:GetObject", "Resource": f"{BUCKET}/releases/*"},
        {"Sid": "PutPiMetrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
         "Condition": {"StringEquals": {"cloudwatch:namespace": "PriceIntelligence"}}},
        {"Sid": "PublishPiAlerts", "Effect": "Allow", "Action": "sns:Publish",
         "Resource": f"arn:aws:sns:eu-north-1:{ACCOUNT}:price-intelligence-alerts"},
        {"Sid": "InvokeDeepSeek", "Effect": "Allow", "Action": "bedrock:InvokeModel", "Resource": DEEPSEEK_MODEL},
        {"Sid": "InvokePiInferenceProfiles", "Effect": "Allow", "Action": "bedrock:InvokeModel",
         "Resource": sorted([HAIKU_SYSTEM, *profiles])},
        {"Sid": "InvokeHaikuThroughProfiles", "Effect": "Allow", "Action": "bedrock:InvokeModel",
         "Resource": HAIKU_MODELS,
         "Condition": {"StringEquals": {"bedrock:InferenceProfileArn": haiku_profiles}}},
    ]}


def expected_scheduler_trust():
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}}}]}


def expected_scheduler_policy(instance=INSTANCE):
    return {"Version": "2012-10-17", "Statement": [{
        "Sid": "StartPiInstance", "Effect": "Allow", "Action": "ec2:StartInstances",
        "Resource": f"arn:aws:ec2:eu-north-1:{ACCOUNT}:instance/{instance}"}]}


def expected_deploy_trust():
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Federated": OIDC_ARN}, "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {"StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                                       "token.actions.githubusercontent.com:sub": MAIN_SUBJECT}}}]}


def expected_deploy_policy(instance=INSTANCE):
    arn = f"arn:aws:ec2:eu-north-1:{ACCOUNT}:instance/{instance}"
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "PublishReleases", "Effect": "Allow", "Action": "s3:PutObject", "Resource": f"{BUCKET}/releases/*"},
        {"Sid": "ReadAndMoveCurrentPointer", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": f"{BUCKET}/releases/current"},
        {"Sid": "StartPiInstance", "Effect": "Allow", "Action": "ec2:StartInstances", "Resource": arn},
        {"Sid": "SetPiModeTag", "Effect": "Allow", "Action": "ec2:CreateTags", "Resource": arn,
         "Condition": {"ForAllValues:StringEquals": {"aws:TagKeys": ["pi-mode"]}}},
    ]}


class JsonDoc:
    """Stubber parameter matcher: the sent value is a JSON string equal (as data) to `doc`."""

    def __init__(self, doc):
        self.doc = doc

    def __eq__(self, other):
        return isinstance(other, str) and json.loads(other) == self.doc

    def __ne__(self, other):
        return not self == other

    def __repr__(self):
        return f"JsonDoc({self.doc!r})"


def urlencoded(doc):
    """How IAM returns a policy document on the wire."""
    return urllib.parse.quote(json.dumps(doc))


# --- Pure builders -----------------------------------------------------------------------------
def test_instance_trust_policy_is_ec2_only():
    assert instance_role.trust_policy() == expected_instance_trust()


def test_instance_policy_matches_the_spec():
    profiles = {"price-intelligence-deepseek-v3-2": DEEPSEEK_APP, "price-intelligence-haiku-4-5": HAIKU_APP}
    assert instance_role.instance_policy(ACCOUNT, profiles, list(reversed(HAIKU_MODELS))) == \
        expected_instance_policy()


def test_instance_policy_without_application_profiles():
    """Missing profiles are left out; DeepSeek stays reachable by its plain model ID (fallback)."""
    policy = instance_role.instance_policy(ACCOUNT, {}, HAIKU_MODELS)
    assert policy == expected_instance_policy(profiles=(), haiku_app=False)
    deepseek = {"price-intelligence-deepseek-v3-2": DEEPSEEK_APP}
    assert instance_role.instance_policy(ACCOUNT, deepseek, HAIKU_MODELS) == \
        expected_instance_policy(profiles=(DEEPSEEK_APP,), haiku_app=False)


@pytest.mark.parametrize("profiles, models", [
    ({"another-profile": DEEPSEEK_APP}, HAIKU_MODELS),                                    # unknown name
    ({"price-intelligence-haiku-4-5": HAIKU_APP.replace(ACCOUNT, "111111111111")}, HAIKU_MODELS),
    ({"price-intelligence-haiku-4-5": HAIKU_APP.replace("eu-north-1", "eu-west-1")}, HAIKU_MODELS),
    ({"price-intelligence-haiku-4-5": HAIKU_SYSTEM}, HAIKU_MODELS),                      # not an application profile
    ({}, []),                                                                             # no Haiku model
    ({}, ["arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"]),
    ({}, ["arn:aws:bedrock:eu-*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"]),
    ({}, ["arn:aws:bedrock:eu-west-1::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0"]),
    ({}, ["arn:aws:bedrock:eu-west-1::foundation-model/*"]),
    ({}, [HAIKU_MODELS[0] + "x"]),
])
def test_instance_policy_rejects_foreign_or_wide_arns(profiles, models):
    with pytest.raises(ValueError):
        instance_role.instance_policy(ACCOUNT, profiles, models)


@pytest.mark.parametrize("suffix", ["*", "", "?", "abc*", "abcdefghijk", "abcdefghijk*", "abcdefghijk?",
                                    "abcdefghijklm", "ABCDEFGHIJKL", "abcdefghijk\n", "abcdefghijkl\n",
                                    "abcdefghijkl/x", "abcdefghijk٠", " abcdefghijkl"])
@pytest.mark.parametrize("name", ["price-intelligence-haiku-4-5", "price-intelligence-deepseek-v3-2"])
def test_instance_policy_rejects_a_wildcard_or_malformed_profile_id(name, suffix):
    """Fixed bug (was xfail): a prefix-only check let `.../application-inference-profile/*` (or an
    empty ID) through as a wildcard Resource of bedrock:InvokeModel. IDs are exactly [a-z0-9]{12}."""
    with pytest.raises(ValueError):
        instance_role.instance_policy(ACCOUNT, {name: APP_PREFIX + suffix}, HAIKU_MODELS)


@pytest.mark.parametrize("value", [None, 123, b"x", ["x"]])
def test_instance_policy_rejects_non_string_profile_arns(value):
    with pytest.raises(ValueError):
        instance_role.instance_policy(ACCOUNT, {"price-intelligence-haiku-4-5": value}, HAIKU_MODELS)


BAD_ACCOUNTS = ["*", "", "00000000000", "0000000000000", "00000000000٠", "０００００００００００", "00000000000a",
                f" {ACCOUNT}", f"{ACCOUNT}\n", "000000*00000", None, 111111111111, b"000000000000"]
ACCOUNT_BUILDERS = {
    "instance_policy": lambda a: instance_role.instance_policy(a, {}, HAIKU_MODELS),
    "scheduler_trust": scheduler_role.trust_policy,
    "scheduler_policy": lambda a: scheduler_role.scheduler_policy(a, INSTANCE),
    "deploy_trust": deploy_role.trust_policy,
    "deploy_policy": lambda a: deploy_role.deploy_policy(a, INSTANCE),
}


@pytest.mark.parametrize("bad", BAD_ACCOUNTS, ids=repr)
@pytest.mark.parametrize("builder", list(ACCOUNT_BUILDERS))
def test_account_scoped_builders_reject_anything_but_twelve_ascii_digits(builder, bad):
    """A `*` or empty account would turn every ARN of the policy into a wildcard or a malformed ARN."""
    with pytest.raises(ValueError):
        ACCOUNT_BUILDERS[builder](bad)


@pytest.mark.parametrize("bad", BAD_ACCOUNTS, ids=repr)
def test_check_account_rejects_anything_but_twelve_ascii_digits(bad):
    with pytest.raises(ValueError):
        instance_role.check_account(bad)
    assert instance_role.check_account(ACCOUNT) is None


def test_check_account_lives_in_config_and_the_builders_use_that_one():
    """Moved to `infra/config.py` (F3 follow-up D); `instance_role` re-exports the same function,
    which the scheduler and deploy builders import."""
    from infra import config
    assert instance_role.check_account is config.check_account
    assert scheduler_role.check_account is config.check_account
    assert deploy_role.check_account is config.check_account
    assert config.check_account("111111111111") is None


def test_scheduler_builders_match_the_spec():
    assert scheduler_role.trust_policy(ACCOUNT) == expected_scheduler_trust()
    assert scheduler_role.scheduler_policy(ACCOUNT, INSTANCE) == expected_scheduler_policy()


def test_deploy_builders_match_the_spec():
    assert deploy_role.trust_policy(ACCOUNT) == expected_deploy_trust()
    assert deploy_role.deploy_policy(ACCOUNT, INSTANCE) == expected_deploy_policy()


def test_deploy_trust_is_main_only_with_string_equals():
    cond = deploy_role.trust_policy(ACCOUNT)["Statement"][0]["Condition"]
    assert list(cond) == ["StringEquals"]
    assert cond["StringEquals"]["token.actions.githubusercontent.com:sub"] == MAIN_SUBJECT
    assert "*" not in json.dumps(cond) and "?" not in json.dumps(cond)
    assert config.DEPLOY_BRANCH == "main"


@pytest.mark.parametrize("bad", ["*", "", "i-*", "i-0abc", "i-0ABC1234DEF567890", f"{INSTANCE},{OTHER_INSTANCE}",
                                 f"{INSTANCE}\n", f" {INSTANCE}", "i-0abc1234def5678901", None, 123,
                                 "instance/i-0abc1234def567890"])
def test_instance_scoped_builders_reject_anything_but_one_instance_id(bad):
    with pytest.raises(ValueError):
        scheduler_role.scheduler_policy(ACCOUNT, bad)
    with pytest.raises(ValueError):
        deploy_role.deploy_policy(ACCOUNT, bad)


ALL_DOCS = {
    "instance trust": lambda: instance_role.trust_policy(),
    "instance policy": lambda: instance_role.instance_policy(
        ACCOUNT, {"price-intelligence-deepseek-v3-2": DEEPSEEK_APP, "price-intelligence-haiku-4-5": HAIKU_APP},
        HAIKU_MODELS),
    "scheduler trust": lambda: scheduler_role.trust_policy(ACCOUNT),
    "scheduler policy": lambda: scheduler_role.scheduler_policy(ACCOUNT, INSTANCE),
    "deploy trust": lambda: deploy_role.trust_policy(ACCOUNT),
    "deploy policy": lambda: deploy_role.deploy_policy(ACCOUNT, INSTANCE),
}


@pytest.mark.parametrize("name", sorted(ALL_DOCS))
def test_documents_are_allow_only_without_wildcard_actions_or_not_elements(name):
    doc = ALL_DOCS[name]()
    for st in doc["Statement"]:
        assert st["Effect"] == "Allow"
        assert not {"NotAction", "NotResource", "NotPrincipal"} & set(st)
        actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        assert actions and all("*" not in a and "?" not in a for a in actions)


@pytest.mark.parametrize("name", ["instance policy", "scheduler policy", "deploy policy"])
def test_wildcards_only_where_documented(name):
    """`*` only as the PutMetricData resource (with the namespace condition) and the end of a
    bucket prefix (data, models, state, logs, releases): never the whole bucket; `?` only in the
    secret's 6-character random suffix."""
    secret = f"arn:aws:secretsmanager:eu-north-1:{ACCOUNT}:secret:prod/wayakit-app-??????"
    allowed = {"*", *(f"{BUCKET}/{p}/*" for p in ("data", "models", "state", "logs", "releases"))}
    for st in ALL_DOCS[name]()["Statement"]:
        resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        for r in resources:
            assert "*" not in r or r in allowed, r
            assert "?" not in r or r == secret, r
        if "*" in resources:
            assert st["Sid"] == "PutPiMetrics"
            assert st["Condition"] == {"StringEquals": {"cloudwatch:namespace": "PriceIntelligence"}}


def test_documents_fit_iam_limits_and_are_valid_request_parameters():
    """Inline role policies: 10,240 characters per role; trust policies: 2,048 by default."""
    model = botocore.session.get_session().get_service_model("iam")
    for name, build in ALL_DOCS.items():
        text = _aws.policy_json(build())
        assert len(text) <= (2048 if "trust" in name else 10240), name
        if "trust" in name:
            validate_parameters({"RoleName": "x", "AssumeRolePolicyDocument": text},
                                model.operation_model("CreateRole").input_shape)
        else:
            validate_parameters({"RoleName": "x", "PolicyName": "p", "PolicyDocument": text},
                                model.operation_model("PutRolePolicy").input_shape)


def test_sids_are_unique_and_valid():
    for name, build in ALL_DOCS.items():
        sids = [st["Sid"] for st in build()["Statement"] if "Sid" in st]
        assert len(sids) == len(set(sids)), name
        assert all(re.fullmatch(r"[A-Za-z0-9]+", s) for s in sids), name


def test_names_and_constants():
    assert instance_role.POLICY_NAME == "price-intelligence-instance-access"
    assert scheduler_role.POLICY_NAME == "price-intelligence-scheduler-access"
    assert deploy_role.POLICY_NAME == "price-intelligence-deploy-access"
    assert instance_role.MANAGED_POLICIES == ("arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",)
    assert instance_role.MAX_SESSION_SECONDS == deploy_role.MAX_SESSION_SECONDS == 3600
    for desc in (instance_role.DESCRIPTION, scheduler_role.DESCRIPTION, deploy_role.DESCRIPTION):
        validate_parameters({"RoleName": "x", "AssumeRolePolicyDocument": "{}", "Description": desc},
                            botocore.session.get_session().get_service_model("iam")
                            .operation_model("CreateRole").input_shape)


@pytest.mark.parametrize("value", [
    expected_instance_trust(),
    json.dumps(expected_instance_trust()),
    urlencoded(expected_instance_trust()),
])
def test_as_policy_accepts_dict_json_and_urlencoded(value):
    assert instance_role.as_policy(value) == expected_instance_trust()


@pytest.mark.parametrize("text, expected", [
    (f"instance {INSTANCE}", "instance i-<instance>"),
    (f"{INSTANCE_ARN}", f"arn:aws:ec2:eu-north-1:{ACCOUNT}:instance/i-<instance>"),
    (f'"Resource": "{INSTANCE_ARN}"', f'"Resource": "arn:aws:ec2:eu-north-1:{ACCOUNT}:instance/i-<instance>"'),
    ("i-0abc1234", "i-<instance>"),
    ("no ids here", "no ids here"),
])
def test_redact_instance_ids(text, expected):
    assert instance_role.redact_instance_ids(text) == expected


# --- Audit summary (pure) ----------------------------------------------------------------------
def test_act_and_info_mask_any_account_and_instance_id(capsys):
    """F3 review: after the known account and the delimited instance IDs, the catch-all masks any
    other 12-digit number (e.g. another account in a CloudTrail record of the audit) and any
    instance ID glued to other characters."""
    text = f"{OLD_ROLE_ARN} used by arn:aws:iam::123456789012:user/x on {INSTANCE} and x{OTHER_INSTANCE}"
    line = instance_role._info(text, account=ACCOUNT)
    assert line == ("arn:aws:iam::<account>:role/Wayakit-EC2-Secrets-Role used by arn:aws:iam::<account>:user/x "
                    "on i-<instance> and x<instance-id>")
    act = instance_role._act(text, dry_run=True, account=ACCOUNT)
    assert act == "[dry-run] would " + line
    out = capsys.readouterr().out
    assert "123456789012" not in out and ACCOUNT not in out
    assert INSTANCE not in out and OTHER_INSTANCE not in out


def assume_record(session, *, by="ec2.amazonaws.com", event="AssumeRole", eid=None):
    return {"eventID": eid or f"a-{session}-{by}", "eventName": event, "eventSource": "sts.amazonaws.com",
            "userIdentity": {"type": "AWSService", "invokedBy": by},
            "requestParameters": {"roleArn": OLD_ROLE_ARN, "roleSessionName": session}}


def session_record(event, source="secretsmanager.amazonaws.com", secret=None, issuer=OLD_ROLE_ARN, eid=None):
    rec = {"eventID": eid or f"s-{event}-{secret}-{issuer}", "eventName": event, "eventSource": source,
           "userIdentity": {"type": "AssumedRole",
                            "sessionContext": {"sessionIssuer": {"type": "Role", "arn": issuer}}},
           "requestParameters": {}}
    if secret:
        rec["requestParameters"]["secretId"] = secret
    return rec


PI_SECRET_ARN = f"arn:aws:secretsmanager:eu-north-1:{ACCOUNT}:secret:prod/wayakit-app-AbC123"


def test_summarize_audit_only_pi():
    s = instance_role.summarize_audit(
        OLD_ROLE_ARN, [assume_record(INSTANCE)] * 2 + [assume_record(INSTANCE, eid="x")],
        [session_record("GetSecretValue", secret="prod/wayakit-app"),
         session_record("GetSecretValue", secret=PI_SECRET_ARN, eid="2"),
         session_record("PutMetricData", source="monitoring.amazonaws.com")],
        [INSTANCE], [INSTANCE])
    assert s["only_pi"] is True and s["evidence"] is True
    assert s["assume_events"] == 3 and s["assumed_by"] == {"ec2.amazonaws.com": 3}
    assert s["sessions"] == {INSTANCE: 3}
    assert s["calls"] == {"secretsmanager:GetSecretValue": 2, "monitoring:PutMetricData": 1}
    assert s["secrets_read"] == {"prod/wayakit-app": 1, PI_SECRET_ARN: 1}
    assert s["other_sessions"] == s["other_instances"] == s["other_principals"] == s["other_secrets"] == []


@pytest.mark.parametrize("case", ["no sessions", "no pi ids", "other session", "other instance",
                                  "other principal", "other secret", "similar secret name"])
def test_summarize_audit_not_only_pi(case):
    assume, records, pi, assoc = [assume_record(INSTANCE)], [], [INSTANCE], [INSTANCE]
    if case == "no sessions":
        assume = []
    elif case == "no pi ids":
        pi = []
    elif case == "other session":
        assume.append(assume_record(OTHER_INSTANCE))
    elif case == "other instance":
        assoc = [INSTANCE, OTHER_INSTANCE]
    elif case == "other principal":
        assume.append(assume_record(INSTANCE, by="lambda.amazonaws.com"))
    elif case == "other secret":
        records = [session_record("GetSecretValue", secret="prod/voz-del-cliente")]
    elif case == "similar secret name":
        records = [session_record("GetSecretValue", secret="prod/wayakit-app-extra")]
    s = instance_role.summarize_audit(OLD_ROLE_ARN, assume, records, pi, assoc)
    assert s["only_pi"] is False
    if case == "no sessions":
        assert s["evidence"] is False


def test_summarize_audit_ignores_other_issuers_and_non_assume_events():
    other = f"arn:aws:iam::{ACCOUNT}:role/someone-else"
    s = instance_role.summarize_audit(
        OLD_ROLE_ARN,
        [assume_record(INSTANCE), assume_record(OTHER_INSTANCE, event="GetRole")],
        [session_record("GetSecretValue", secret="prod/other", issuer=other)],
        [INSTANCE], [INSTANCE])
    assert s["calls"] == {} and s["secrets_read"] == {}
    assert s["sessions"] == {INSTANCE: 1} and s["only_pi"] is True


# --- Stubber session ---------------------------------------------------------------------------
class StubbedSession:
    """Real botocore clients (fake credentials, eu-north-1) with a Stubber each. Records every
    `client()` request and every operation name, in call order across services."""

    def __init__(self):
        base = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                             region_name=config.REGION)
        self.clients = {n: base.client(n, region_name=config.REGION) for n in SERVICES}
        self.stubs = {n: Stubber(c) for n, c in self.clients.items()}
        self.client_requests = []
        self.ops = []
        for name, client in self.clients.items():
            client.meta.events.register("before-parameter-build", self._recorder(name))

    def _recorder(self, service):
        def record(model, params=None, **_):
            self.ops.append((service, model.name, copy.deepcopy(params)))
        return record

    def client(self, service, **kwargs):
        self.client_requests.append((service, kwargs))
        return self.clients[service]

    def __getattr__(self, name):
        if name in SERVICES:
            return self.stubs[name]
        raise AttributeError(name)

    def op_names(self, service=None):
        return [op for svc, op, _ in self.ops if service in (None, svc)]


def run(s, module, argv, capsys):
    """Run `module.main` on the stubbed session; every queued response must be consumed, the
    output never carries the account or an instance ID, and every client is pinned to eu-north-1."""
    for stub in s.stubs.values():
        stub.activate()
    try:
        result = module.main(argv, session=s)
    finally:
        out, err = capsys.readouterr()
        for stub in s.stubs.values():
            stub.deactivate()
    for stub in s.stubs.values():
        stub.assert_no_pending_responses()
    assert ACCOUNT not in out + err
    assert INSTANCE not in out + err and OTHER_INSTANCE not in out + err
    assert all(kw == {"region_name": "eu-north-1"} for _, kw in s.client_requests)
    assert_old_roles_untouched(s.ops)
    return result, out


def assert_old_roles_untouched(ops):
    """No call other than Get/List/Describe/Lookup ever names an old role."""
    for _, op, params in ops:
        if not op.startswith(READ_PREFIXES):
            assert not set(OLD_ROLES) & set(json.dumps(params or {}, default=str).split('"')), (op, params)


def assert_read_only(s):
    writes = [op for op in s.op_names() if not op.startswith(READ_PREFIXES)]
    assert writes == [], writes


def sts(s):
    s.sts.add_response("get_caller_identity",
                       {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/x", "UserId": "AIDAEXAMPLE"}, {})


def ec2_lookup(s, pages=((INSTANCE,),)):
    for i, ids in enumerate(pages):
        params = {"Filters": DESCRIBE_FILTERS}
        if i:
            params["NextToken"] = f"t{i}"
        response = {"Reservations": [{"Instances": [{"InstanceId": x} for x in ids]}]}
        if i + 1 < len(pages):
            response["NextToken"] = f"t{i + 1}"
        s.ec2.add_response("describe_instances", response, params)


def profile_summary(name, arn):
    return {"inferenceProfileName": name, "inferenceProfileArn": arn, "inferenceProfileId": arn.rsplit("/", 1)[1],
            "models": [{"modelArn": HAIKU_MODELS[0]}], "status": "ACTIVE", "type": "APPLICATION"}


def bedrock_reads(s, *, deepseek=True, haiku=True, models=HAIKU_MODELS, paged=False):
    found = []
    if deepseek:
        found.append(profile_summary("price-intelligence-deepseek-v3-2", DEEPSEEK_APP))
    if haiku:
        found.append(profile_summary("price-intelligence-haiku-4-5", HAIKU_APP))
    other = profile_summary("someone-elses-profile", APP_PREFIX + "zzzz9999zzzz")
    if paged:
        s.bedrock.add_response("list_inference_profiles",
                               {"inferenceProfileSummaries": [other] + found[:1], "nextToken": "n1"},
                               {"typeEquals": "APPLICATION"})
        s.bedrock.add_response("list_inference_profiles", {"inferenceProfileSummaries": found[1:]},
                               {"typeEquals": "APPLICATION", "nextToken": "n1"})
    else:
        s.bedrock.add_response("list_inference_profiles", {"inferenceProfileSummaries": [other] + found},
                               {"typeEquals": "APPLICATION"})
    system = {"inferenceProfileName": "EU Claude Haiku 4.5", "inferenceProfileArn": HAIKU_SYSTEM,
              "inferenceProfileId": HAIKU_SYSTEM_ID, "models": [{"modelArn": m} for m in models],
              "status": "ACTIVE", "type": "SYSTEM_DEFINED"}
    s.bedrock.add_response("get_inference_profile", system, {"inferenceProfileIdentifier": HAIKU_SYSTEM_ID})
    if haiku:
        app = dict(profile_summary("price-intelligence-haiku-4-5", HAIKU_APP),
                   models=[{"modelArn": m} for m in reversed(models)])
        s.bedrock.add_response("get_inference_profile", app, {"inferenceProfileIdentifier": HAIKU_APP})


def role_body(name, *, trust, description, max_session=3600, tags=(PROJECT_TAG,), encoded=True):
    role = {"Path": "/", "RoleName": name, "RoleId": "AROAEXAMPLEEXAMPLE01",
            "Arn": f"arn:aws:iam::{ACCOUNT}:role/{name}", "CreateDate": WHEN,
            "AssumeRolePolicyDocument": urlencoded(trust) if encoded else json.dumps(trust),
            "MaxSessionDuration": max_session, "Tags": list(tags)}
    if description is not None:
        role["Description"] = description
    return role


def no_such_entity(stub, op, params):
    stub.add_client_error(op, "NoSuchEntity", http_status_code=404, expected_params=params)


def role_missing(s, name):
    no_such_entity(s.iam, "get_role", {"RoleName": name})


def role_created(s, name, trust, description, policy_name, policy):
    s.iam.add_response("create_role", {"Role": role_body(name, trust=trust, description=description)},
                       {"RoleName": name, "AssumeRolePolicyDocument": JsonDoc(trust), "Description": description,
                        "MaxSessionDuration": 3600, "Tags": [PROJECT_TAG]})
    s.iam.add_response("put_role_policy", {},
                       {"RoleName": name, "PolicyName": policy_name, "PolicyDocument": JsonDoc(policy)})


def role_up_to_date(s, name, trust, description, policy_name, policy, *, extra_inline=()):
    s.iam.add_response("get_role", {"Role": role_body(name, trust=trust, description=description)},
                       {"RoleName": name})
    s.iam.add_response("get_role_policy",
                       {"RoleName": name, "PolicyName": policy_name, "PolicyDocument": urlencoded(policy)},
                       {"RoleName": name, "PolicyName": policy_name})
    s.iam.add_response("list_role_policies", {"PolicyNames": [policy_name, *extra_inline]}, {"RoleName": name})


def instance_profile_body(roles=("price-intelligence-instance",), tags=(PROJECT_TAG,)):
    return {"InstanceProfile": {
        "Path": "/", "InstanceProfileName": "price-intelligence-instance", "InstanceProfileId": "AIPAEXAMPLEEXAMPLE01",
        "Arn": f"arn:aws:iam::{ACCOUNT}:instance-profile/price-intelligence-instance", "CreateDate": WHEN,
        "Tags": list(tags),
        "Roles": [role_body(r, trust=expected_instance_trust(), description=None) for r in roles]}}


IR = "price-intelligence-instance"
IR_POLICY = "price-intelligence-instance-access"
SSM = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"


# --- instance_role.main ------------------------------------------------------------------------
def test_instance_role_dry_run_on_an_empty_account_only_reads(capsys):
    s = StubbedSession()
    sts(s)
    bedrock_reads(s)
    role_missing(s, IR)
    no_such_entity(s.iam, "get_instance_profile", {"InstanceProfileName": IR})
    result, out = run(s, instance_role, ["--dry-run"], capsys)
    assert result == 0
    assert s.op_names() == ["GetCallerIdentity", "ListInferenceProfiles", "GetInferenceProfile",
                            "GetInferenceProfile", "GetRole", "GetInstanceProfile"]
    assert_read_only(s)
    assert f"[dry-run] would create role {IR}" in out
    assert f"[dry-run] would attach managed policy {SSM}" in out
    assert f"[dry-run] would create instance profile {IR}" in out
    assert f"[dry-run] would add role {IR} to instance profile {IR}" in out
    assert "nothing was changed" in out
    assert "<account>" in out


def test_instance_role_apply_on_an_empty_account(capsys):
    s = StubbedSession()
    sts(s)
    bedrock_reads(s)
    role_missing(s, IR)
    role_created(s, IR, expected_instance_trust(), instance_role.DESCRIPTION, IR_POLICY, expected_instance_policy())
    s.iam.add_response("attach_role_policy", {}, {"RoleName": IR, "PolicyArn": SSM})
    no_such_entity(s.iam, "get_instance_profile", {"InstanceProfileName": IR})
    s.iam.add_response("create_instance_profile", instance_profile_body(roles=()),
                       {"InstanceProfileName": IR, "Tags": [PROJECT_TAG]})
    s.iam.add_response("add_role_to_instance_profile", {}, {"InstanceProfileName": IR, "RoleName": IR})
    result, out = run(s, instance_role, [], capsys)
    assert result == 0
    assert s.op_names("iam") == ["GetRole", "CreateRole", "PutRolePolicy", "AttachRolePolicy",
                                 "GetInstanceProfile", "CreateInstanceProfile", "AddRoleToInstanceProfile"]
    assert "[dry-run]" not in out and f"-> create role {IR}" in out


def test_instance_role_up_to_date_makes_no_write(capsys):
    s = StubbedSession()
    sts(s)
    bedrock_reads(s, paged=True)
    role_up_to_date(s, IR, expected_instance_trust(), instance_role.DESCRIPTION, IR_POLICY,
                    expected_instance_policy(), extra_inline=("legacy-inline",))
    s.iam.add_response("list_attached_role_policies",
                       {"AttachedPolicies": [{"PolicyName": "AmazonSSMManagedInstanceCore", "PolicyArn": SSM},
                                             {"PolicyName": "Extra", "PolicyArn": "arn:aws:iam::aws:policy/Extra"}]},
                       {"RoleName": IR})
    s.iam.add_response("get_instance_profile", instance_profile_body(), {"InstanceProfileName": IR})
    result, out = run(s, instance_role, [], capsys)
    assert result == 0
    assert_read_only(s)
    assert "[WARN] role price-intelligence-instance has another inline policy 'legacy-inline'" in out
    assert "[WARN] role price-intelligence-instance also has managed policy arn:aws:iam::aws:policy/Extra" in out
    assert f"Inline policy {IR_POLICY}: up to date" in out


def test_instance_role_fixes_drift_with_exact_calls(capsys):
    s = StubbedSession()
    sts(s)
    bedrock_reads(s)
    wrong_trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {
        "Service": ["ec2.amazonaws.com", "lambda.amazonaws.com"]}, "Action": "sts:AssumeRole"}]}
    s.iam.add_response("get_role", {"Role": role_body(IR, trust=wrong_trust, description="old", max_session=43200,
                                                      tags=[{"Key": "owner", "Value": "x"}])}, {"RoleName": IR})
    s.iam.add_response("update_assume_role_policy", {},
                       {"RoleName": IR, "PolicyDocument": JsonDoc(expected_instance_trust())})
    s.iam.add_response("update_role", {},
                       {"RoleName": IR, "Description": instance_role.DESCRIPTION, "MaxSessionDuration": 3600})
    s.iam.add_response("tag_role", {}, {"RoleName": IR, "Tags": [PROJECT_TAG]})
    no_such_entity(s.iam, "get_role_policy", {"RoleName": IR, "PolicyName": IR_POLICY})
    s.iam.add_response("list_role_policies", {"PolicyNames": []}, {"RoleName": IR})
    s.iam.add_response("put_role_policy", {},
                       {"RoleName": IR, "PolicyName": IR_POLICY, "PolicyDocument": JsonDoc(expected_instance_policy())})
    s.iam.add_response("list_attached_role_policies", {"AttachedPolicies": []}, {"RoleName": IR})
    s.iam.add_response("attach_role_policy", {}, {"RoleName": IR, "PolicyArn": SSM})
    s.iam.add_response("get_instance_profile", instance_profile_body(roles=(), tags=()), {"InstanceProfileName": IR})
    s.iam.add_response("tag_instance_profile", {}, {"InstanceProfileName": IR, "Tags": [PROJECT_TAG]})
    s.iam.add_response("add_role_to_instance_profile", {}, {"InstanceProfileName": IR, "RoleName": IR})
    result, _ = run(s, instance_role, [], capsys)
    assert result == 0


def test_instance_role_dry_run_on_drift_only_reads(capsys):
    s = StubbedSession()
    sts(s)
    bedrock_reads(s)
    s.iam.add_response("get_role", {"Role": role_body(IR, trust={"Statement": []}, description="old", tags=[])},
                       {"RoleName": IR})
    s.iam.add_response("get_role_policy",
                       {"RoleName": IR, "PolicyName": IR_POLICY, "PolicyDocument": urlencoded({"Statement": []})},
                       {"RoleName": IR, "PolicyName": IR_POLICY})
    s.iam.add_response("list_role_policies", {"PolicyNames": [IR_POLICY]}, {"RoleName": IR})
    s.iam.add_response("list_attached_role_policies", {"AttachedPolicies": []}, {"RoleName": IR})
    s.iam.add_response("get_instance_profile", instance_profile_body(roles=(), tags=()), {"InstanceProfileName": IR})
    result, out = run(s, instance_role, ["--dry-run"], capsys)
    assert result == 0
    assert_read_only(s)
    for action in ("update the trust policy", "update the description", "tag role", "put inline policy",
                   "attach managed policy", "tag instance profile", "add role"):
        assert f"[dry-run] would {action}" in out, action


def test_instance_role_leaves_out_missing_profiles_with_a_warning(capsys):
    s = StubbedSession()
    sts(s)
    bedrock_reads(s, deepseek=False, haiku=False)
    role_missing(s, IR)
    no_such_entity(s.iam, "get_instance_profile", {"InstanceProfileName": IR})
    _, out = run(s, instance_role, ["--dry-run"], capsys)
    assert out.count("[WARN] application inference profile") == 2
    assert s.op_names("bedrock") == ["ListInferenceProfiles", "GetInferenceProfile"]


def test_instance_role_refuses_a_non_eu_haiku_route_before_any_iam_call(capsys):
    s = StubbedSession()
    sts(s)
    bedrock_reads(s, models=HAIKU_MODELS + [
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"])
    for stub in s.stubs.values():
        stub.activate()
    with pytest.raises(SystemExit, match="Cannot build the instance policy"):
        instance_role.main([], session=s)
    assert s.op_names("iam") == []


def test_instance_profile_holding_another_role_stops_without_changes(capsys):
    s = StubbedSession()
    sts(s)
    bedrock_reads(s)
    role_up_to_date(s, IR, expected_instance_trust(), instance_role.DESCRIPTION, IR_POLICY, expected_instance_policy())
    s.iam.add_response("list_attached_role_policies",
                       {"AttachedPolicies": [{"PolicyName": "AmazonSSMManagedInstanceCore", "PolicyArn": SSM}]},
                       {"RoleName": IR})
    s.iam.add_response("get_instance_profile", instance_profile_body(roles=("Wayakit-EC2-Secrets-Role",)),
                       {"InstanceProfileName": IR})
    for stub in s.stubs.values():
        stub.activate()
    with pytest.raises(SystemExit, match="holds role"):
        instance_role.main([], session=s)
    assert_read_only(s)
    assert_old_roles_untouched(s.ops)


@pytest.mark.parametrize("op", ["get_role", "get_instance_profile"])
def test_errors_other_than_no_such_entity_are_raised(op, capsys):
    s = StubbedSession()
    sts(s)
    bedrock_reads(s)
    if op == "get_role":
        s.iam.add_client_error("get_role", "AccessDenied", http_status_code=403)
    else:
        role_missing(s, IR)
        s.iam.add_client_error("get_instance_profile", "AccessDenied", http_status_code=403)
    for stub in s.stubs.values():
        stub.activate()
    with pytest.raises(ClientError, match="AccessDenied"):
        instance_role.main(["--dry-run"], session=s)
    assert_read_only(s)


# --- scheduler_role.main -----------------------------------------------------------------------
SR = "price-intelligence-scheduler"
SR_POLICY = "price-intelligence-scheduler-access"


def test_scheduler_role_dry_run_only_reads(capsys):
    s = StubbedSession()
    sts(s)
    ec2_lookup(s)
    role_missing(s, SR)
    result, out = run(s, scheduler_role, ["--dry-run"], capsys)
    assert result == 0
    assert s.op_names() == ["GetCallerIdentity", "DescribeInstances", "GetRole"]
    assert f"[dry-run] would create role {SR}" in out and "i-<instance>" in out


def test_scheduler_role_apply_creates_exactly_the_role(capsys):
    s = StubbedSession()
    sts(s)
    ec2_lookup(s, pages=((), (INSTANCE,)))
    role_missing(s, SR)
    role_created(s, SR, expected_scheduler_trust(), scheduler_role.DESCRIPTION, SR_POLICY,
                 expected_scheduler_policy())
    result, _ = run(s, scheduler_role, [], capsys)
    assert result == 0
    assert s.op_names("iam") == ["GetRole", "CreateRole", "PutRolePolicy"]


def test_scheduler_role_up_to_date_makes_no_write(capsys):
    s = StubbedSession()
    sts(s)
    ec2_lookup(s)
    role_up_to_date(s, SR, expected_scheduler_trust(), scheduler_role.DESCRIPTION, SR_POLICY,
                    expected_scheduler_policy())
    run(s, scheduler_role, [], capsys)
    assert_read_only(s)


def test_scheduler_role_follows_a_replaced_instance(capsys):
    s = StubbedSession()
    sts(s)
    ec2_lookup(s, pages=((OTHER_INSTANCE,),))
    role_up_to_date(s, SR, expected_scheduler_trust(), scheduler_role.DESCRIPTION, SR_POLICY,
                    expected_scheduler_policy())
    s.iam.add_response("put_role_policy", {}, {"RoleName": SR, "PolicyName": SR_POLICY,
                                               "PolicyDocument": JsonDoc(expected_scheduler_policy(OTHER_INSTANCE))})
    run(s, scheduler_role, [], capsys)


@pytest.mark.parametrize("module", [scheduler_role, deploy_role])
@pytest.mark.parametrize("pages", [((),), ((INSTANCE, OTHER_INSTANCE),), ((INSTANCE,), (OTHER_INSTANCE,))])
def test_zero_or_several_instances_stop_before_iam(module, pages, capsys):
    s = StubbedSession()
    sts(s)
    ec2_lookup(s, pages=pages)
    for stub in s.stubs.values():
        stub.activate()
    with pytest.raises(SystemExit, match="exactly one live instance"):
        module.main([], session=s)
    assert s.op_names("iam") == []


def test_malformed_instance_id_from_ec2_stops(capsys):
    s = StubbedSession()
    sts(s)
    ec2_lookup(s, pages=(("i-NOTANID",),))
    for stub in s.stubs.values():
        stub.activate()
    with pytest.raises(SystemExit, match="unexpected instance ID"):
        scheduler_role.main(["--dry-run"], session=s)
    assert s.op_names("iam") == []


# --- deploy_role.main --------------------------------------------------------------------------
DR = "price-intelligence-deploy"
DR_POLICY = "price-intelligence-deploy-access"


def oidc_exists(s, *, audiences=("sts.amazonaws.com",), tags=(PROJECT_TAG,)):
    s.iam.add_response("get_open_id_connect_provider",
                       {"Url": "token.actions.githubusercontent.com", "ClientIDList": list(audiences),
                        "ThumbprintList": ["0" * 40], "CreateDate": WHEN, "Tags": list(tags)},
                       {"OpenIDConnectProviderArn": OIDC_ARN})


def test_deploy_role_dry_run_on_an_empty_account_only_reads(capsys):
    s = StubbedSession()
    sts(s)
    ec2_lookup(s)
    no_such_entity(s.iam, "get_open_id_connect_provider", {"OpenIDConnectProviderArn": OIDC_ARN})
    role_missing(s, DR)
    result, out = run(s, deploy_role, ["--dry-run"], capsys)
    assert result == 0
    assert s.op_names() == ["GetCallerIdentity", "DescribeInstances", "GetOpenIDConnectProvider", "GetRole"]
    assert "[dry-run] would create OIDC provider https://token.actions.githubusercontent.com" in out
    assert f"[dry-run] would create role {DR}" in out


def test_deploy_role_apply_on_an_empty_account(capsys):
    s = StubbedSession()
    sts(s)
    ec2_lookup(s)
    no_such_entity(s.iam, "get_open_id_connect_provider", {"OpenIDConnectProviderArn": OIDC_ARN})
    s.iam.add_response("create_open_id_connect_provider", {"OpenIDConnectProviderArn": OIDC_ARN},
                       {"Url": "https://token.actions.githubusercontent.com", "ClientIDList": ["sts.amazonaws.com"],
                        "Tags": [PROJECT_TAG]})  # no ThumbprintList: IAM trusts GitHub's CA
    role_missing(s, DR)
    role_created(s, DR, expected_deploy_trust(), deploy_role.DESCRIPTION, DR_POLICY, expected_deploy_policy())
    result, _ = run(s, deploy_role, [], capsys)
    assert result == 0
    assert s.op_names("iam") == ["GetOpenIDConnectProvider", "CreateOpenIDConnectProvider", "GetRole",
                                 "CreateRole", "PutRolePolicy"]


def test_deploy_role_up_to_date_makes_no_write(capsys):
    s = StubbedSession()
    sts(s)
    ec2_lookup(s)
    oidc_exists(s)
    role_up_to_date(s, DR, expected_deploy_trust(), deploy_role.DESCRIPTION, DR_POLICY, expected_deploy_policy())
    run(s, deploy_role, [], capsys)
    assert_read_only(s)


def test_deploy_role_replaces_a_wider_trust(capsys):
    """A trust that allowed any branch (StringLike refs/heads/*) is narrowed back to main."""
    s = StubbedSession()
    sts(s)
    ec2_lookup(s)
    oidc_exists(s)
    wide = expected_deploy_trust()
    wide["Statement"][0]["Condition"] = {
        "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
        "StringLike": {"token.actions.githubusercontent.com:sub":
                       "repo:Wayakit-Biotechnology-in-Hygiene/wayakit-price-prediction:*"}}
    s.iam.add_response("get_role", {"Role": role_body(DR, trust=wide, description=deploy_role.DESCRIPTION)},
                       {"RoleName": DR})
    s.iam.add_response("update_assume_role_policy", {},
                       {"RoleName": DR, "PolicyDocument": JsonDoc(expected_deploy_trust())})
    s.iam.add_response("get_role_policy",
                       {"RoleName": DR, "PolicyName": DR_POLICY,
                        "PolicyDocument": urlencoded(expected_deploy_policy())},
                       {"RoleName": DR, "PolicyName": DR_POLICY})
    s.iam.add_response("list_role_policies", {"PolicyNames": [DR_POLICY]}, {"RoleName": DR})
    run(s, deploy_role, [], capsys)


def test_oidc_provider_existing_gets_audience_and_tag(capsys):
    s = StubbedSession()
    sts(s)
    ec2_lookup(s)
    oidc_exists(s, audiences=("other-audience",), tags=())
    s.iam.add_response("add_client_id_to_open_id_connect_provider", {},
                       {"OpenIDConnectProviderArn": OIDC_ARN, "ClientID": "sts.amazonaws.com"})
    s.iam.add_response("tag_open_id_connect_provider", {}, {"OpenIDConnectProviderArn": OIDC_ARN,
                                                            "Tags": [PROJECT_TAG]})
    role_up_to_date(s, DR, expected_deploy_trust(), deploy_role.DESCRIPTION, DR_POLICY, expected_deploy_policy())
    run(s, deploy_role, [], capsys)
    assert "UpdateOpenIDConnectProviderThumbprint" not in s.op_names()
    assert "DeleteOpenIDConnectProvider" not in s.op_names()


def test_oidc_provider_of_another_project_is_not_retagged(capsys):
    s = StubbedSession()
    sts(s)
    ec2_lookup(s)
    oidc_exists(s, tags=({"Key": "project", "Value": "voz-del-cliente"},))
    role_up_to_date(s, DR, expected_deploy_trust(), deploy_role.DESCRIPTION, DR_POLICY, expected_deploy_policy())
    _, out = run(s, deploy_role, [], capsys)
    assert_read_only(s)
    assert "[WARN] the OIDC provider is tagged project=voz-del-cliente" in out


# --- --audit-old-role (read-only) --------------------------------------------------------------
def event(record):
    return {"EventId": record["eventID"], "EventName": record["eventName"], "CloudTrailEvent": json.dumps(record)}


def lookup(s, key, value, records, *, times=None, pages=1):
    start, end = times or (ANY, ANY)
    chunks = [records[i::pages] for i in range(pages)]
    for i, chunk in enumerate(chunks):
        params = {"LookupAttributes": [{"AttributeKey": key, "AttributeValue": value}],
                  "StartTime": start, "EndTime": end, "MaxResults": 50}
        if i:
            params["NextToken"] = f"c{i}"
        response = {"Events": [event(r) for r in chunk]}
        if i + 1 < len(chunks):
            response["NextToken"] = f"c{i + 1}"
        s.cloudtrail.add_response("lookup_events", response, params)


def audit_prelude(s, *, associated=(INSTANCE,), pi=(INSTANCE,), path="/"):
    role = role_body("Wayakit-EC2-Secrets-Role", trust=expected_instance_trust(), description=None)
    role.update(Path=path, Arn=f"arn:aws:iam::{ACCOUNT}:role{path}Wayakit-EC2-Secrets-Role")
    s.iam.add_response("get_role", {"Role": dict(role, RoleLastUsed={"LastUsedDate": WHEN, "Region": "eu-north-1"})},
                       {"RoleName": "Wayakit-EC2-Secrets-Role"})
    s.iam.add_response("list_instance_profiles_for_role",
                       {"InstanceProfiles": [{"Path": "/", "InstanceProfileName": "Wayakit-EC2-Secrets-Role",
                                              "InstanceProfileId": "AIPAEXAMPLEEXAMPLE02", "Arn": OLD_PROFILE_ARN,
                                              "CreateDate": WHEN, "Roles": []}]},
                       {"RoleName": "Wayakit-EC2-Secrets-Role"})
    assocs = [{"AssociationId": f"iip-assoc-{n}", "InstanceId": i, "State": "associated",
               "IamInstanceProfile": {"Arn": OLD_PROFILE_ARN, "Id": "AIPAEXAMPLEEXAMPLE02"}}
              for n, i in enumerate(associated)]
    assocs.append({"AssociationId": "iip-assoc-x", "InstanceId": "i-0000000000000000a", "State": "disassociated",
                   "IamInstanceProfile": {"Arn": OLD_PROFILE_ARN, "Id": "AIPAEXAMPLEEXAMPLE02"}})
    s.ec2.add_response("describe_iam_instance_profile_associations",
                       {"IamInstanceProfileAssociations": assocs[:1], "NextToken": "p1"}, {})
    s.ec2.add_response("describe_iam_instance_profile_associations",
                       {"IamInstanceProfileAssociations": assocs[1:]}, {"NextToken": "p1"})
    ec2_lookup(s, pages=(tuple(pi),))


AUDIT_OPS = ["GetCallerIdentity", "GetRole", "ListInstanceProfilesForRole", "DescribeIamInstanceProfileAssociations",
             "DescribeIamInstanceProfileAssociations", "DescribeInstances"]


def test_audit_old_role_only_pi(capsys):
    s = StubbedSession()
    sts(s)
    audit_prelude(s)
    a = assume_record(INSTANCE, eid="e1")
    lookup(s, "ResourceName", OLD_ROLE_ARN, [a, assume_record(INSTANCE, eid="e2")], pages=2)
    lookup(s, "ResourceName", "Wayakit-EC2-Secrets-Role", [a])  # same event found twice: counted once
    lookup(s, "Username", INSTANCE, [session_record("GetSecretValue", secret=PI_SECRET_ARN),
                                     session_record("GetObject", source="s3.amazonaws.com", secret=None)])
    result, out = run(s, instance_role, ["--audit-old-role"], capsys)
    assert result == 0
    assert s.op_names() == AUDIT_OPS + ["LookupEvents"] * 4
    assert_read_only(s)
    assert "AssumeRole events: 2" in out
    assert "secretsmanager:GetSecretValue: 1" in out
    assert "verdict: only the PI instance" in out
    assert "wayakit-app-server (PI instance)" in out


def test_audit_old_role_other_user_is_reported_masked(capsys):
    s = StubbedSession()
    sts(s)
    audit_prelude(s, associated=(INSTANCE, OTHER_INSTANCE))
    lookup(s, "ResourceName", OLD_ROLE_ARN, [assume_record(INSTANCE), assume_record(OTHER_INSTANCE)])
    lookup(s, "ResourceName", "Wayakit-EC2-Secrets-Role", [])
    lookup(s, "Username", INSTANCE, [])
    lookup(s, "Username", OTHER_INSTANCE, [session_record("GetSecretValue", secret="prod/other-app")])
    _, out = run(s, instance_role, ["--audit-old-role", "--dry-run"], capsys)
    assert_read_only(s)
    assert "verdict: NOT only the PI instance" in out
    assert f"i-***{OTHER_INSTANCE[-4:]} (another instance)" in out
    assert "(1 not the PI instance)" in out


def test_audit_old_role_with_a_path_uses_the_arn_from_iam(capsys):
    """A role with a path has it in its ARN: the audit must look up and match that ARN (from
    GetRole), not one rebuilt from the name, or every session of the role would be ignored."""
    path_arn = f"arn:aws:iam::{ACCOUNT}:role/service/Wayakit-EC2-Secrets-Role"
    s = StubbedSession()
    sts(s)
    audit_prelude(s, path="/service/")
    lookup(s, "ResourceName", path_arn, [assume_record(INSTANCE)])
    lookup(s, "ResourceName", "Wayakit-EC2-Secrets-Role", [])
    lookup(s, "Username", INSTANCE, [session_record("GetSecretValue", secret=PI_SECRET_ARN, issuer=path_arn),
                                     session_record("GetSecretValue", secret="prod/other-app", issuer=OLD_ROLE_ARN)])
    result, out = run(s, instance_role, ["--audit-old-role"], capsys)
    assert result == 0
    assert_read_only(s)
    assert "secretsmanager:GetSecretValue: 1" in out  # the path-less issuer is another role: not counted
    assert "prod/other-app" not in out
    assert "verdict: only the PI instance" in out


def test_audit_old_role_without_sessions_is_inconclusive(capsys):
    s = StubbedSession()
    sts(s)
    audit_prelude(s)
    lookup(s, "ResourceName", OLD_ROLE_ARN, [])
    lookup(s, "ResourceName", "Wayakit-EC2-Secrets-Role", [])
    _, out = run(s, instance_role, ["--audit-old-role"], capsys)
    assert "verdict: INCONCLUSIVE" in out
    assert "verdict: only the PI instance" not in out


def test_audit_old_role_missing_role(capsys):
    s = StubbedSession()
    sts(s)
    role_missing(s, "Wayakit-EC2-Secrets-Role")
    result, out = run(s, instance_role, ["--audit-old-role"], capsys)
    assert result == 0 and "does not exist" in out
    assert s.op_names() == ["GetCallerIdentity", "GetRole"]


def test_audit_window_is_the_last_90_days(capsys):
    now = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc)
    times = (now - dt.timedelta(days=90), now)
    s = StubbedSession()
    audit_prelude(s)
    lookup(s, "ResourceName", OLD_ROLE_ARN, [assume_record(INSTANCE)], times=times)
    lookup(s, "ResourceName", "Wayakit-EC2-Secrets-Role", [], times=times)
    lookup(s, "Username", INSTANCE, [], times=times)
    for stub in s.stubs.values():
        stub.activate()
    summary = instance_role.audit_old_role(s, ACCOUNT, now=now)
    for stub in s.stubs.values():
        stub.assert_no_pending_responses()
    assert summary["only_pi"] is True
    capsys.readouterr()


# --- Stateful fake: apply twice, dry-run on a drifted account ----------------------------------
@functools.lru_cache(maxsize=1)
def _service_models():
    """botocore service models and {lower-case name without "_": OperationName}, loaded once (slow)."""
    bc = botocore.session.get_session()
    models = {n: bc.get_service_model(n) for n in SERVICES}
    names = {n: {o.lower(): o for o in m.operation_names} for n, m in models.items()}
    return models, names


class FakeAws:
    """Stateful fake of STS, EC2, IAM, Bedrock and CloudTrail. Every request is validated against
    the botocore model and any write naming an old role fails."""

    def __init__(self, *, instances=(INSTANCE,), profiles=True):
        self.model, self.op_names = _service_models()
        self.calls = []
        self.instances = list(instances)
        self.roles = {}
        self.instance_profiles = {}
        self.oidc = {}
        self.profiles = {"price-intelligence-deepseek-v3-2": DEEPSEEK_APP,
                         "price-intelligence-haiku-4-5": HAIKU_APP} if profiles else {}

    def client(self, service, **kwargs):
        assert kwargs == {"region_name": "eu-north-1"}
        return _FakeClient(self, service)

    def call(self, service, op, params):
        model = self.model[service]
        camel = self.op_names[service][op.replace("_", "")]
        validate_parameters(params, model.operation_model(camel).input_shape)
        if not camel.startswith(READ_PREFIXES):
            assert not set(OLD_ROLES) & {params.get("RoleName"), params.get("InstanceProfileName")}, (camel, params)
        self.calls.append((service, camel, copy.deepcopy(params)))
        return getattr(self, f"_{service}_{op}")(**params)

    def writes(self, start=0):
        return [op for _, op, _ in self.calls[start:] if not op.startswith(READ_PREFIXES)]

    def state(self):
        return copy.deepcopy((self.roles, self.instance_profiles, self.oidc))

    @staticmethod
    def _nse(op):
        return ClientError({"Error": {"Code": "NoSuchEntity", "Message": "nf"}}, op)

    def _sts_get_caller_identity(self):
        return {"Account": ACCOUNT}

    def _ec2_describe_instances(self, Filters):
        assert Filters == DESCRIBE_FILTERS
        return {"Reservations": [{"Instances": [{"InstanceId": i} for i in self.instances]}]}

    def _bedrock_list_inference_profiles(self, typeEquals):
        assert typeEquals == "APPLICATION"
        return {"inferenceProfileSummaries": [profile_summary(n, a) for n, a in self.profiles.items()]}

    def _bedrock_get_inference_profile(self, inferenceProfileIdentifier):
        assert inferenceProfileIdentifier in (HAIKU_SYSTEM_ID, HAIKU_APP)
        return {"models": [{"modelArn": m} for m in HAIKU_MODELS]}

    # IAM roles
    def _iam_get_role(self, RoleName):
        if RoleName not in self.roles:
            raise self._nse("GetRole")
        r = self.roles[RoleName]
        return {"Role": {"RoleName": RoleName, "AssumeRolePolicyDocument": json.loads(r["trust"]),
                         "Description": r["description"], "MaxSessionDuration": r["max"], "Tags": list(r["tags"])}}

    def _iam_create_role(self, RoleName, AssumeRolePolicyDocument, Description, MaxSessionDuration, Tags):
        assert RoleName not in self.roles, "duplicate create"
        self.roles[RoleName] = {"trust": AssumeRolePolicyDocument, "description": Description,
                                "max": MaxSessionDuration, "tags": list(Tags), "inline": {}, "attached": set()}
        return {"Role": {"RoleName": RoleName}}

    def _iam_update_assume_role_policy(self, RoleName, PolicyDocument):
        self.roles[RoleName]["trust"] = PolicyDocument
        return {}

    def _iam_update_role(self, RoleName, Description, MaxSessionDuration):
        self.roles[RoleName].update(description=Description, max=MaxSessionDuration)
        return {}

    def _iam_tag_role(self, RoleName, Tags):
        merged = _aws.tags_to_dict(self.roles[RoleName]["tags"])
        merged.update(_aws.tags_to_dict(Tags))
        self.roles[RoleName]["tags"] = _aws.to_tag_list(merged)
        return {}

    def _iam_get_role_policy(self, RoleName, PolicyName):
        inline = self.roles[RoleName]["inline"]
        if PolicyName not in inline:
            raise self._nse("GetRolePolicy")
        return {"RoleName": RoleName, "PolicyName": PolicyName, "PolicyDocument": json.loads(inline[PolicyName])}

    def _iam_list_role_policies(self, RoleName):
        return {"PolicyNames": sorted(self.roles[RoleName]["inline"])}

    def _iam_put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.roles[RoleName]["inline"][PolicyName] = PolicyDocument
        return {}

    def _iam_list_attached_role_policies(self, RoleName):
        return {"AttachedPolicies": [{"PolicyArn": a} for a in sorted(self.roles[RoleName]["attached"])]}

    def _iam_attach_role_policy(self, RoleName, PolicyArn):
        self.roles[RoleName]["attached"].add(PolicyArn)
        return {}

    # Instance profiles
    def _iam_get_instance_profile(self, InstanceProfileName):
        if InstanceProfileName not in self.instance_profiles:
            raise self._nse("GetInstanceProfile")
        p = self.instance_profiles[InstanceProfileName]
        return {"InstanceProfile": {"Roles": [{"RoleName": r} for r in p["roles"]], "Tags": list(p["tags"])}}

    def _iam_create_instance_profile(self, InstanceProfileName, Tags):
        assert InstanceProfileName not in self.instance_profiles, "duplicate create"
        self.instance_profiles[InstanceProfileName] = {"roles": [], "tags": list(Tags)}
        return {"InstanceProfile": {}}

    def _iam_tag_instance_profile(self, InstanceProfileName, Tags):
        self.instance_profiles[InstanceProfileName]["tags"] += list(Tags)
        return {}

    def _iam_add_role_to_instance_profile(self, InstanceProfileName, RoleName):
        p = self.instance_profiles[InstanceProfileName]
        assert not p["roles"], "an instance profile holds one role"
        p["roles"].append(RoleName)
        return {}

    # OIDC
    def _iam_get_open_id_connect_provider(self, OpenIDConnectProviderArn):
        if OpenIDConnectProviderArn not in self.oidc:
            raise self._nse("GetOpenIDConnectProvider")
        return copy.deepcopy(self.oidc[OpenIDConnectProviderArn])

    def _iam_create_open_id_connect_provider(self, Url, ClientIDList, Tags):
        assert OIDC_ARN not in self.oidc, "duplicate create"
        self.oidc[OIDC_ARN] = {"Url": Url.removeprefix("https://"), "ClientIDList": list(ClientIDList),
                               "Tags": list(Tags)}
        return {"OpenIDConnectProviderArn": OIDC_ARN}

    def _iam_add_client_id_to_open_id_connect_provider(self, OpenIDConnectProviderArn, ClientID):
        self.oidc[OpenIDConnectProviderArn]["ClientIDList"].append(ClientID)
        return {}

    def _iam_tag_open_id_connect_provider(self, OpenIDConnectProviderArn, Tags):
        self.oidc[OpenIDConnectProviderArn]["Tags"] += list(Tags)
        return {}


class _FakeClient:
    def __init__(self, aws, service):
        self._aws, self._service = aws, service

    def __getattr__(self, op):
        return lambda **params: self._aws.call(self._service, op, params)


MODULES = [instance_role, scheduler_role, deploy_role]
ROLE_OF = {instance_role: IR, scheduler_role: SR, deploy_role: DR}


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[1])
def test_apply_twice_second_run_writes_nothing(module, capsys):
    aws = FakeAws()
    assert module.main([], session=aws) == 0
    first = aws.writes()
    assert any(op.startswith("Create") for op in first)
    mark = len(aws.calls)
    assert module.main([], session=aws) == 0
    assert aws.writes(mark) == []
    out = capsys.readouterr().out
    assert ACCOUNT not in out and INSTANCE not in out
    role = aws.roles[ROLE_OF[module]]
    assert {"Key": "project", "Value": "price-intelligence"} in role["tags"]


def test_apply_leaves_the_expected_state(capsys):
    aws = FakeAws()
    for module in MODULES:
        module.main([], session=aws)
    capsys.readouterr()
    assert set(aws.roles) == {IR, SR, DR}
    assert json.loads(aws.roles[IR]["trust"]) == expected_instance_trust()
    assert json.loads(aws.roles[IR]["inline"][IR_POLICY]) == expected_instance_policy()
    assert aws.roles[IR]["attached"] == {SSM}
    assert aws.instance_profiles[IR] == {"roles": [IR], "tags": [PROJECT_TAG]}
    assert json.loads(aws.roles[SR]["trust"]) == expected_scheduler_trust()
    assert json.loads(aws.roles[SR]["inline"][SR_POLICY]) == expected_scheduler_policy()
    assert json.loads(aws.roles[DR]["trust"]) == expected_deploy_trust()
    assert json.loads(aws.roles[DR]["inline"][DR_POLICY]) == expected_deploy_policy()
    assert aws.oidc[OIDC_ARN] == {"Url": "token.actions.githubusercontent.com", "ClientIDList": ["sts.amazonaws.com"],
                                  "Tags": [PROJECT_TAG]}
    assert {name: set(role["inline"]) for name, role in aws.roles.items()} == {
        IR: {IR_POLICY}, SR: {SR_POLICY}, DR: {DR_POLICY}}
    assert aws.roles[SR]["attached"] == aws.roles[DR]["attached"] == set()
    assert all(role["max"] == 3600 and role["tags"] == [PROJECT_TAG] for role in aws.roles.values())


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[1])
@pytest.mark.parametrize("start", ["empty", "applied", "drifted"])
def test_dry_run_never_writes_and_never_changes_state(module, start, capsys):
    aws = FakeAws()
    if start != "empty":
        for m in MODULES:
            m.main([], session=aws)
    if start == "drifted":
        for role in aws.roles.values():
            role.update(trust=json.dumps({"Statement": []}), description="x", max=7200, tags=[])
            role["inline"] = {}
            role["attached"] = set()
        aws.instance_profiles[IR] = {"roles": [], "tags": []}
        aws.oidc[OIDC_ARN].update(ClientIDList=[], Tags=[])
    before, mark = aws.state(), len(aws.calls)
    assert module.main(["--dry-run"], session=aws) == 0
    assert aws.writes(mark) == []
    assert aws.state() == before
    out = capsys.readouterr().out
    if start == "applied":
        assert "[dry-run]" not in out
    else:
        assert "[dry-run] would" in out


def test_audit_never_writes_with_the_fake(capsys):
    """The audit only uses Get/List/Describe/Lookup; the fake has no CloudTrail state, so a
    write would hit a missing handler or the old-role guard."""
    class AuditFake(FakeAws):
        def _iam_get_role(self, RoleName):
            assert RoleName == "Wayakit-EC2-Secrets-Role"
            return {"Role": {"RoleName": RoleName, "Arn": OLD_ROLE_ARN}}  # real GetRole always has Arn

        def _iam_list_instance_profiles_for_role(self, RoleName):
            return {"InstanceProfiles": [{"Arn": OLD_PROFILE_ARN}]}

        def _ec2_describe_iam_instance_profile_associations(self, **kw):
            return {"IamInstanceProfileAssociations": []}

        def _cloudtrail_lookup_events(self, **kw):
            return {"Events": []}

    aws = AuditFake()
    assert instance_role.main(["--audit-old-role"], session=aws) == 0
    assert aws.writes() == []
    assert "INCONCLUSIVE" in capsys.readouterr().out


# --- Sources -----------------------------------------------------------------------------------
@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[1])
def test_sources_hold_no_account_ids_emails_or_instance_ids(module):
    with open(module.__file__, encoding="utf-8") as fh:
        text = fh.read()
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", text)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    assert not re.search(r"\bi-[0-9a-f]{8,17}\b", text)
    for name in OLD_ROLES:
        # The old roles may only be read: no write call is ever built with them.
        for line in text.splitlines():
            if name in line:
                assert not re.search(r"\b(create|put|update|delete|attach|detach|tag|add|remove)_", line), line


def test_old_role_constants():
    assert config.OLD_INSTANCE_ROLE == "Wayakit-EC2-Secrets-Role"
    assert config.OLD_SCHEDULER_ROLE == "Wayakit-EventBridge-Role"
    assert config.INSTANCE_ROLE not in OLD_ROLES and config.SCHEDULER_ROLE not in OLD_ROLES
