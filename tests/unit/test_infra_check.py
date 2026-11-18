"""Unit tests of `infra/check_pi.py` (read-only check of the PI AWS resources).

No test reaches AWS. Two kinds of double:
- `botocore.stub.Stubber` on real clients built with fake credentials in eu-north-1, behind a
  `ReadOnlyClient` wrapper. Stubber checks the exact operations, their order and parameters, and
  validates every request and response against the service models. The wrapper records every
  call and refuses (and records) any operation that is not Describe*/Get*/List*/Lookup*, so a
  write can never be hidden by `run_section` turning the error into a FAIL line.
- The pure evaluators are fed directly, including what the apply scripts (`instance_role`,
  `scheduler_role`, `deploy_role`, `bucket`, `schedule`, `alerts`) produce: check and apply must
  agree.

The fake account is `000000000000`, the instance IDs and the email are obviously fake.
"""
from __future__ import annotations

import ast
import contextlib
import copy
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import urllib.parse

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

from infra import _aws, alerts, bucket, check_pi, config, deploy_role, instance_role, schedule, scheduler_role
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.unit

ACCOUNT = "000000000000"
IID = "i-0123456789abcdef0"
IID2 = "i-0fedcba9876543210"
EMAIL = "alerts-owner@example.com"
NOW = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
REGION = config.REGION
SERVICES = ("sts", "s3", "ec2", "bedrock", "iam", "scheduler", "sns")
READ_PREFIXES = ("Describe", "Get", "List", "Lookup")

NAME = config.bucket_name(ACCOUNT)
# Every S3 read names the expected owner (the STS account): a bucket with PI's name in another
# account answers AccessDenied instead of being read (F3 follow-up D).
S3_PARAMS = {"Bucket": NAME, "ExpectedBucketOwner": ACCOUNT}
TAGS = [dict(config.PROJECT_TAG)]
BEDROCK_TAGS = [{"key": config.PROJECT_TAG_KEY, "value": config.PROJECT_TAG_VALUE}]
FOREIGN_TAGS = [{"Key": "owner", "Value": "someone-else"}]
HAIKU_REGIONS = ("eu-central-1", "eu-north-1", "eu-west-1", "eu-west-3")
HAIKU_MODELS = tuple(config.foundation_model_arn(config.HAIKU_MODEL_ID, r) for r in HAIKU_REGIONS)
DEEPSEEK_APP_ARN = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/ds0example01"
HAIKU_APP_ARN = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/hk0example01"
PROFILE_ARNS = {config.DEEPSEEK_PROFILE: DEEPSEEK_APP_ARN, config.HAIKU_PROFILE: HAIKU_APP_ARN}
DEPLOY_POLICY_ARN = f"arn:aws:iam::{ACCOUNT}:policy/price-intelligence-deploy-access"
INSTANCE_FILTERS = [{"Name": "tag:Name", "Values": [config.INSTANCE_NAME]},
                    {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
ROLE_LABEL = {r: f"role {r}" for r in (config.INSTANCE_ROLE, config.SCHEDULER_ROLE, config.DEPLOY_ROLE)}
SCHEDULE_LABEL = f"schedule {config.SCHEDULE_NAME}"
INSTANCE_LABEL = f"instance {config.INSTANCE_NAME}"
BUCKET_LABEL = f"bucket {NAME}"
TOPIC_LABEL = f"SNS topic {config.ALERTS_TOPIC}"


# --- Small helpers ---------------------------------------------------------------------------
def labels(findings, status):
    return [f.label for f in findings if f.status == status]


def fails(findings):
    return labels(findings, check_pi.FAIL)


def warns(findings):
    return labels(findings, check_pi.WARN)


def only_ok(findings):
    return all(f.status == check_pi.OK for f in findings)


def line_status(out):
    """[(status, label)] of every printed finding line."""
    found = []
    for line in out.splitlines():
        m = re.match(r"\[(OK|WARN|FAIL)\] (.+?)(?:: |$)", line)
        if m:
            found.append((m.group(1), m.group(2)))
    return found


def printed(out, status):
    """Labels printed with `status`; `<account>` is put back so they compare with the labels the
    evaluators build (the redaction itself is checked by `assert_no_leak` on the raw output)."""
    return [label.replace("<account>", ACCOUNT) for s, label in line_status(out) if s == status]


# --- Stubbed AWS -----------------------------------------------------------------------------
class Err:
    """A client error AWS answers instead of a response."""

    def __init__(self, code, status=404):
        self.code, self.status = code, status


class ReadOnlyClient:
    """Wraps a botocore client: records every API call and refuses any that is not a read."""

    def __init__(self, service, client, calls, violations):
        self._service, self._client = service, client
        self._calls, self._violations = calls, violations

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        api = self._client.meta.method_to_api_mapping.get(name)
        if api is None:
            return attr
        if not api.startswith(READ_PREFIXES):
            self._violations.append(f"{self._service}:{api}")
            raise AssertionError(f"check_pi called the mutating operation {self._service}:{api}")

        def call(**kwargs):
            self._calls.append((self._service, api, kwargs))
            return attr(**kwargs)
        return call


class FakeSession:
    def __init__(self, aws):
        self.aws = aws

    def client(self, service, **kwargs):
        assert kwargs.get("region_name") == REGION, (service, kwargs)
        self.aws.client_requests.append(service)
        return self.aws.wrapped[service]


class Aws:
    def __init__(self):
        boto = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                             region_name="us-east-1")
        self.calls, self.violations, self.client_requests = [], [], []
        self.raw = {s: boto.client(s, region_name=REGION) for s in SERVICES}
        self.stubs = {s: Stubber(c) for s, c in self.raw.items()}
        self.wrapped = {s: ReadOnlyClient(s, c, self.calls, self.violations) for s, c in self.raw.items()}

    def ok(self, service, op, response, params):
        self.stubs[service].add_response(op, copy.deepcopy(response), copy.deepcopy(params))

    def answer(self, service, op, value, params):
        if isinstance(value, Err):
            self.stubs[service].add_client_error(op, service_error_code=value.code,
                                                 http_status_code=value.status, expected_params=params)
        else:
            self.ok(service, op, value, params)

    def run(self, argv=()):
        for stub in self.stubs.values():
            stub.activate()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = check_pi.main(list(argv), session=FakeSession(self))
            for service, stub in self.stubs.items():
                stub.assert_no_pending_responses()
        finally:
            for stub in self.stubs.values():
                stub.deactivate()
        assert self.violations == []
        return rc, buf.getvalue()

    def ops(self):
        return [(s, api) for s, api, _ in self.calls]


# --- The AWS state as data ---------------------------------------------------------------------
def bucket_state_from_desired(ds):
    """`bucket.desired_state` in the shape of the get_bucket_* responses."""
    return {
        "location": {"LocationConstraint": REGION},
        "encryption": {"ServerSideEncryptionConfiguration": {"Rules": ds["encryption"]}},
        "public_access": {"PublicAccessBlockConfiguration": ds["public_access_block"]},
        "versioning": {"Status": ds["versioning"]},
        "ownership": {"OwnershipControls": {"Rules": [{"ObjectOwnership": ds["ownership"]}]}},
        "policy": {"Policy": json.dumps(ds["policy"])},
        "lifecycle": {"Rules": ds["lifecycle"]},
        "tags": {"TagSet": ds["tags"]},
    }


def instance(iid=IID, tokens="required", hop=2, tags=None):
    return {"InstanceId": iid, "InstanceType": "t3.medium", "State": {"Name": "stopped", "Code": 80},
            "MetadataOptions": {"HttpTokens": tokens, "HttpPutResponseHopLimit": hop, "HttpEndpoint": "enabled"},
            "Tags": TAGS + [{"Key": "Name", "Value": config.INSTANCE_NAME}] if tags is None else tags}


def association(profile=config.INSTANCE_PROFILE, state="associated", iid=IID):
    return {"AssociationId": "iip-assoc-0example", "InstanceId": iid, "State": state,
            "IamInstanceProfile": {"Arn": config.instance_profile_arn(ACCOUNT, profile), "Id": "AIPAEXAMPLEEXAMPLE01"}}


def profile_summary(name, arn, model_arns, ptype="APPLICATION", status="ACTIVE"):
    return {"inferenceProfileName": name, "inferenceProfileArn": arn, "inferenceProfileId": arn.rsplit("/", 1)[-1],
            "status": status, "type": ptype, "models": [{"modelArn": a} for a in model_arns]}


def deepseek_summary():
    return profile_summary(config.DEEPSEEK_PROFILE, DEEPSEEK_APP_ARN,
                           [config.foundation_model_arn(config.DEEPSEEK_MODEL_ID)])


def haiku_summary():
    return profile_summary(config.HAIKU_PROFILE, HAIKU_APP_ARN, HAIKU_MODELS)


def haiku_system():
    return profile_summary("EU Anthropic Claude Haiku 4.5",
                           config.inference_profile_arn(ACCOUNT, config.HAIKU_SYSTEM_PROFILE_ID),
                           HAIKU_MODELS, ptype="SYSTEM_DEFINED")


def role(trust, inline=None, attached=(), managed=None, tags=None):
    return {"trust": trust, "inline": dict(inline or {}), "attached": list(attached),
            "managed": dict(managed or {}), "tags": TAGS if tags is None else tags}


def good_world():
    """Everything as the apply scripts leave it, monthly schedule ENABLED (go-live state)."""
    return {
        "bucket": bucket_state_from_desired(bucket.desired_state(ACCOUNT)),
        # Two pages: describe_instances pagination is followed.
        "instance_pages": [[], [instance()]],
        "associations": [association()],
        # Two pages and a foreign application profile that must be ignored.
        "profile_pages": [[haiku_summary(),
                           profile_summary("someone-else-profile",
                                           f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/zz0example01",
                                           [config.foundation_model_arn("amazon.nova-pro-v1:0")])],
                          [deepseek_summary()]],
        "haiku_system": haiku_system(),
        "profile_tags": {config.DEEPSEEK_PROFILE: BEDROCK_TAGS, config.HAIKU_PROFILE: BEDROCK_TAGS},
        "roles": {
            config.INSTANCE_ROLE: role(instance_role.trust_policy(),
                                       {instance_role.POLICY_NAME: instance_role.instance_policy(
                                           ACCOUNT, PROFILE_ARNS, HAIKU_MODELS)},
                                       attached=[config.SSM_MANAGED_POLICY_ARN]),
            # Two inline policies on two pages; the second is a harmless Deny.
            config.SCHEDULER_ROLE: role(scheduler_role.trust_policy(ACCOUNT),
                                        {scheduler_role.POLICY_NAME: scheduler_role.scheduler_policy(ACCOUNT, IID),
                                         "deny-stop": {"Version": config.POLICY_VERSION, "Statement": [
                                             {"Effect": "Deny", "Action": "ec2:StopInstances", "Resource": "*"}]}}),
            # Permissions in a customer-managed attached policy (GetPolicy/GetPolicyVersion path).
            config.DEPLOY_ROLE: role(deploy_role.trust_policy(ACCOUNT), attached=[DEPLOY_POLICY_ARN],
                                     managed={DEPLOY_POLICY_ARN: deploy_role.deploy_policy(ACCOUNT, IID)}),
        },
        "instance_profile": {
            "Path": "/", "InstanceProfileName": config.INSTANCE_PROFILE, "InstanceProfileId": "AIPAEXAMPLEEXAMPLE01",
            "Arn": config.instance_profile_arn(ACCOUNT), "CreateDate": NOW, "Tags": TAGS,
            "Roles": [{"Path": "/", "RoleName": config.INSTANCE_ROLE, "RoleId": "AROAEXAMPLEEXAMPLE01",
                       "Arn": config.role_arn(ACCOUNT, config.INSTANCE_ROLE), "CreateDate": NOW}]},
        "old_roles": {config.OLD_INSTANCE_ROLE: True, config.OLD_SCHEDULER_ROLE: True},
        "oidc": {"Url": config.GITHUB_OIDC_HOST, "ClientIDList": [config.GITHUB_OIDC_AUDIENCE],
                 "ThumbprintList": [], "CreateDate": NOW, "Tags": TAGS},
        "group": {"Arn": config.schedule_group_arn(ACCOUNT), "Name": config.SCHEDULE_GROUP, "State": "ACTIVE"},
        "group_tags": TAGS,
        "schedule": dict(schedule.desired_schedule(ACCOUNT, IID, state="ENABLED"), Arn=config.schedule_arn(ACCOUNT)),
        "retired": {n: {"Name": n, "GroupName": config.RETIRED_SCHEDULE_GROUP, "State": "DISABLED"}
                    for n in config.RETIRED_SCHEDULES},
        "topic": dict(alerts.desired_attributes(), TopicArn=config.topic_arn(ACCOUNT), Owner=ACCOUNT),
        "topic_tags": TAGS,
        # Two pages; the confirmed one is on the second page.
        "subscription_pages": [[{"SubscriptionArn": "PendingConfirmation", "Protocol": "email",
                                 "Endpoint": "pending-person@example.com", "TopicArn": config.topic_arn(ACCOUNT),
                                 "Owner": ACCOUNT}],
                               [{"SubscriptionArn": config.topic_arn(ACCOUNT) + ":0example-0000",
                                 "Protocol": "email", "Endpoint": EMAIL, "TopicArn": config.topic_arn(ACCOUNT),
                                 "Owner": ACCOUNT}]],
    }


def missing_world():
    """Nothing PI exists yet (before the first apply)."""
    return {
        "bucket": {"location": Err("NoSuchBucket")},
        "instance_pages": [[]],
        "associations": [],
        "profile_pages": [[]],
        "haiku_system": Err("ResourceNotFoundException"),
        "profile_tags": {},
        "roles": {config.INSTANCE_ROLE: None, config.SCHEDULER_ROLE: None, config.DEPLOY_ROLE: None},
        "instance_profile": None,
        "old_roles": {config.OLD_INSTANCE_ROLE: False, config.OLD_SCHEDULER_ROLE: False},
        "oidc": None,
        "group": None,
        "group_tags": [],
        "schedule": None,
        "retired": {n: None for n in config.RETIRED_SCHEDULES},
        "topic": None,
        "topic_tags": [],
        "subscription_pages": [[]],
    }


S3_READS = (("encryption", "get_bucket_encryption"), ("public_access", "get_public_access_block"),
            ("versioning", "get_bucket_versioning"), ("ownership", "get_bucket_ownership_controls"),
            ("policy", "get_bucket_policy"), ("lifecycle", "get_bucket_lifecycle_configuration"),
            ("tags", "get_bucket_tagging"))


def _pages(aws, service, op, pages, key, base, token_in, token_out, wrap=None):
    for i, items in enumerate(pages):
        params = dict(base, **({token_in: f"tok{i}"} if i else {}))
        resp = {key: wrap(items) if wrap else list(items)}
        if i < len(pages) - 1:
            resp[token_out] = f"tok{i + 1}"
        aws.ok(service, op, resp, params)


def flat_instances(w):
    return [i for page in w["instance_pages"] for i in page]


def flat_profiles(w):
    return {p["inferenceProfileName"]: p for page in w["profile_pages"] for p in page}


def enqueue(aws, w, sections=check_pi.SECTIONS):
    """Queue the responses for `main` over `sections`, in the order check_pi must ask for them
    (the instance and Bedrock profile reads are made once and shared between sections)."""
    seen = set()
    aws.ok("sts", "get_caller_identity", {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/fake",
                                          "UserId": "AIDAEXAMPLE"}, {})

    def instances():
        if "instances" not in seen:
            seen.add("instances")
            _pages(aws, "ec2", "describe_instances", w["instance_pages"], "Reservations",
                   {"Filters": INSTANCE_FILTERS}, "NextToken", "NextToken",
                   wrap=lambda items: [{"Instances": items}] if items else [])

    def profiles():
        if "profiles" not in seen:
            seen.add("profiles")
            _pages(aws, "bedrock", "list_inference_profiles", w["profile_pages"], "inferenceProfileSummaries",
                   {"typeEquals": "APPLICATION"}, "nextToken", "nextToken")

    def haiku():
        if "haiku" not in seen:
            seen.add("haiku")
            aws.answer("bedrock", "get_inference_profile", w["haiku_system"],
                       {"inferenceProfileIdentifier": config.HAIKU_SYSTEM_PROFILE_ID})

    def read_role(name):
        r = w["roles"][name]
        if r is None:
            aws.answer("iam", "get_role", Err("NoSuchEntity"), {"RoleName": name})
            return
        aws.ok("iam", "get_role", {"Role": {
            "Path": "/", "RoleName": name, "RoleId": "AROAEXAMPLEEXAMPLE01", "Arn": config.role_arn(ACCOUNT, name),
            "CreateDate": NOW, "AssumeRolePolicyDocument": urllib.parse.quote(json.dumps(r["trust"])),
            "Tags": r["tags"]}}, {"RoleName": name})
        names = list(r["inline"])
        pages = [[n] for n in names] or [[]]
        for i, page in enumerate(pages):
            resp = {"PolicyNames": page, "IsTruncated": i < len(pages) - 1}
            if i < len(pages) - 1:
                resp["Marker"] = f"m{i + 1}"
            aws.ok("iam", "list_role_policies", resp, dict(RoleName=name, **({"Marker": f"m{i}"} if i else {})))
        for n in names:
            aws.ok("iam", "get_role_policy", {"RoleName": name, "PolicyName": n,
                                              "PolicyDocument": urllib.parse.quote(json.dumps(r["inline"][n]))},
                   {"RoleName": name, "PolicyName": n})
        aws.ok("iam", "list_attached_role_policies",
               {"AttachedPolicies": [{"PolicyName": a.rsplit("/", 1)[-1], "PolicyArn": a} for a in r["attached"]],
                "IsTruncated": False}, {"RoleName": name})
        for arn in r["attached"]:
            if arn.startswith("arn:aws:iam::aws:policy/"):
                continue
            aws.ok("iam", "get_policy", {"Policy": {"PolicyName": arn.rsplit("/", 1)[-1], "Arn": arn,
                                                    "DefaultVersionId": "v3"}}, {"PolicyArn": arn})
            aws.ok("iam", "get_policy_version", {"PolicyVersion": {
                "Document": urllib.parse.quote(json.dumps(r["managed"][arn])), "VersionId": "v3",
                "IsDefaultVersion": True}}, {"PolicyArn": arn, "VersionId": "v3"})

    for section in sections:
        if section == "bucket":
            aws.answer("s3", "get_bucket_location", w["bucket"]["location"], S3_PARAMS)
            if not isinstance(w["bucket"]["location"], Err):
                for key, op in S3_READS:
                    aws.answer("s3", op, w["bucket"][key], S3_PARAMS)
        elif section == "instance":
            instances()
            found = flat_instances(w)
            if len(found) == 1:
                aws.ok("ec2", "describe_iam_instance_profile_associations",
                       {"IamInstanceProfileAssociations": w["associations"]},
                       {"Filters": [{"Name": "instance-id", "Values": [found[0]["InstanceId"]]}]})
        elif section == "bedrock":
            profiles()
            haiku()
            existing = flat_profiles(w)
            for name in (config.DEEPSEEK_PROFILE, config.HAIKU_PROFILE):
                if name in existing:
                    aws.ok("bedrock", "list_tags_for_resource", {"tags": w["profile_tags"].get(name, [])},
                           {"resourceARN": existing[name]["inferenceProfileArn"]})
        elif section == "iam":
            instances()
            profiles()
            haiku()
            read_role(config.INSTANCE_ROLE)
            ip = w["instance_profile"]
            aws.answer("iam", "get_instance_profile", Err("NoSuchEntity") if ip is None else {"InstanceProfile": ip},
                       {"InstanceProfileName": config.INSTANCE_PROFILE})
            read_role(config.SCHEDULER_ROLE)
            read_role(config.DEPLOY_ROLE)
            for name in (config.OLD_INSTANCE_ROLE, config.OLD_SCHEDULER_ROLE):
                aws.answer("iam", "get_role", {"Role": {
                    "Path": "/", "RoleName": name, "RoleId": "AROAEXAMPLEEXAMPLE02",
                    "Arn": config.role_arn(ACCOUNT, name), "CreateDate": NOW,
                    "AssumeRolePolicyDocument": "{}"}} if w["old_roles"][name] else Err("NoSuchEntity"),
                    {"RoleName": name})
        elif section == "oidc":
            aws.answer("iam", "get_open_id_connect_provider", w["oidc"] or Err("NoSuchEntity"),
                       {"OpenIDConnectProviderArn": config.oidc_provider_arn(ACCOUNT)})
        elif section == "schedule":
            group = w["group"]
            aws.answer("scheduler", "get_schedule_group", group or Err("ResourceNotFoundException"),
                       {"Name": config.SCHEDULE_GROUP})
            if group:
                aws.ok("scheduler", "list_tags_for_resource", {"Tags": w["group_tags"]}, {"ResourceArn": group["Arn"]})
                aws.answer("scheduler", "get_schedule", w["schedule"] or Err("ResourceNotFoundException"),
                           {"Name": config.SCHEDULE_NAME, "GroupName": config.SCHEDULE_GROUP})
            instances()
            for name in config.RETIRED_SCHEDULES:
                aws.answer("scheduler", "get_schedule", w["retired"][name] or Err("ResourceNotFoundException"),
                           {"Name": name, "GroupName": config.RETIRED_SCHEDULE_GROUP})
        elif section == "alerts":
            arn = config.topic_arn(ACCOUNT)
            aws.answer("sns", "get_topic_attributes",
                       Err("NotFound") if w["topic"] is None else {"Attributes": w["topic"]}, {"TopicArn": arn})
            if w["topic"] is not None:
                aws.ok("sns", "list_tags_for_resource", {"Tags": w["topic_tags"]}, {"ResourceArn": arn})
                _pages(aws, "sns", "list_subscriptions_by_topic", w["subscription_pages"], "Subscriptions",
                       {"TopicArn": arn}, "NextToken", "NextToken")
        else:  # pragma: no cover
            raise AssertionError(section)


def run_world(w, argv=(), sections=None):
    aws = Aws()
    enqueue(aws, w, sections or (_only(argv) or check_pi.SECTIONS))
    rc, out = aws.run(argv)
    return aws, rc, out


def _only(argv):
    argv = list(argv)
    if "--only" not in argv:
        return None
    return [a for a in argv[argv.index("--only") + 1:] if not a.startswith("--")]


def assert_no_leak(out, *instance_ids):
    assert ACCOUNT not in out
    for iid in instance_ids or (IID, IID2):
        assert iid not in out
    assert EMAIL not in out and "alerts-owner@" not in out
    assert "pending-person@" not in out


GOOD_OPS = [
    ("sts", "GetCallerIdentity"),
    ("s3", "GetBucketLocation"), ("s3", "GetBucketEncryption"), ("s3", "GetPublicAccessBlock"),
    ("s3", "GetBucketVersioning"), ("s3", "GetBucketOwnershipControls"), ("s3", "GetBucketPolicy"),
    ("s3", "GetBucketLifecycleConfiguration"), ("s3", "GetBucketTagging"),
    ("ec2", "DescribeInstances"), ("ec2", "DescribeInstances"), ("ec2", "DescribeIamInstanceProfileAssociations"),
    ("bedrock", "ListInferenceProfiles"), ("bedrock", "ListInferenceProfiles"), ("bedrock", "GetInferenceProfile"),
    ("bedrock", "ListTagsForResource"), ("bedrock", "ListTagsForResource"),
    ("iam", "GetRole"), ("iam", "ListRolePolicies"), ("iam", "GetRolePolicy"), ("iam", "ListAttachedRolePolicies"),
    ("iam", "GetInstanceProfile"),
    ("iam", "GetRole"), ("iam", "ListRolePolicies"), ("iam", "ListRolePolicies"), ("iam", "GetRolePolicy"),
    ("iam", "GetRolePolicy"), ("iam", "ListAttachedRolePolicies"),
    ("iam", "GetRole"), ("iam", "ListRolePolicies"), ("iam", "ListAttachedRolePolicies"), ("iam", "GetPolicy"),
    ("iam", "GetPolicyVersion"),
    ("iam", "GetRole"), ("iam", "GetRole"),
    ("iam", "GetOpenIDConnectProvider"),
    ("scheduler", "GetScheduleGroup"), ("scheduler", "ListTagsForResource"), ("scheduler", "GetSchedule"),
    ("scheduler", "GetSchedule"), ("scheduler", "GetSchedule"),
    ("sns", "GetTopicAttributes"), ("sns", "ListTagsForResource"), ("sns", "ListSubscriptionsByTopic"),
    ("sns", "ListSubscriptionsByTopic"),
]


# === main() with Stubber: read-only, exact calls, exit code, no leaks ===========================
def test_everything_as_applied_is_all_ok_with_exact_read_calls():
    aws, rc, out = run_world(good_world())
    assert rc == 0
    assert aws.ops() == GOOD_OPS
    assert all(api.startswith(READ_PREFIXES) for _, api in aws.ops())
    assert printed(out, "FAIL") == [] and printed(out, "WARN") == []
    assert out.rstrip().splitlines()[-1] == f"{len(line_status(out))} OK, 0 WARN, 0 FAIL"
    # Every section reported something, and the key resources are named.
    for label in (BUCKET_LABEL, f"{INSTANCE_LABEL} IMDSv2", f"{INSTANCE_LABEL} instance profile",
                  f"Bedrock profile {config.HAIKU_PROFILE}", f"Bedrock profile {config.DEEPSEEK_PROFILE}",
                  *ROLE_LABEL.values(), f"instance profile {config.INSTANCE_PROFILE}",
                  f"OIDC provider {config.GITHUB_OIDC_HOST}", f"schedule group {config.SCHEDULE_GROUP}",
                  SCHEDULE_LABEL, f"{SCHEDULE_LABEL} state", TOPIC_LABEL, f"{TOPIC_LABEL} email subscriptions"):
        assert label in printed(out, "OK"), label
    assert_no_leak(out)
    # The profile ARNs are printed (they go to the instance env file), with the account redacted.
    assert DEEPSEEK_APP_ARN.replace(ACCOUNT, "<account>") in out
    # The foreign application profile is ignored.
    assert "someone-else-profile" not in out
    assert set(aws.client_requests) == set(SERVICES)


def test_every_s3_read_names_the_expected_bucket_owner():
    """All 8 bucket reads pass ExpectedBucketOwner = the account from STS (F3 follow-up D)."""
    aws, rc, _ = run_world(good_world())
    s3 = [(api, kwargs) for service, api, kwargs in aws.calls if service == "s3"]
    assert len(s3) == 1 + len(S3_READS) == 8, s3
    assert all(kwargs == S3_PARAMS for _, kwargs in s3), s3


def test_bucket_of_another_account_is_one_fail_line_and_nothing_else_is_read():
    """S3 answers AccessDenied when the bucket's owner is not ExpectedBucketOwner."""
    aws = Aws()
    aws.ok("sts", "get_caller_identity", {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/fake",
                                          "UserId": "AIDAEXAMPLE"}, {})
    aws.answer("s3", "get_bucket_location", Err("AccessDenied", 403), S3_PARAMS)
    rc, out = aws.run(["--only", "bucket"])
    assert rc == 1
    assert printed(out, "FAIL") == ["bucket"]
    assert "bucket: AWS error AccessDenied" in out
    assert [api for s, api, _ in aws.calls if s == "s3"] == ["GetBucketLocation"]
    assert_no_leak(out)


def test_dry_run_makes_exactly_the_same_read_calls_and_output():
    aws_a, rc_a, out_a = run_world(good_world())
    aws_b, rc_b, out_b = run_world(good_world(), ["--dry-run"])
    assert (rc_a, out_a) == (rc_b, out_b)
    assert aws_a.ops() == aws_b.ops() == GOOD_OPS


def test_second_run_is_identical_and_never_writes():
    """Running the check twice changes nothing: same reads, same report (no create on rerun)."""
    first = run_world(good_world())
    second = run_world(good_world())
    assert first[0].ops() == second[0].ops()
    assert first[1:] == second[1:]


def test_nothing_exists_yet_fails_with_only_reads_and_stops_reading_early():
    aws, rc, out = run_world(missing_world())
    assert rc == 1
    assert aws.ops() == [
        ("sts", "GetCallerIdentity"),
        ("s3", "GetBucketLocation"),  # NoSuchBucket: no further bucket reads
        ("ec2", "DescribeInstances"),  # 0 instances: no association read
        ("bedrock", "ListInferenceProfiles"), ("bedrock", "GetInferenceProfile"),  # no tag reads
        ("iam", "GetRole"), ("iam", "GetInstanceProfile"), ("iam", "GetRole"), ("iam", "GetRole"),
        ("iam", "GetRole"), ("iam", "GetRole"),
        ("iam", "GetOpenIDConnectProvider"),
        ("scheduler", "GetScheduleGroup"),  # no group: monthly schedule not read
        ("scheduler", "GetSchedule"), ("scheduler", "GetSchedule"),
        ("sns", "GetTopicAttributes"),  # NotFound: nothing else
    ]
    fail_labels = printed(out, "FAIL")
    for label in (BUCKET_LABEL, INSTANCE_LABEL, f"Bedrock system profile {config.HAIKU_SYSTEM_PROFILE_ID}",
                  f"Bedrock profile {config.HAIKU_PROFILE}", f"instance profile {config.INSTANCE_PROFILE}",
                  f"OIDC provider {config.GITHUB_OIDC_HOST}", f"schedule group {config.SCHEDULE_GROUP}",
                  SCHEDULE_LABEL, TOPIC_LABEL):
        assert label in fail_labels, label
    for name in (config.INSTANCE_ROLE, config.SCHEDULER_ROLE, config.DEPLOY_ROLE):
        assert f"role {name}" in fail_labels, name  # the three PI roles: not found, each one named
    assert "role" not in fail_labels  # no unnamed "role" label
    # Missing DeepSeek profile and missing old roles are WARN; deleted retired schedules are OK.
    assert f"Bedrock profile {config.DEEPSEEK_PROFILE}" in printed(out, "WARN")
    assert f"old role {config.OLD_INSTANCE_ROLE}" in printed(out, "WARN")
    for name in config.RETIRED_SCHEDULES:
        assert f"retired schedule default/{name}" in printed(out, "OK")
    assert "AWS error" not in out  # absent resources are findings, not errors
    assert_no_leak(out)


def test_bucket_settings_never_configured_are_fails_not_errors():
    w = good_world()
    w["bucket"] = {"location": {"LocationConstraint": REGION},
                   "encryption": Err("ServerSideEncryptionConfigurationNotFoundError"),
                   "public_access": Err("NoSuchPublicAccessBlockConfiguration"),
                   "versioning": {},
                   "ownership": Err("OwnershipControlsNotFoundError"),
                   "policy": Err("NoSuchBucketPolicy"),
                   "lifecycle": Err("NoSuchLifecycleConfiguration"),
                   "tags": Err("NoSuchTagSet")}
    aws, rc, out = run_world(w, ["--only", "bucket"])
    assert rc == 1
    assert "AWS error" not in out
    assert set(printed(out, "FAIL")) == {
        f"{BUCKET_LABEL} encryption", f"{BUCKET_LABEL} Block Public Access", f"{BUCKET_LABEL} versioning",
        f"{BUCKET_LABEL} object ownership", "bucket policy", "bucket lifecycle", f"{BUCKET_LABEL} tag"}
    assert printed(out, "WARN") == [f"{BUCKET_LABEL} bucket key"]
    assert printed(out, "OK") == [BUCKET_LABEL]


def test_aws_errors_become_one_fail_per_section_and_the_rest_still_runs():
    aws = Aws()
    aws.ok("sts", "get_caller_identity", {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/fake",
                                          "UserId": "AIDAEXAMPLE"}, {})
    denied = Err("AccessDenied", 403)
    aws.answer("s3", "get_bucket_location", denied, S3_PARAMS)
    aws.answer("ec2", "describe_instances", Err("UnauthorizedOperation", 403), {"Filters": INSTANCE_FILTERS})
    aws.answer("bedrock", "list_inference_profiles", Err("AccessDeniedException", 403), {"typeEquals": "APPLICATION"})
    # iam section reads the instance first (not cached after an error): it fails again there.
    aws.answer("ec2", "describe_instances", Err("UnauthorizedOperation", 403), {"Filters": INSTANCE_FILTERS})
    aws.answer("iam", "get_open_id_connect_provider", denied,
               {"OpenIDConnectProviderArn": config.oidc_provider_arn(ACCOUNT)})
    aws.answer("scheduler", "get_schedule_group", Err("AccessDeniedException", 403), {"Name": config.SCHEDULE_GROUP})
    aws.answer("sns", "get_topic_attributes", Err("AuthorizationError", 403), {"TopicArn": config.topic_arn(ACCOUNT)})
    rc, out = aws.run([])
    assert rc == 1
    assert line_status(out) == [("FAIL", s) for s in check_pi.SECTIONS]
    assert "[FAIL] bucket: AWS error AccessDenied" in out
    assert "[FAIL] instance: AWS error UnauthorizedOperation" in out
    assert "[FAIL] iam: AWS error UnauthorizedOperation" in out
    assert "[FAIL] alerts: AWS error AuthorizationError" in out
    assert out.rstrip().splitlines()[-1] == "0 OK, 0 WARN, 7 FAIL"
    assert all(api.startswith(READ_PREFIXES) for _, api in aws.ops())
    assert_no_leak(out)


def test_sts_failure_never_reaches_a_zero_exit():
    aws = Aws()
    aws.answer("sts", "get_caller_identity", Err("ExpiredToken", 403), {})
    with pytest.raises(ClientError):
        aws.run([])
    assert aws.ops() == [("sts", "GetCallerIdentity")]


def test_malformed_account_from_sts_stops_before_any_other_read():
    aws = Aws()
    aws.ok("sts", "get_caller_identity", {"Account": "not-an-account", "Arn": "arn:aws:iam::x:user/y",
                                          "UserId": "AIDAEXAMPLE"}, {})
    with pytest.raises(SystemExit) as exc:
        aws.run([])
    assert exc.value.code not in (0, None)
    assert aws.ops() == [("sts", "GetCallerIdentity")]


def test_only_schedule_reads_the_instance_but_prints_only_schedule_lines():
    aws, rc, out = run_world(good_world(), ["--only", "schedule"])
    assert rc == 0
    assert aws.ops() == [("sts", "GetCallerIdentity"), ("scheduler", "GetScheduleGroup"),
                         ("scheduler", "ListTagsForResource"), ("scheduler", "GetSchedule"),
                         ("ec2", "DescribeInstances"), ("ec2", "DescribeInstances"),
                         ("scheduler", "GetSchedule"), ("scheduler", "GetSchedule")]
    assert {label.split(" ")[0] for _, label in line_status(out)} <= {"schedule", "retired"}
    assert f"{SCHEDULE_LABEL} input" in printed(out, "OK")
    assert_no_leak(out)


def test_only_iam_reads_instance_and_bedrock_but_prints_no_bedrock_or_instance_lines():
    aws, rc, out = run_world(good_world(), ["--only", "iam"])
    assert rc == 0
    services = [s for s, _ in aws.ops()]
    assert services.count("bedrock") == 3 and services.count("ec2") == 2
    assert ("bedrock", "ListTagsForResource") not in aws.ops()
    assert ("ec2", "DescribeIamInstanceProfileAssociations") not in aws.ops()
    assert not [label for _, label in line_status(out) if label.startswith(("Bedrock", "instance "))
                and not label.startswith("instance profile")]
    assert_no_leak(out)


def test_only_with_an_unknown_section_is_a_usage_error_before_any_call():
    aws = Aws()
    with pytest.raises(SystemExit) as exc:
        aws.run(["--only", "ssm"])
    assert exc.value.code == 2
    assert aws.calls == []


def test_warnings_alone_never_change_the_exit_code():
    w = good_world()
    w["schedule"]["State"] = "DISABLED"  # created disabled until the F2 deploy
    w["profile_pages"] = [[haiku_summary()]]  # DeepSeek profile missing: plain model ID fallback
    w["roles"][config.INSTANCE_ROLE]["inline"] = {instance_role.POLICY_NAME: instance_role.instance_policy(
        ACCOUNT, {config.HAIKU_PROFILE: HAIKU_APP_ARN}, HAIKU_MODELS)}
    w["subscription_pages"] = [w["subscription_pages"][0]]  # only a pending subscription
    w["old_roles"][config.OLD_SCHEDULER_ROLE] = False  # rollback target gone
    w["bucket"]["encryption"]["ServerSideEncryptionConfiguration"]["Rules"][0]["BucketKeyEnabled"] = False
    w["associations"] = [association(state="associating")]
    aws, rc, out = run_world(w)
    assert rc == 0
    assert printed(out, "FAIL") == []
    assert sorted(printed(out, "WARN")) == sorted([
        f"{SCHEDULE_LABEL} state", f"Bedrock profile {config.DEEPSEEK_PROFILE}",
        f"{TOPIC_LABEL} email subscriptions", f"old role {config.OLD_SCHEDULER_ROLE}",
        f"{BUCKET_LABEL} bucket key", f"{INSTANCE_LABEL} instance profile"])
    assert out.rstrip().endswith(" 6 WARN, 0 FAIL")
    assert_no_leak(out)


def _mut_versioning(w):
    w["bucket"]["versioning"] = {"Status": "Suspended"}


def _mut_no_policy(w):
    w["bucket"]["policy"] = Err("NoSuchBucketPolicy")


def _mut_lifecycle_data(w):
    w["bucket"]["lifecycle"]["Rules"].append({"ID": "x", "Status": "Enabled", "Filter": {"Prefix": "data/"},
                                              "Expiration": {"Days": 30}})


def _mut_imdsv1(w):
    w["instance_pages"] = [[instance(tokens="optional")]]


def _mut_hop(w):
    w["instance_pages"] = [[instance(hop=1)]]


def _mut_old_profile(w):
    w["associations"] = [association(config.OLD_INSTANCE_ROLE)]


def _mut_two_instances(w):
    w["instance_pages"] = [[instance()], [instance(IID2)]]


def _mut_s3_star(w):
    w["roles"][config.INSTANCE_ROLE]["inline"][instance_role.POLICY_NAME]["Statement"].append(
        {"Effect": "Allow", "Action": "s3:*", "Resource": "*"})


def _mut_stringlike_sub(w):
    w["roles"][config.DEPLOY_ROLE]["trust"]["Statement"][0]["Condition"] = {
        "StringEquals": {f"{config.GITHUB_OIDC_HOST}:aud": config.GITHUB_OIDC_AUDIENCE},
        "StringLike": {f"{config.GITHUB_OIDC_HOST}:sub": f"repo:{config.GITHUB_ORG}/{config.GITHUB_REPO}:*"}}


def _mut_scheduler_no_source_account(w):
    del w["roles"][config.SCHEDULER_ROLE]["trust"]["Statement"][0]["Condition"]


def _mut_haiku_app_missing(w):
    w["profile_pages"] = [[deepseek_summary()]]


def _mut_cron(w):
    w["schedule"]["ScheduleExpression"] = "cron(50 21 ? * 6L *)"


def _mut_timezone(w):
    w["schedule"]["ScheduleExpressionTimezone"] = "UTC"


def _mut_retired_enabled(w):
    w["retired"][config.RETIRED_SCHEDULES[0]]["State"] = "ENABLED"


def _mut_no_subscription(w):
    w["subscription_pages"] = [[]]


def _mut_topic_missing(w):
    w["topic"] = None


def _mut_deploy_role_missing(w):
    w["roles"][config.DEPLOY_ROLE] = None


PLANTED = {
    "versioning suspended": (_mut_versioning, {f"{BUCKET_LABEL} versioning"}),
    "no bucket policy": (_mut_no_policy, {"bucket policy"}),
    "lifecycle expires data/": (_mut_lifecycle_data, {"bucket lifecycle kept prefixes"}),
    "IMDSv1 allowed": (_mut_imdsv1, {f"{INSTANCE_LABEL} IMDSv2"}),
    "hop limit 1": (_mut_hop, {f"{INSTANCE_LABEL} IMDS hop limit"}),
    "old instance profile": (_mut_old_profile, {f"{INSTANCE_LABEL} instance profile"}),
    "two instances": (_mut_two_instances, {INSTANCE_LABEL}),
    "s3:* on *": (_mut_s3_star, {f"{ROLE_LABEL[config.INSTANCE_ROLE]} least privilege",
                                 f"{ROLE_LABEL[config.INSTANCE_ROLE]} wildcards",
                                 f"{ROLE_LABEL[config.INSTANCE_ROLE]} actions"}),
    "StringLike sub": (_mut_stringlike_sub, {f"{ROLE_LABEL[config.DEPLOY_ROLE]} trust"}),
    "scheduler trust without SourceAccount": (_mut_scheduler_no_source_account,
                                              {f"{ROLE_LABEL[config.SCHEDULER_ROLE]} trust"}),
    "Haiku application profile missing": (_mut_haiku_app_missing, {f"Bedrock profile {config.HAIKU_PROFILE}"}),
    "wrong cron": (_mut_cron, {SCHEDULE_LABEL}),
    "wrong time zone": (_mut_timezone, {SCHEDULE_LABEL}),
    "retired schedule enabled": (_mut_retired_enabled, {f"retired schedule default/{config.RETIRED_SCHEDULES[0]}",
                                                        "instance starts"}),
    "no email subscription": (_mut_no_subscription, {f"{TOPIC_LABEL} email subscriptions"}),
    "topic missing": (_mut_topic_missing, {TOPIC_LABEL}),
    "deploy role missing": (_mut_deploy_role_missing, {ROLE_LABEL[config.DEPLOY_ROLE]}),
}


@pytest.mark.parametrize("case", sorted(PLANTED))
def test_each_planted_problem_fails_exactly_where_expected(case):
    mutate, expected = PLANTED[case]
    w = good_world()
    mutate(w)
    aws, rc, out = run_world(w)
    assert rc == 1
    assert set(printed(out, "FAIL")) == expected
    assert all(api.startswith(READ_PREFIXES) for _, api in aws.ops())
    assert_no_leak(out)


def test_a_missing_role_is_named_in_the_output():
    """Regression: a missing role used to print '[FAIL] role: not found' without the role name."""
    w = good_world()
    _mut_deploy_role_missing(w)
    _, _, out = run_world(w, ["--only", "iam"])
    assert f"[FAIL] role {config.DEPLOY_ROLE}: not found" in out.splitlines()


def test_evaluate_role_missing_without_name_keeps_the_generic_label():
    exp = check_pi.deploy_role_expectation(ACCOUNT, IID)
    assert [(f.status, f.label) for f in check_pi.evaluate_role(None, exp, [])] == [("FAIL", "role")]
    named = check_pi.evaluate_role(None, exp, [], name=config.SCHEDULER_ROLE)
    assert [(f.status, f.label) for f in named] == [("FAIL", ROLE_LABEL[config.SCHEDULER_ROLE])]


def test_unexpected_errors_are_redacted_fail_lines(monkeypatch):
    def boom(ctx):
        raise RuntimeError(f"broke on {ctx.account} {ctx.instance_id()}")
    monkeypatch.setitem(check_pi.SECTION_FUNCS, "bucket", boom)
    w = good_world()
    aws = Aws()
    enqueue(aws, w, ["instance"])
    rc, out = aws.run(["--only", "instance", "bucket"])
    assert rc == 1
    assert "[FAIL] bucket: unexpected RuntimeError: broke on <account> <instance-id>" in out
    assert_no_leak(out)


def test_botocore_errors_become_a_fail_line_with_the_class_only():
    class Ctx:
        account = ACCOUNT

        def client(self, service):
            raise EndpointConnectionError(endpoint_url=f"https://{ACCOUNT}.example.invalid")

    findings = check_pi.run_section("alerts", Ctx())
    assert [f.line() for f in findings] == ["[FAIL] alerts: AWS client error EndpointConnectionError"]


def test_exit_code_is_has_failures():
    for findings, rc in (([check_pi.Finding("OK", "a")], 0), ([check_pi.Finding("WARN", "a")], 0),
                         ([check_pi.Finding("OK", "a"), check_pi.Finding("FAIL", "b")], 1), ([], 0)):
        assert int(check_pi.has_failures(findings)) == rc


# --- Read-only, independent of the paths exercised ---------------------------------------------
def test_read_only_guard_is_not_vacuous():
    aws = Aws()
    with pytest.raises(AssertionError):
        aws.wrapped["s3"].put_bucket_policy
    with pytest.raises(AssertionError):
        aws.wrapped["scheduler"].update_schedule
    assert aws.violations == ["s3:PutBucketPolicy", "scheduler:UpdateSchedule"]


def test_source_only_names_read_operations():
    """Every boto3 method name in check_pi.py (called or passed to `_read`) is a read, so no
    path, tested or not, can write."""
    boto = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name=REGION)
    mapping = {}
    for service in (*SERVICES, "ssm", "cloudwatch", "cloudtrail", "secretsmanager", "logs"):
        mapping.update(boto.client(service, region_name=REGION).meta.method_to_api_mapping)
    tree = ast.parse((REPO_ROOT / "infra" / "check_pi.py").read_text())
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used = {name: mapping[name] for name in names if name in mapping}
    assert {"get_bucket_location", "describe_instances", "get_role", "get_schedule",
            "list_subscriptions_by_topic", "get_inference_profile"} <= set(used)
    assert {n: api for n, api in used.items() if not api.startswith(READ_PREFIXES)} == {}
    mutating = re.compile(r"^(put|create|delete|update|tag|untag|associate|disassociate|replace|start|stop|"
                          r"terminate|modify|attach|detach|send|publish|subscribe|invoke|run|set|add|remove)_")
    assert sorted(n for n in names if mutating.match(n)) == ["add_argument"]  # argparse, not boto3


def test_import_has_no_side_effects_and_help_works_without_aws():
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
               AWS_EC2_METADATA_DISABLED="true")
    probe = subprocess.run([sys.executable, "-c", "import sys, infra.check_pi; "
                            "print('boto3' in sys.modules, 'botocore' in sys.modules)"],
                           cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.split() == ["False", "False"]
    helped = subprocess.run([sys.executable, "-m", "infra.check_pi", "--help"], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
    assert helped.returncode == 0, helped.stderr
    assert "--only" in helped.stdout and "--dry-run" in helped.stdout


# === Pure evaluators ===========================================================================
# --- Results ---------------------------------------------------------------------------------
def test_finding_line_summary_and_redact():
    assert check_pi.Finding("OK", "bucket x", "fine").line() == "[OK] bucket x: fine"
    assert check_pi.Finding("FAIL", "bucket x").line() == "[FAIL] bucket x"
    fs = [check_pi.Finding(s, "x") for s in ("OK", "OK", "WARN", "FAIL")]
    assert check_pi.summary(fs) == "2 OK, 1 WARN, 1 FAIL"
    text = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{IID}"
    assert check_pi.redact(text, ACCOUNT, IID) == f"arn:aws:ec2:{REGION}:<account>:instance/<instance-id>"
    # F3 review: without arguments the catch-all still masks any account or instance ID (it used
    # to return the text unchanged).
    assert check_pi.redact(text) == f"arn:aws:ec2:{REGION}:<account>:instance/<instance-id>"
    assert check_pi.redact("no ids here: 12345678901 1234567890123 i-abc") == \
        "no ids here: 12345678901 1234567890123 i-abc"


@pytest.mark.parametrize("text,expected", [
    # Another account (e.g. in a trust policy or CloudTrail) after the known one is replaced.
    (f"arn:aws:iam::{ACCOUNT}:role/a arn:aws:iam::123456789012:role/b",
     "arn:aws:iam::<account>:role/a arn:aws:iam::<account>:role/b"),
    # Another instance after the known one, also glued to other characters.
    (f"{IID} {IID2} x{IID2}y", "<instance-id> <instance-id> x<instance-id>y"),
    ("i-0abc1234.eu-north-1.compute.internal", "<instance-id>.eu-north-1.compute.internal"),
    # Not IDs: 11 or 13 digits, 12 digits glued to letters, `i-` with fewer than 8 hex.
    ("11111111111 2222222222222 a333333333333 i-0abc123", "11111111111 2222222222222 a333333333333 i-0abc123"),
])
def test_redact_catch_all_masks_leftover_accounts_and_instance_ids(text, expected):
    out = check_pi.redact(text, ACCOUNT, IID)
    assert out == expected
    assert check_pi.redact(out, ACCOUNT, IID) == out


# --- Policy helpers --------------------------------------------------------------------------
def test_as_document_accepts_dict_json_and_url_encoded_json():
    doc = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]}
    assert check_pi.as_document(doc) is doc
    assert check_pi.as_document(json.dumps(doc)) == doc
    assert check_pi.as_document(urllib.parse.quote(json.dumps(doc))) == doc
    assert check_pi.as_document(None) is None
    single = {"Statement": {"Effect": "Allow", "Action": "a:b", "Resource": "r"}}
    assert check_pi.statements(single) == [single["Statement"]]


@pytest.mark.parametrize("pattern,value,expected", [
    ("arn:aws:s3:::b/*", "arn:aws:s3:::b/data/x/y", True),
    ("arn:aws:s3:::*", "arn:aws:s3:::other/key", True),
    ("arn:aws:*:*:*:secret:x-*", f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:x-AbCdEf", True),
    ("a?c", "abc", True), ("a?c", "ac", False), ("a?c", "abbc", False),
    ("a.c", "abc", False),  # '.' is literal
    ("a+(b)", "a+(b)", True), ("a+(b)", "aa(b)", False),
    ("arn:aws:s3:::b", "arn:aws:s3:::b/x", False),
    ("ARN:aws", "arn:aws", False),
])
def test_wildcard_match(pattern, value, expected):
    assert check_pi.wildcard_match(pattern, value) is expected


def test_allows_action_case_insensitive_resource_case_sensitive():
    stmts = [{"Effect": "Allow", "Action": "S3:GetObject", "Resource": "arn:aws:s3:::b/*"}]
    assert check_pi.allows(stmts, "s3:getobject", "arn:aws:s3:::b/k")
    assert not check_pi.allows(stmts, "s3:GetObject", "arn:aws:s3:::B/k")


def test_allows_deny_without_condition_wins_conditional_deny_is_ignored():
    allow = {"Effect": "Allow", "Action": "s3:*", "Resource": "*"}
    deny = {"Effect": "Deny", "Action": "s3:DeleteObject", "Resource": "arn:aws:s3:::b/*"}
    assert check_pi.allows([allow], "s3:DeleteObject", "arn:aws:s3:::b/k")
    assert not check_pi.allows([allow, deny], "s3:DeleteObject", "arn:aws:s3:::b/k")
    cond = dict(deny, Condition={"Bool": {"aws:SecureTransport": "false"}})
    assert check_pi.allows([allow, cond], "s3:DeleteObject", "arn:aws:s3:::b/k")


def test_allows_not_action_and_not_resource():
    na = {"Effect": "Allow", "NotAction": "iam:*", "Resource": "arn:aws:s3:::b"}
    assert check_pi.allows([na], "s3:ListBucket", "arn:aws:s3:::b")
    assert not check_pi.allows([na], "iam:PassRole", "arn:aws:s3:::b")
    nr = {"Effect": "Allow", "Action": "s3:GetObject", "NotResource": "arn:aws:s3:::secret/*"}
    assert check_pi.allows([nr], "s3:GetObject", "arn:aws:s3:::other/k")
    assert not check_pi.allows([nr], "s3:GetObject", "arn:aws:s3:::secret/k")


@pytest.mark.parametrize("stmt,flagged", [
    ({"Effect": "Allow", "Action": "*", "Resource": "arn:aws:s3:::b"}, True),
    ({"Effect": "Allow", "Action": "s3:Get*", "Resource": "arn:aws:s3:::b/*"}, True),
    ({"Effect": "Allow", "Action": "s3:GetObjec?", "Resource": "arn:aws:s3:::b/*"}, True),
    ({"Effect": "Allow", "Action": "sns:Publish", "Resource": "*"}, True),
    ({"Effect": "Allow", "Action": ["cloudwatch:PutMetricData", "sns:Publish"], "Resource": "*"}, True),
    ({"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*"}, False),
    ({"Effect": "Allow", "NotAction": "iam:*", "Resource": "arn:aws:s3:::b"}, True),
    ({"Effect": "Allow", "Action": "s3:GetObject", "NotResource": "arn:aws:s3:::b/*"}, True),
    ({"Effect": "Deny", "Action": "*", "Resource": "*"}, False),
    ({"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/*"}, False),
])
def test_broad_grants(stmt, flagged):
    assert bool(check_pi.broad_grants([stmt])) is flagged


def test_condition_values_normalizes_bools_and_key_case():
    stmt = {"Condition": {"Bool": {"AWS:SecureTransport": False},
                          "StringEquals": {"aws:SourceAccount": ACCOUNT}}}
    assert check_pi.condition_values(stmt, "aws:securetransport") == {"Bool": ["false"]}
    assert check_pi.condition_values(stmt, "aws:SourceAccount") == {"StringEquals": [ACCOUNT]}
    assert check_pi.condition_values({}, "x") == {}


# --- Roles: agreement with the apply scripts -------------------------------------------------
def instance_state(policy=None, attached=(config.SSM_MANAGED_POLICY_ARN,), tags=TAGS, trust=None):
    policy = policy if policy is not None else instance_role.instance_policy(ACCOUNT, PROFILE_ARNS, HAIKU_MODELS)
    return check_pi.RoleState(config.INSTANCE_ROLE, trust or instance_role.trust_policy(), tags, list(attached),
                              {instance_role.POLICY_NAME: policy})


def eval_instance(state):
    exp = check_pi.instance_role_expectation(ACCOUNT, tuple(PROFILE_ARNS.values()), HAIKU_MODELS)
    return check_pi.evaluate_role(state, exp, check_pi.evaluate_service_trust(
        f"role {state.name}", state.trust, "ec2.amazonaws.com"))


def scheduler_state(policy=None, trust=None):
    return check_pi.RoleState(config.SCHEDULER_ROLE, trust or scheduler_role.trust_policy(ACCOUNT), TAGS, [],
                              {"p": policy or scheduler_role.scheduler_policy(ACCOUNT, IID)})


def eval_scheduler(state, iid=IID):
    return check_pi.evaluate_role(state, check_pi.scheduler_role_expectation(ACCOUNT, iid),
                                  check_pi.evaluate_service_trust(f"role {state.name}", state.trust,
                                                                  "scheduler.amazonaws.com", ACCOUNT))


def deploy_state(policy=None, trust=None):
    return check_pi.RoleState(config.DEPLOY_ROLE, trust or deploy_role.trust_policy(ACCOUNT), TAGS, [],
                              {"p": policy or deploy_role.deploy_policy(ACCOUNT, IID)})


def eval_deploy(state, iid=IID):
    return check_pi.evaluate_role(state, check_pi.deploy_role_expectation(ACCOUNT, iid),
                                  check_pi.evaluate_deploy_trust(state.trust, ACCOUNT))


def test_roles_built_by_the_apply_scripts_are_all_ok():
    assert only_ok(eval_instance(instance_state()))
    assert only_ok(eval_scheduler(scheduler_state()))
    assert only_ok(eval_deploy(deploy_state()))


def test_instance_role_with_only_the_haiku_profile_is_all_ok():
    """DeepSeek profile missing (documented fallback): the policy without it still passes."""
    policy = instance_role.instance_policy(ACCOUNT, {config.HAIKU_PROFILE: HAIKU_APP_ARN}, HAIKU_MODELS)
    exp = check_pi.instance_role_expectation(ACCOUNT, (HAIKU_APP_ARN,), HAIKU_MODELS)
    assert only_ok(check_pi.evaluate_role(instance_state(policy), exp, []))


def test_instance_role_probes_skipped_without_profiles_is_a_warn():
    exp = check_pi.instance_role_expectation(ACCOUNT)
    out = check_pi.evaluate_permissions("role x", [], exp)
    assert "role x probes skipped" in warns(out)


def _add(policy, *stmts):
    policy = copy.deepcopy(policy)
    policy["Statement"].extend(stmts)
    return policy


def _instance_policy():
    return instance_role.instance_policy(ACCOUNT, PROFILE_ARNS, HAIKU_MODELS)


def _replace_resource(policy, sid, resource):
    policy = copy.deepcopy(policy)
    for s in policy["Statement"]:
        if s.get("Sid") == sid:
            s["Resource"] = resource
    return policy


I = ROLE_LABEL[config.INSTANCE_ROLE]
S = ROLE_LABEL[config.SCHEDULER_ROLE]
D = ROLE_LABEL[config.DEPLOY_ROLE]
BUCKET_ARN = config.bucket_arn(ACCOUNT)
INSTANCE_ARN = config.instance_arn(ACCOUNT, IID)


@pytest.mark.parametrize("policy,expected_fails", [
    # "*" also grants PutMetricData without the namespace condition.
    pytest.param(_add(_instance_policy(), {"Effect": "Allow", "Action": "*", "Resource": BUCKET_ARN}),
                 {f"{I} wildcards", f"{I} condition on cloudwatch:PutMetricData", f"{I} actions"},
                 id="star-action"),
    pytest.param(_add(_instance_policy(), {"Effect": "Allow", "Action": "s3:*", "Resource": "*"}),
                 {f"{I} wildcards", f"{I} least privilege", f"{I} actions"}, id="s3-star-on-star"),
    pytest.param(_add(_instance_policy(), {"Effect": "Allow", "Action": "sns:Publish", "Resource": "*"}),
                 {f"{I} wildcards", f"{I} least privilege"}, id="publish-on-star"),
    pytest.param(_replace_resource(_instance_policy(), "ReadPiSecret",
                                   f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:prod/*"),
                 {f"{I} least privilege"}, id="other-secret"),
    pytest.param(_replace_resource(_instance_policy(), "ReadPiSecret",
                                   f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:prod/wayakit-app-*"),
                 {f"{I} least privilege"}, id="secret-name-prefix-star"),
    pytest.param(_replace_resource(_instance_policy(), "ListPiBucket", "arn:aws:s3:::*"),
                 {f"{I} least privilege"}, id="other-bucket-list"),
    pytest.param(_replace_resource(_instance_policy(), "ReadWritePiObjects", "arn:aws:s3:::*/*"),
                 {f"{I} least privilege"}, id="other-bucket-objects"),
    pytest.param(_replace_resource(_instance_policy(), "PublishPiAlerts", f"arn:aws:sns:{REGION}:{ACCOUNT}:*"),
                 {f"{I} least privilege"}, id="other-topic"),
    pytest.param(_replace_resource(_instance_policy(), "InvokeDeepSeek", f"arn:aws:bedrock:{REGION}::foundation-model/*"),
                 {f"{I} least privilege"}, id="other-model"),
    pytest.param(_add(_instance_policy(), {"Effect": "Allow", "Action": "iam:PassRole",
                                           "Resource": config.role_arn(ACCOUNT, config.INSTANCE_ROLE)}),
                 {f"{I} least privilege", f"{I} actions"}, id="pass-role"),
    pytest.param(_add(_instance_policy(), {"Effect": "Allow", "NotAction": "iam:*", "Resource": BUCKET_ARN}),
                 {f"{I} wildcards", f"{I} condition on cloudwatch:PutMetricData"}, id="not-action"),
    pytest.param(_add(_instance_policy(), {"Effect": "Allow", "NotAction": "iam:*", "Resource": "*"}),
                 {f"{I} wildcards", f"{I} least privilege", f"{I} condition on cloudwatch:PutMetricData"},
                 id="not-action-on-star"),
    pytest.param(_add(_instance_policy(), {"Effect": "Deny", "Action": "sns:Publish",
                                           "Resource": config.topic_arn(ACCOUNT)}),
                 {f"{I} required permissions"}, id="publish-denied"),
    # F3 review (Important 2): the instance only reads releases/; only the deploy role writes them.
    pytest.param(_replace_resource(_instance_policy(), "ReadWritePiObjects", f"{BUCKET_ARN}/*"),
                 {f"{I} least privilege"}, id="pre-review-whole-bucket-read-write"),
    pytest.param(_add(_instance_policy(), {"Effect": "Allow", "Action": "s3:PutObject",
                                           "Resource": f"{BUCKET_ARN}/{config.RELEASES_PREFIX}*"}),
                 {f"{I} least privilege"}, id="put-on-releases"),
    pytest.param(_add(_instance_policy(), {"Effect": "Allow", "Action": "s3:PutObject",
                                           "Resource": f"{BUCKET_ARN}/{config.RELEASES_CURRENT_KEY}"}),
                 {f"{I} least privilege"}, id="put-on-current-pointer-only"),
    pytest.param(_add(_instance_policy(), {"Effect": "Allow", "Action": "s3:DeleteObject",
                                           "Resource": f"{BUCKET_ARN}/{config.RELEASES_PREFIX}*.tar.gz"}),
                 {f"{I} least privilege"}, id="delete-release-tarballs"),
    pytest.param(_replace_resource(_instance_policy(), "ReadPiReleases",
                                   f"arn:aws:s3:::price-intelligence-*/{config.RELEASES_PREFIX}*"),
                 {f"{I} least privilege"}, id="releases-on-lookalike-buckets"),
    pytest.param(_replace_resource(_instance_policy(), "ReadPiReleases", f"{BUCKET_ARN}/other/*"),
                 {f"{I} required permissions"}, id="releases-not-readable"),
    pytest.param(_replace_resource(_instance_policy(), "ReadWritePiObjects",
                                   [f"{BUCKET_ARN}/{p}*" for p in ("data/", "models/", "state/")]),
                 {f"{I} required permissions"}, id="logs-prefix-not-writable"),
])
def test_instance_role_least_privilege_fails(policy, expected_fails):
    assert set(fails(eval_instance(instance_state(policy)))) == expected_fails


def test_instance_expectation_writes_data_prefixes_and_only_reads_releases():
    """must_allow: Get/Put/Delete on each data prefix, only Get on releases/; must_deny: Put and
    Delete on `releases/current` and on a release tarball."""
    exp = check_pi.instance_role_expectation(ACCOUNT, tuple(PROFILE_ARNS.values()), HAIKU_MODELS)
    obj = f"{BUCKET_ARN}/"
    allowed_s3 = {(p.action, p.resource) for p in exp.must_allow if p.resource.startswith(obj)}
    data = {(a, f"{obj}{prefix}probe") for prefix in ("data/", "models/", "state/", "logs/")
            for a in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject")}
    releases = {("s3:GetObject", f"{obj}releases/current"), ("s3:GetObject", f"{obj}releases/probe.tar.gz")}
    assert allowed_s3 == data | releases
    denied = {(p.action, p.resource) for p in exp.must_deny}
    assert {(a, f"{obj}releases/{k}") for a in ("s3:PutObject", "s3:DeleteObject")
            for k in ("current", "probe.tar.gz")} <= denied
    assert check_pi.DATA_PREFIXES == ("data/", "models/", "state/", "logs/")


def test_write_on_releases_names_each_denied_probe():
    policy = _add(_instance_policy(), {"Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject"],
                                       "Resource": f"{BUCKET_ARN}/*"})
    [line] = [f.line() for f in eval_instance(instance_state(policy)) if f.status == check_pi.FAIL]
    assert line.startswith(f"[FAIL] {I} least privilege: also allows ")
    for action in ("s3:PutObject", "s3:DeleteObject"):
        for key in ("releases/current", "releases/probe.tar.gz"):
            assert f"{action} on {BUCKET_ARN}/{key}" in line


def test_bucket_name_prefix_wildcard_is_not_least_privilege():
    """Regression: the only look-alike probe was 'other-bucket', so 'price-intelligence-*' passed."""
    policy = _replace_resource(_instance_policy(), "ListPiBucket", "arn:aws:s3:::price-intelligence-*")
    assert f"{I} least privilege" in fails(eval_instance(instance_state(policy)))


def test_lookalike_buckets_share_the_pi_name_as_prefix_and_suffix():
    name = config.bucket_name(ACCOUNT)
    others = check_pi.lookalike_buckets(ACCOUNT)
    assert name not in others
    assert any(o.startswith(name) and o != name for o in others)
    assert any(o.endswith(name) and o != name for o in others)
    assert any(name not in o for o in others)


@pytest.mark.parametrize("sid,resource", [
    ("ListPiBucket", "arn:aws:s3:::price-intelligence-*"),
    ("ListPiBucket", f"arn:aws:s3:::*-{ACCOUNT}"),
    ("ListPiBucket", f"arn:aws:s3:::*{config.bucket_name(ACCOUNT)}*"),
    ("ReadWritePiObjects", "arn:aws:s3:::price-intelligence-*/*"),
    ("ReadWritePiObjects", f"arn:aws:s3:::*-{ACCOUNT}/*"),
    ("ReadWritePiObjects", f"arn:aws:s3:::{config.bucket_name(ACCOUNT)}*/*"),
])
def test_instance_bucket_name_wildcards_fail_least_privilege(sid, resource):
    policy = _replace_resource(_instance_policy(), sid, resource)
    assert fails(eval_instance(instance_state(policy))) == [f"{I} least privilege"]


@pytest.mark.parametrize("sid,resource", [
    ("PublishReleases", f"arn:aws:s3:::price-intelligence-*/{config.RELEASES_PREFIX}*"),
    ("PublishReleases", f"arn:aws:s3:::*-{ACCOUNT}/{config.RELEASES_PREFIX}*"),
    ("ReadAndMoveCurrentPointer", f"arn:aws:s3:::price-intelligence-*/{config.RELEASES_CURRENT_KEY}"),
    ("ReadAndMoveCurrentPointer", f"arn:aws:s3:::*-{ACCOUNT}/{config.RELEASES_CURRENT_KEY}"),
])
def test_deploy_bucket_name_wildcards_fail_least_privilege(sid, resource):
    policy = _replace_resource(deploy_role.deploy_policy(ACCOUNT, IID), sid, resource)
    assert fails(eval_deploy(deploy_state(policy))) == [f"{D} least privilege"]


def test_put_metric_data_must_keep_the_namespace_condition():
    policy = _instance_policy()
    for s in policy["Statement"]:
        if s.get("Sid") == "PutPiMetrics":
            del s["Condition"]
    assert fails(eval_instance(instance_state(policy))) == [f"{I} condition on cloudwatch:PutMetricData"]
    policy = _instance_policy()
    for s in policy["Statement"]:
        if s.get("Sid") == "PutPiMetrics":
            s["Condition"] = {"StringEquals": {"cloudwatch:namespace": "Other"}}
    assert fails(eval_instance(instance_state(policy))) == [f"{I} condition on cloudwatch:PutMetricData"]


def test_missing_required_grant_fails():
    policy = copy.deepcopy(_instance_policy())
    policy["Statement"] = [s for s in policy["Statement"] if s.get("Sid") != "PublishPiAlerts"]
    out = eval_instance(instance_state(policy))
    assert fails(out) == [f"{I} required permissions"]
    assert "sns:Publish" in next(f.detail for f in out if f.label == f"{I} required permissions")


def test_haiku_models_in_every_routed_region_are_required():
    policy = instance_role.instance_policy(ACCOUNT, PROFILE_ARNS, HAIKU_MODELS[:1])
    assert fails(eval_instance(instance_state(policy))) == [f"{I} required permissions"]


def test_extra_specific_action_is_reported_and_named():
    """An action outside the spec, even on a specific resource, is a FAIL (F3 follow-up D: it
    was a WARN) and the action is named."""
    policy = _add(_instance_policy(), {"Effect": "Allow", "Action": "iam:AttachRolePolicy",
                                       "Resource": config.role_arn(ACCOUNT, config.INSTANCE_ROLE)})
    out = eval_instance(instance_state(policy))
    line = next(f for f in out if f.label == f"{I} actions")
    assert line.status == check_pi.FAIL
    assert line.detail == "not in the spec: iam:attachrolepolicy"
    assert fails(out) == [f"{I} actions"]
    assert check_pi.has_failures(out)


@pytest.mark.parametrize("role", ["instance", "scheduler", "deploy"])
@pytest.mark.parametrize("where", ["inline", "managed"])
def test_extra_action_fails_on_every_role_inline_or_customer_managed(role, where):
    """The same extra action (on the role's own, specific resource) is a FAIL on each PI role,
    whether it comes from an inline or a customer-managed policy."""
    extra = {"Effect": "Allow", "Action": "ec2:RebootInstances", "Resource": INSTANCE_ARN}
    build = {"instance": (instance_state, eval_instance), "scheduler": (scheduler_state, eval_scheduler),
             "deploy": (deploy_state, eval_deploy)}[role]
    state = build[0]()
    if where == "inline":
        name, doc = next(iter(state.documents.items()))
        state.documents[name] = _add(doc, extra)
    else:  # read_role keys customer-managed documents by policy ARN and lists it as attached
        arn = f"arn:aws:iam::{ACCOUNT}:policy/pi-extra"
        state.attached.append(arn)
        state.documents[arn] = {"Version": "2012-10-17", "Statement": [extra]}
    out = build[1](state)
    label = {"instance": I, "scheduler": S, "deploy": D}[role]
    assert fails(out) == [f"{label} actions"], out
    assert "ec2:rebootinstances" in next(f for f in out if f.label == f"{label} actions").detail


def test_managed_policies_exact():
    assert fails(eval_instance(instance_state(attached=()))) == [f"{I} AWS-managed policies"]
    admin = "arn:aws:iam::aws:policy/AdministratorAccess"
    assert fails(eval_instance(instance_state(attached=(config.SSM_MANAGED_POLICY_ARN, admin)))) == [
        f"{I} AWS-managed policies"]


def test_role_without_project_tag_fails():
    assert fails(eval_instance(instance_state(tags=FOREIGN_TAGS))) == [f"{I} tag"]


def test_instance_trust_other_principal_fails():
    trust = {"Statement": [{"Effect": "Allow", "Principal": {"Service": ["ec2.amazonaws.com", "lambda.amazonaws.com"]},
                            "Action": "sts:AssumeRole"}]}
    assert fails(eval_instance(instance_state(trust=trust))) == [f"{I} trust"]
    trust = {"Statement": [{"Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"},
                            "Action": "sts:AssumeRole"}]}
    assert fails(eval_instance(instance_state(trust=trust))) == [f"{I} trust"]


@pytest.mark.parametrize("policy,expected", [
    pytest.param({"Statement": [{"Effect": "Allow", "Action": "ec2:StartInstances",
                                 "Resource": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/*"}]},
                 {f"{S} least privilege"}, id="any-instance"),
    pytest.param({"Statement": [{"Effect": "Allow", "Action": ["ec2:StartInstances", "ec2:StopInstances"],
                                 "Resource": INSTANCE_ARN}]},
                 {f"{S} least privilege", f"{S} actions"}, id="stop-too"),
    pytest.param({"Statement": [{"Effect": "Allow", "Action": "ec2:StartInstances",
                                 "Resource": config.instance_arn(ACCOUNT, IID2)}]},
                 {f"{S} required permissions"}, id="other-instance"),
    pytest.param({"Statement": [{"Effect": "Allow", "Action": "ec2:*", "Resource": INSTANCE_ARN}]},
                 {f"{S} least privilege", f"{S} wildcards", f"{S} actions"}, id="ec2-star"),
])
def test_scheduler_role_fails(policy, expected):
    assert set(fails(eval_scheduler(scheduler_state(policy)))) == expected


@pytest.mark.parametrize("condition", [
    None,
    {"StringEquals": {"aws:SourceAccount": "111111111111"}},
    {"StringLike": {"aws:SourceAccount": ACCOUNT}},
])
def test_scheduler_trust_needs_source_account(condition):
    trust = scheduler_role.trust_policy(ACCOUNT)
    if condition is None:
        del trust["Statement"][0]["Condition"]
    else:
        trust["Statement"][0]["Condition"] = condition
    assert fails(eval_scheduler(scheduler_state(trust=trust))) == [f"{S} trust"]


def test_scheduler_without_instance_skips_the_grant_probe_with_a_warn():
    out = eval_scheduler(scheduler_state(), iid=None)
    assert fails(out) == [] and warns(out) == [f"{S} probes skipped"]


@pytest.mark.parametrize("change,expected", [
    pytest.param(lambda p: _replace_resource(p, "PublishReleases", f"{BUCKET_ARN}/*"),
                 {f"{D} least privilege"}, id="put-anywhere-in-bucket"),
    pytest.param(lambda p: _replace_resource(p, "StartPiInstance", f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/*"),
                 {f"{D} least privilege"}, id="start-any-instance"),
    pytest.param(lambda p: _replace_resource(p, "ReadAndMoveCurrentPointer", f"{BUCKET_ARN}/*"),
                 {f"{D} least privilege"}, id="read-anything"),
    pytest.param(lambda p: _add(p, {"Effect": "Allow", "Action": "s3:DeleteObject",
                                    "Resource": f"{BUCKET_ARN}/{config.RELEASES_CURRENT_KEY}"}),
                 {f"{D} least privilege", f"{D} actions"}, id="delete-pointer"),
])
def test_deploy_role_least_privilege_fails(change, expected):
    assert set(fails(eval_deploy(deploy_state(change(deploy_role.deploy_policy(ACCOUNT, IID)))))) == expected


@pytest.mark.parametrize("condition", [
    None,
    {"ForAllValues:StringEquals": {"aws:TagKeys": ["pi-mode", "Name"]}},
    {"ForAnyValue:StringEquals": {"aws:TagKeys": ["pi-mode"]}},
    {"ForAllValues:StringLike": {"aws:TagKeys": ["pi-*"]}},
])
def test_deploy_create_tags_only_the_pi_mode_key(condition):
    policy = deploy_role.deploy_policy(ACCOUNT, IID)
    for s in policy["Statement"]:
        if s.get("Sid") == "SetPiModeTag":
            if condition is None:
                del s["Condition"]
            else:
                s["Condition"] = condition
    assert fails(eval_deploy(deploy_state(policy))) == [f"{D} condition on ec2:CreateTags"]


SUB = f"{config.GITHUB_OIDC_HOST}:sub"
AUD = f"{config.GITHUB_OIDC_HOST}:aud"


def _deploy_trust(condition=None, principal=None, action=None):
    trust = deploy_role.trust_policy(ACCOUNT)
    s = trust["Statement"][0]
    if condition is not None:
        s["Condition"] = condition
    if principal is not None:
        s["Principal"] = principal
    if action is not None:
        s["Action"] = action
    return trust


@pytest.mark.parametrize("trust", [
    pytest.param(_deploy_trust({"StringEquals": {AUD: config.GITHUB_OIDC_AUDIENCE},
                                "StringLike": {SUB: f"repo:{config.GITHUB_ORG}/{config.GITHUB_REPO}:*"}}),
                 id="stringlike-sub"),
    pytest.param(_deploy_trust({"StringEquals": {AUD: config.GITHUB_OIDC_AUDIENCE, SUB: config.DEPLOY_OIDC_SUBJECT},
                                "StringLike": {SUB: "repo:*"}}), id="extra-stringlike"),
    pytest.param(_deploy_trust({"StringEquals": {AUD: config.GITHUB_OIDC_AUDIENCE, SUB: config.DEPLOY_OIDC_SUBJECT.replace(
        "refs/heads/main", "refs/heads/server-improvement")}}), id="other-branch"),
    pytest.param(_deploy_trust({"StringEquals": {AUD: config.GITHUB_OIDC_AUDIENCE, SUB: [
        config.DEPLOY_OIDC_SUBJECT, f"repo:{config.GITHUB_ORG}/{config.GITHUB_REPO}:pull_request"]}}), id="two-subjects"),
    pytest.param(_deploy_trust({"StringEquals": {SUB: config.DEPLOY_OIDC_SUBJECT}}), id="no-aud"),
    pytest.param(_deploy_trust(principal={"Federated": f"arn:aws:iam::{ACCOUNT}:oidc-provider/other.example.com"}),
                 id="other-provider"),
    pytest.param(_deploy_trust(principal={"AWS": "*"}), id="any-principal"),
    pytest.param(_deploy_trust(action="sts:AssumeRole"), id="wrong-action"),
    pytest.param({"Statement": []}, id="no-statement"),
])
def test_deploy_trust_fails(trust):
    assert fails(check_pi.evaluate_deploy_trust(trust, ACCOUNT)) == [f"{D} trust"]
    assert fails(eval_deploy(deploy_state(trust=trust))) == [f"{D} trust"]


def test_deploy_trust_with_an_extra_open_statement_fails():
    trust = deploy_role.trust_policy(ACCOUNT)
    trust["Statement"].append({"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "sts:AssumeRole"})
    assert fails(check_pi.evaluate_deploy_trust(trust, ACCOUNT)) == [f"{D} trust"]


def test_evaluate_role_missing_instance_profile_and_old_roles():
    assert [f.status for f in check_pi.evaluate_role(None, check_pi.RoleExpectation(), [])] == ["FAIL"]
    assert fails(check_pi.evaluate_instance_profile(None)) == [f"instance profile {config.INSTANCE_PROFILE}"]
    prof = {"Roles": [{"RoleName": config.OLD_INSTANCE_ROLE}], "Tags": TAGS}
    assert fails(check_pi.evaluate_instance_profile(prof)) == [f"instance profile {config.INSTANCE_PROFILE}"]
    prof = {"Roles": [{"RoleName": config.INSTANCE_ROLE}], "Tags": []}
    assert fails(check_pi.evaluate_instance_profile(prof)) == [f"instance profile {config.INSTANCE_PROFILE} tag"]
    assert check_pi.evaluate_old_role("x", True).status == "OK"
    assert check_pi.evaluate_old_role("x", False).status == "WARN"


# --- Bucket ----------------------------------------------------------------------------------
def good_bucket():
    return bucket_state_from_desired(bucket.desired_state(ACCOUNT))


def test_bucket_as_applied_is_all_ok_also_as_aws_returns_it():
    assert only_ok(check_pi.evaluate_bucket(good_bucket(), ACCOUNT))
    state = good_bucket()
    # AWS may answer bucket-wide rules with an empty Filter, reorder rules and keep foreign tags.
    state["lifecycle"]["Rules"] = [dict(r, Filter={}) if r["Filter"] == {"Prefix": ""} else r
                                   for r in reversed(state["lifecycle"]["Rules"])]
    state["tags"]["TagSet"] = FOREIGN_TAGS + TAGS
    assert only_ok(check_pi.evaluate_bucket(state, ACCOUNT))


def _bucket_with(key, value):
    state = good_bucket()
    state[key] = value
    return state


@pytest.mark.parametrize("key,value,expected", [
    ("location", {"LocationConstraint": None}, BUCKET_LABEL),
    ("location", {"LocationConstraint": "eu-west-1"}, BUCKET_LABEL),
    ("encryption", {"ServerSideEncryptionConfiguration": {"Rules": [
        {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}, "BucketKeyEnabled": True}]}},
     f"{BUCKET_LABEL} encryption"),
    ("public_access", {"PublicAccessBlockConfiguration": dict(bucket.PUBLIC_ACCESS_BLOCK, RestrictPublicBuckets=False)},
     f"{BUCKET_LABEL} Block Public Access"),
    ("public_access", check_pi.Missing("NoSuchPublicAccessBlockConfiguration"), f"{BUCKET_LABEL} Block Public Access"),
    ("versioning", {}, f"{BUCKET_LABEL} versioning"),
    ("versioning", {"Status": "Suspended"}, f"{BUCKET_LABEL} versioning"),
    ("ownership", {"OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerPreferred"}]}},
     f"{BUCKET_LABEL} object ownership"),
    ("policy", check_pi.Missing("NoSuchBucketPolicy"), "bucket policy"),
    ("tags", {"TagSet": FOREIGN_TAGS}, f"{BUCKET_LABEL} tag"),
    ("tags", check_pi.Missing("NoSuchTagSet"), f"{BUCKET_LABEL} tag"),
])
def test_bucket_setting_fails(key, value, expected):
    assert fails(check_pi.evaluate_bucket(_bucket_with(key, value), ACCOUNT)) == [expected]


def test_missing_bucket_is_a_single_fail():
    out = check_pi.evaluate_bucket({"location": check_pi.Missing("NoSuchBucket")}, ACCOUNT)
    assert [(f.status, f.label) for f in out] == [("FAIL", BUCKET_LABEL)]


def _policy(*stmts):
    return json.dumps({"Version": config.POLICY_VERSION, "Statement": list(stmts)})


TLS_DENY = bucket.bucket_policy(ACCOUNT)["Statement"][0]


@pytest.mark.parametrize("policy,expected", [
    pytest.param(_policy(), {"bucket policy TLS only"}, id="empty"),
    pytest.param(_policy(dict(TLS_DENY, Resource=[BUCKET_ARN])), {"bucket policy TLS only"}, id="bucket-only"),
    pytest.param(_policy(dict(TLS_DENY, Condition={"Bool": {"aws:SecureTransport": "true"}})),
                 {"bucket policy TLS only"}, id="wrong-value"),
    pytest.param(_policy(dict(TLS_DENY, Effect="Allow")), {"bucket policy TLS only", "bucket policy public access"},
                 id="allow-instead-of-deny"),
    pytest.param(_policy(TLS_DENY, {"Sid": "Public", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
                                    "Resource": f"{BUCKET_ARN}/*"}), {"bucket policy public access"}, id="public-read"),
    pytest.param(_policy(TLS_DENY, {"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "s3:GetObject",
                                    "Resource": f"{BUCKET_ARN}/*"}), {"bucket policy public access"}, id="public-aws"),
])
def test_bucket_policy_fails(policy, expected):
    assert set(fails(check_pi.evaluate_bucket_policy(policy, ACCOUNT))) == expected


def test_public_allow_with_list_principal_is_flagged():
    """Regression: {'AWS': ['*']} (list form of {'AWS': '*'}) was not flagged as public."""
    policy = _policy(TLS_DENY, {"Effect": "Allow", "Principal": {"AWS": ["*"]}, "Action": "s3:GetObject",
                                "Resource": f"{BUCKET_ARN}/*"})
    assert fails(check_pi.evaluate_bucket_policy(policy, ACCOUNT)) == ["bucket policy public access"]


def test_public_allow_with_not_principal_is_flagged():
    """Decision by pi-dev (to confirm by the lead): any Allow with NotPrincipal counts as public."""
    policy = _policy(TLS_DENY, {"Effect": "Allow", "NotPrincipal": {"AWS": config.role_arn(ACCOUNT, config.INSTANCE_ROLE)},
                                "Action": "s3:GetObject", "Resource": f"{BUCKET_ARN}/*"})
    assert fails(check_pi.evaluate_bucket_policy(policy, ACCOUNT)) == ["bucket policy public access"]


@pytest.mark.parametrize("principal", [{"AWS": ["*"]}, {"AWS": "*"}, "*"])
def test_tls_deny_with_any_everyone_principal_form_is_ok(principal):
    """Regression: a TLS Deny written with {'AWS': ['*']} was a false FAIL 'bucket policy TLS only'."""
    policy = _policy(dict(TLS_DENY, Principal=principal))
    assert only_ok(check_pi.evaluate_bucket_policy(policy, ACCOUNT))


@pytest.mark.parametrize("principal,expected", [
    ("*", True), ({"AWS": "*"}, True), ({"AWS": ["*"]}, True),
    ({"AWS": ["arn:aws:iam::000000000000:root", "*"]}, True),
    ({"AWS": "arn:aws:iam::000000000000:root"}, False), ({"AWS": []}, False),
    ({"Service": "*"}, False), ({"Service": "s3.amazonaws.com"}, False), (None, False), ("", False),
])
def test_is_everyone(principal, expected):
    assert check_pi.is_everyone(principal) is expected


def _lifecycle(*extra, base=True):
    rules = copy.deepcopy(bucket.lifecycle_rules()) if base else []
    return rules + list(extra)


@pytest.mark.parametrize("prefix", ["data/", "models/", "state/", "releases/", "dat", "data/sub/", ""])
def test_lifecycle_expiring_kept_prefixes_fails(prefix):
    rules = _lifecycle({"ID": "x", "Status": "Enabled", "Filter": {"Prefix": prefix}, "Expiration": {"Days": 30}})
    out = check_pi.evaluate_lifecycle(rules)
    assert "bucket lifecycle kept prefixes" in fails(out)


def test_lifecycle_expiring_by_date_or_and_filter_fails():
    rules = _lifecycle({"ID": "x", "Status": "Enabled", "Filter": {"And": {"Prefix": "models/", "Tags": []}},
                        "Expiration": {"Date": NOW.isoformat()}})
    assert "bucket lifecycle kept prefixes" in fails(check_pi.evaluate_lifecycle(rules))


def test_lifecycle_disabled_rule_and_logs_rule_are_fine():
    rules = _lifecycle({"ID": "x", "Status": "Disabled", "Filter": {"Prefix": "data/"}, "Expiration": {"Days": 1}})
    assert only_ok(check_pi.evaluate_lifecycle(rules))


@pytest.mark.parametrize("rules,expected", [
    (check_pi.Missing("NoSuchLifecycleConfiguration"), {"bucket lifecycle"}),
    ([r for r in bucket.lifecycle_rules() if r["ID"] != "logs-expire"], {"bucket lifecycle logs/"}),
    ([dict(r, Expiration={"Days": 90}) if r["ID"] == "logs-expire" else r for r in bucket.lifecycle_rules()],
     {"bucket lifecycle logs/"}),
    ([dict(r, NoncurrentVersionExpiration={"NoncurrentDays": 1}) if r["ID"] == "noncurrent-versions-expire" else r
      for r in bucket.lifecycle_rules()], {"bucket lifecycle noncurrent versions"}),
    ([r for r in bucket.lifecycle_rules() if r["ID"] != "cleanup-markers-and-uploads"],
     {"bucket lifecycle incomplete multipart"}),
])
def test_lifecycle_settings_fail(rules, expected):
    assert set(fails(check_pi.evaluate_lifecycle(rules))) == expected


# --- Instance --------------------------------------------------------------------------------
@pytest.mark.parametrize("instances,assocs,expected", [
    ([], [], [INSTANCE_LABEL]),
    ([instance(), instance(IID2)], [], [INSTANCE_LABEL]),
    ([instance(tokens="optional")], [association()], [f"{INSTANCE_LABEL} IMDSv2"]),
    ([instance(hop=1)], [association()], [f"{INSTANCE_LABEL} IMDS hop limit"]),
    ([instance(hop=3)], [association()], [f"{INSTANCE_LABEL} IMDS hop limit"]),
    ([dict(instance(), MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 2,
                                         "HttpEndpoint": "disabled"})], [association()],
     [f"{INSTANCE_LABEL} IMDS endpoint"]),
    ([instance(tags=[{"Key": "Name", "Value": config.INSTANCE_NAME}])], [association()], [f"{INSTANCE_LABEL} tag"]),
    ([instance()], [association(config.OLD_INSTANCE_ROLE)], [f"{INSTANCE_LABEL} instance profile"]),
    ([instance()], [], [f"{INSTANCE_LABEL} instance profile"]),
    ([instance()], [association(), association(config.OLD_INSTANCE_ROLE)], [f"{INSTANCE_LABEL} instance profile"]),
])
def test_instance_fails(instances, assocs, expected):
    assert fails(check_pi.evaluate_instance(instances, assocs)) == expected


def test_instance_ok_and_disassociated_history_ignored():
    assocs = [association(config.OLD_INSTANCE_ROLE, state="disassociated"), association()]
    assert only_ok(check_pi.evaluate_instance([instance()], assocs))
    assert warns(check_pi.evaluate_instance([instance()], [association(state="associating")])) == [
        f"{INSTANCE_LABEL} instance profile"]


# --- Bedrock ---------------------------------------------------------------------------------
def test_bedrock_profiles():
    assert only_ok(check_pi.evaluate_bedrock_profile(config.HAIKU_PROFILE, haiku_summary(), BEDROCK_TAGS,
                                                     config.HAIKU_MODEL_ID))
    assert warns(check_pi.evaluate_bedrock_profile(config.DEEPSEEK_PROFILE, None, [], config.DEEPSEEK_MODEL_ID,
                                                   required=False)) == [f"Bedrock profile {config.DEEPSEEK_PROFILE}"]
    assert fails(check_pi.evaluate_bedrock_profile(config.HAIKU_PROFILE, None, [], config.HAIKU_MODEL_ID)) == [
        f"Bedrock profile {config.HAIKU_PROFILE}"]
    label = f"Bedrock profile {config.HAIKU_PROFILE}"
    cases = [
        (dict(haiku_summary(), status="FAILED"), BEDROCK_TAGS, [label]),
        (haiku_summary(), [], [f"{label} tag"]),
        (dict(haiku_summary(), models=[]), BEDROCK_TAGS, [f"{label} model"]),
        (dict(haiku_summary(), models=[{"modelArn": config.foundation_model_arn("amazon.nova-pro-v1:0")}]),
         BEDROCK_TAGS, [f"{label} model"]),
        (dict(haiku_summary(), models=[{"modelArn": config.foundation_model_arn("x" + config.HAIKU_MODEL_ID)}]),
         BEDROCK_TAGS, [f"{label} model"]),
    ]
    for summary, tags, expected in cases:
        assert fails(check_pi.evaluate_bedrock_profile(config.HAIKU_PROFILE, summary, tags,
                                                       config.HAIKU_MODEL_ID)) == expected
    sys_label = f"Bedrock system profile {config.HAIKU_SYSTEM_PROFILE_ID}"
    assert fails(check_pi.evaluate_haiku_system_profile(None)) == [sys_label]
    assert fails(check_pi.evaluate_haiku_system_profile(dict(haiku_system(), models=[]))) == [sys_label]
    ok = check_pi.evaluate_haiku_system_profile(haiku_system())
    assert only_ok(ok) and ok[0].detail == "routes to " + ", ".join(sorted(HAIKU_REGIONS))


# --- OIDC ------------------------------------------------------------------------------------
def test_oidc_provider():
    good = {"Url": f"https://{config.GITHUB_OIDC_HOST}", "ClientIDList": [config.GITHUB_OIDC_AUDIENCE], "Tags": TAGS}
    assert only_ok(check_pi.evaluate_oidc(good))
    label = f"OIDC provider {config.GITHUB_OIDC_HOST}"
    assert fails(check_pi.evaluate_oidc(None)) == [label]
    assert fails(check_pi.evaluate_oidc(dict(good, ClientIDList=["other"]))) == [f"{label} audience"]
    assert fails(check_pi.evaluate_oidc(dict(good, Tags=[]))) == [f"{label} tag"]


# --- Schedule --------------------------------------------------------------------------------
def desired(state="ENABLED"):
    return schedule.desired_schedule(ACCOUNT, IID, state=state)


def test_schedule_as_applied():
    assert only_ok(check_pi.evaluate_schedule(desired(), ACCOUNT, IID))
    out = check_pi.evaluate_schedule(desired("DISABLED"), ACCOUNT, IID)
    assert fails(out) == [] and warns(out) == [f"{SCHEDULE_LABEL} state"]
    out = check_pi.evaluate_schedule(desired(), ACCOUNT, None)
    assert fails(out) == [] and warns(out) == [f"{SCHEDULE_LABEL} input"]


def _sched(**changes):
    s = copy.deepcopy(desired())
    target = changes.pop("Target", None)
    s.update(changes)
    if target:
        s["Target"].update(target)
    return s


@pytest.mark.parametrize("sched,expected", [
    pytest.param(None, SCHEDULE_LABEL, id="missing"),
    pytest.param(_sched(ScheduleExpression="cron(50 21 ? * 5L *) "), SCHEDULE_LABEL, id="cron-space"),
    pytest.param(_sched(ScheduleExpression="cron(0 4 ? * FRI *)"), SCHEDULE_LABEL, id="cron-weekly"),
    pytest.param(_sched(ScheduleExpressionTimezone="America/Monterrey"), SCHEDULE_LABEL, id="timezone"),
    pytest.param(_sched(FlexibleTimeWindow={"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 15}),
                 f"{SCHEDULE_LABEL} flexible window", id="flexible"),
    pytest.param(_sched(Target={"Arn": "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"}),
                 f"{SCHEDULE_LABEL} target", id="target"),
    pytest.param(_sched(Target={"RoleArn": config.role_arn(ACCOUNT, config.OLD_SCHEDULER_ROLE)}),
                 f"{SCHEDULE_LABEL} role", id="old-role"),
    pytest.param(_sched(Target={"RetryPolicy": {"MaximumRetryAttempts": 185}}), f"{SCHEDULE_LABEL} retries",
                 id="retries"),
    pytest.param(_sched(Target={"RetryPolicy": None}), f"{SCHEDULE_LABEL} retries", id="no-retry-policy"),
    pytest.param(_sched(Target={"Input": json.dumps({"InstanceIds": [IID2]})}), f"{SCHEDULE_LABEL} input",
                 id="other-instance"),
    pytest.param(_sched(Target={"Input": json.dumps({"InstanceIds": [IID, IID2]})}), f"{SCHEDULE_LABEL} input",
                 id="two-instances"),
    pytest.param(_sched(Target={"Input": "{not json"}), f"{SCHEDULE_LABEL} input", id="bad-json"),
    pytest.param(_sched(Target={"Input": "[]"}), f"{SCHEDULE_LABEL} input", id="json-list"),
])
def test_schedule_fails(sched, expected):
    assert fails(check_pi.evaluate_schedule(sched, ACCOUNT, IID)) == [expected]


def test_schedule_mismatch_detail_shows_both_sides():
    out = check_pi.evaluate_schedule(_sched(ScheduleExpression="cron(0 4 ? * FRI *)"), ACCOUNT, IID)
    detail = out[0].detail
    assert "cron(0 4 ? * FRI *)" in detail and config.SCHEDULE_CRON in detail


def test_schedule_group():
    group = {"State": "ACTIVE"}
    assert only_ok(check_pi.evaluate_schedule_group(group, TAGS))
    label = f"schedule group {config.SCHEDULE_GROUP}"
    assert fails(check_pi.evaluate_schedule_group(None, [])) == [label]
    assert fails(check_pi.evaluate_schedule_group({"State": "DELETING"}, TAGS)) == [label]
    assert fails(check_pi.evaluate_schedule_group(group, [])) == [f"{label} tag"]


def test_retired_schedules_and_double_start():
    name = config.RETIRED_SCHEDULES[0]
    assert check_pi.evaluate_retired(name, None).status == "OK"
    assert check_pi.evaluate_retired(name, {"State": "DISABLED"}).status == "OK"
    assert check_pi.evaluate_retired(name, {"State": "ENABLED"}).status == "FAIL"
    on, off = {"State": "ENABLED"}, {"State": "DISABLED"}
    assert fails(check_pi.evaluate_start_sources(on, {name: on})) == ["instance starts"]
    assert check_pi.evaluate_start_sources(off, {name: on}) == []
    assert check_pi.evaluate_start_sources(on, {name: off, "other": None}) == []
    assert check_pi.evaluate_start_sources(None, {name: on}) == []


# --- Alerts ----------------------------------------------------------------------------------
def _sub(arn, protocol="email", endpoint=EMAIL):
    return {"SubscriptionArn": arn, "Protocol": protocol, "Endpoint": endpoint}


CONFIRMED = config.topic_arn(ACCOUNT) + ":0example-0000"


def test_topic():
    attrs = alerts.desired_attributes()
    out = check_pi.evaluate_topic(attrs, TAGS, [_sub(CONFIRMED)])
    assert only_ok(out)
    assert all(EMAIL not in f.line() for f in out)
    assert "a***@example.com" in out[-1].detail
    label = f"{TOPIC_LABEL} email subscriptions"
    assert fails(check_pi.evaluate_topic(None, [], [])) == [TOPIC_LABEL]
    assert fails(check_pi.evaluate_topic(attrs, TAGS, [])) == [label]
    assert fails(check_pi.evaluate_topic(attrs, TAGS, [_sub(CONFIRMED, "https", "https://example.com/x")])) == [label]
    out = check_pi.evaluate_topic(attrs, TAGS, [_sub("PendingConfirmation")])
    assert fails(out) == [] and warns(out) == [label]
    assert all(EMAIL not in f.line() for f in out)
    assert fails(check_pi.evaluate_topic({}, TAGS, [_sub(CONFIRMED)])) == [f"{TOPIC_LABEL} encryption"]
    assert fails(check_pi.evaluate_topic({"KmsMasterKeyId": "alias/other"}, TAGS, [_sub(CONFIRMED)])) == [
        f"{TOPIC_LABEL} encryption"]
    assert fails(check_pi.evaluate_topic(attrs, [], [_sub(CONFIRMED)])) == [f"{TOPIC_LABEL} tag"]


# --- Tags are required on every resource that has them --------------------------------------
@pytest.mark.parametrize("evaluate", [
    pytest.param(lambda tags: check_pi.evaluate_tags("x", tags), id="generic"),
    pytest.param(lambda tags: check_pi.evaluate_bedrock_profile(config.HAIKU_PROFILE, haiku_summary(), tags,
                                                                config.HAIKU_MODEL_ID), id="bedrock"),
    pytest.param(lambda tags: check_pi.evaluate_schedule_group({"State": "ACTIVE"}, tags), id="schedule-group"),
    pytest.param(lambda tags: check_pi.evaluate_topic(alerts.desired_attributes(), tags, [_sub(CONFIRMED)]),
                 id="topic"),
    pytest.param(lambda tags: check_pi.evaluate_instance([instance(tags=tags)], [association()]), id="instance"),
    pytest.param(lambda tags: check_pi.evaluate_oidc({"Url": config.GITHUB_OIDC_HOST,
                                                      "ClientIDList": [config.GITHUB_OIDC_AUDIENCE],
                                                      "Tags": tags}), id="oidc"),
])
def test_project_tag_required(evaluate):
    assert check_pi.evaluate_tags("x", None).status == "FAIL"
    out = evaluate if isinstance(evaluate, list) else evaluate(TAGS)
    assert only_ok(out if isinstance(out, list) else [out])
    for bad in ([], FOREIGN_TAGS, [{"Key": "project", "Value": "voz-del-cliente"}]):
        res = evaluate(bad)
        res = res if isinstance(res, list) else [res]
        assert len(fails(res)) == 1 and fails(res)[0].endswith(" tag")
