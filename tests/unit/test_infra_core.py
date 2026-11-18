"""Unit tests of the shared `infra/` foundation: `infra/config.py` and `infra/_aws.py`.

No test reaches AWS: STS is answered by `botocore.stub.Stubber` on a client built with fake
credentials, and sessions are fakes. The fake account is `000000000000`.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

import pi_secrets
from infra import _aws, config
from tests.conftest import REPO_ROOT, load_module

pytestmark = pytest.mark.unit

ACCOUNT = "000000000000"


def _fake_boto_session():
    return boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                         region_name="us-east-1")


class RecordingSession:
    """Fake session: records `client()` calls and hands out the clients it was given."""

    def __init__(self, clients=None):
        self.clients = clients or {}
        self.calls = []

    def client(self, service, **kwargs):
        self.calls.append((service, kwargs))
        return self.clients[service]


def _stubbed_sts(account):
    sts = _fake_boto_session().client("sts", region_name=config.REGION)
    stub = Stubber(sts)
    stub.add_response("get_caller_identity",
                      {"Account": account, "Arn": "arn:aws:iam::000000000000:user/x", "UserId": "X"}, {})
    return sts, stub


# --- config ------------------------------------------------------------------------------------
def test_names_and_constants():
    assert config.REGION == "eu-north-1"
    assert config.PROJECT_TAG == {"Key": "project", "Value": "price-intelligence"}
    for name in (config.INSTANCE_ROLE, config.INSTANCE_PROFILE, config.SCHEDULER_ROLE,
                 config.DEPLOY_ROLE, config.SCHEDULE_GROUP, config.SCHEDULE_NAME,
                 config.ALERTS_TOPIC, config.DEEPSEEK_PROFILE, config.HAIKU_PROFILE):
        assert name.startswith("price-intelligence")
    assert config.INSTANCE_ROLE == "price-intelligence-instance"
    assert config.SCHEDULER_ROLE == "price-intelligence-scheduler"
    assert config.DEPLOY_ROLE == "price-intelligence-deploy"
    assert config.SCHEDULE_NAME == "price-intelligence-monthly"
    assert config.SCHEDULE_CRON == "cron(50 21 ? * 5L *)"
    assert config.SCHEDULE_TIMEZONE == "America/Mexico_City"
    assert config.RETIRED_SCHEDULE_GROUP == "default"
    assert set(config.RETIRED_SCHEDULES) == {"Wayakit-Start-Thursday-Night", "Wayakit-Start-Friday-Night"}
    assert config.METRICS_NAMESPACE == "PriceIntelligence"
    assert config.BUCKET_PREFIXES == ("data/", "models/", "state/", "logs/", "releases/")
    assert config.RELEASES_CURRENT_KEY == "releases/current"
    assert config.DEPLOY_OIDC_SUBJECT == (
        "repo:Wayakit-Biotechnology-in-Hygiene/wayakit-price-prediction:ref:refs/heads/main")


def test_config_matches_the_application_code():
    """infra must grant access to exactly what the pipeline uses."""
    assert config.SECRET_NAME == pi_secrets.SECRET_NAME == "prod/wayakit-app"
    assert config.REGION == pi_secrets.REGION_NAME
    ai = load_module("scraper/services/ai_service.py")
    assert config.DEEPSEEK_MODEL_ID == ai.DEFAULT_PRIMARY_MODEL
    assert config.HAIKU_SYSTEM_PROFILE_ID == ai.DEFAULT_FALLBACK_MODEL
    assert config.HAIKU_SYSTEM_PROFILE_ID == f"eu.{config.HAIKU_MODEL_ID}"
    assert config.ENV_PRIMARY_MODEL == ai.PRIMARY_MODEL_ENV
    assert config.ENV_FALLBACK_MODEL == ai.FALLBACK_MODEL_ENV
    assert config.CONDA_ENV in (REPO_ROOT / "run_pipeline.sh").read_text()


def test_profile_default_and_override(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    assert config.profile() == "wayakit-vdc"
    monkeypatch.setenv("AWS_PROFILE", "other")
    assert config.profile() == "other"
    monkeypatch.setenv("AWS_PROFILE", "")
    assert config.profile() == "wayakit-vdc"


def test_arn_builders():
    assert config.bucket_name(ACCOUNT) == "price-intelligence-000000000000"
    assert config.bucket_arn(ACCOUNT) == "arn:aws:s3:::price-intelligence-000000000000"
    assert config.role_arn(ACCOUNT, "r") == "arn:aws:iam::000000000000:role/r"
    assert config.instance_profile_arn(ACCOUNT) == (
        "arn:aws:iam::000000000000:instance-profile/price-intelligence-instance")
    assert config.oidc_provider_arn(ACCOUNT) == (
        "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com")
    assert config.secret_arn_pattern(ACCOUNT) == (
        "arn:aws:secretsmanager:eu-north-1:000000000000:secret:prod/wayakit-app-??????")
    assert config.topic_arn(ACCOUNT) == "arn:aws:sns:eu-north-1:000000000000:price-intelligence-alerts"
    assert config.instance_arn(ACCOUNT, "i-0abc") == "arn:aws:ec2:eu-north-1:000000000000:instance/i-0abc"
    assert config.schedule_arn(ACCOUNT) == (
        "arn:aws:scheduler:eu-north-1:000000000000:schedule/price-intelligence/price-intelligence-monthly")
    assert config.schedule_group_arn(ACCOUNT) == (
        "arn:aws:scheduler:eu-north-1:000000000000:schedule-group/price-intelligence")
    assert config.foundation_model_arn("deepseek.v3.2") == (
        "arn:aws:bedrock:eu-north-1::foundation-model/deepseek.v3.2")
    assert config.foundation_model_arn("m", region="eu-*") == "arn:aws:bedrock:eu-*::foundation-model/m"
    assert config.bedrock_profile_sources(ACCOUNT) == {
        "price-intelligence-deepseek-v3-2": "arn:aws:bedrock:eu-north-1::foundation-model/deepseek.v3.2",
        "price-intelligence-haiku-4-5": ("arn:aws:bedrock:eu-north-1:000000000000:inference-profile/"
                                         "eu.anthropic.claude-haiku-4-5-20251001-v1:0"),
    }


def test_bucket_name_is_a_valid_s3_name():
    name = config.bucket_name(ACCOUNT)
    assert 3 <= len(name) <= 63 and re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", name)


def test_core_sources_hold_no_account_ids_or_real_emails():
    for name in ("config.py", "_aws.py", "__init__.py"):
        text = (REPO_ROOT / "infra" / name).read_text()
        assert not re.search(r"(?<!\d)\d{12}(?!\d)", text), name
        emails = re.findall(r"[\w.+-]+@([\w-]+\.[\w.]+)", text)
        assert all(domain.startswith("example.") for domain in emails), name


def test_importing_infra_does_not_load_boto3():
    code = "import sys, infra.config, infra._aws; print('boto3' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip() == "False"


# --- session, client, account -------------------------------------------------------------------
def test_get_session_returns_the_injected_session():
    fake = object()
    assert _aws.get_session(fake) is fake


def test_get_session_builds_one_on_the_profile_and_region(monkeypatch):
    calls = []
    monkeypatch.setattr(boto3, "Session", lambda **kw: calls.append(kw) or "S")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    assert _aws.get_session() == "S"
    assert calls == [{"profile_name": "wayakit-vdc", "region_name": "eu-north-1"}]


def test_get_session_refuses_the_default_profile(monkeypatch):
    monkeypatch.setattr(boto3, "Session", lambda **kw: pytest.fail("must not build a session"))
    monkeypatch.setenv("AWS_PROFILE", "default")
    with pytest.raises(SystemExit):
        _aws.get_session()


def test_client_pins_the_region():
    sess = RecordingSession({"s3": "C"})
    assert _aws.client(sess, "s3") == "C"
    assert sess.calls == [("s3", {"region_name": "eu-north-1"})]


def test_account_id_from_sts():
    sts, stub = _stubbed_sts(ACCOUNT)
    with stub:
        assert _aws.account_id(RecordingSession({"sts": sts})) == ACCOUNT
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("bad", ["", "12345", "00000000000a", "0000000000000"])
def test_account_id_rejects_malformed_answers(bad):
    class FakeSts:
        def get_caller_identity(self):
            return {"Account": bad}

    with pytest.raises(SystemExit):
        _aws.account_id(RecordingSession({"sts": FakeSts()}))


# --- tags --------------------------------------------------------------------------------------
def test_project_tags_shapes():
    assert _aws.project_tags() == [{"Key": "project", "Value": "price-intelligence"}]
    assert _aws.project_tags({"Name": "wayakit-app-server"}) == [
        {"Key": "Name", "Value": "wayakit-app-server"},
        {"Key": "project", "Value": "price-intelligence"},
    ]
    assert _aws.project_tags(lower=True) == [{"key": "project", "value": "price-intelligence"}]
    assert _aws.project_tags({"project": "price-intelligence"}) == _aws.project_tags()


def test_project_tag_cannot_be_overridden():
    with pytest.raises(ValueError):
        _aws.project_tags({"project": "other"})


def test_tags_to_dict_accepts_both_shapes():
    assert _aws.tags_to_dict(None) == {}
    assert _aws.tags_to_dict([{"Key": "a", "Value": "1"}, {"key": "b", "value": "2"}]) == {"a": "1", "b": "2"}


def test_tags_to_add():
    assert _aws.tags_to_add(None) == {"project": "price-intelligence"}
    assert _aws.tags_to_add([{"Key": "project", "Value": "price-intelligence"}]) == {}
    assert _aws.tags_to_add([{"Key": "project", "Value": "old"}]) == {"project": "price-intelligence"}
    current = [{"Key": "Name", "Value": "x"}]
    assert _aws.tags_to_add(current, {"Name": "x", "project": "price-intelligence"}) == {
        "project": "price-intelligence"}
    assert _aws.tags_to_add(current, [{"Key": "Name", "Value": "y"}]) == {"Name": "y"}


def test_merge_tags_keeps_other_tags_and_is_idempotent():
    current = [{"Key": "owner", "Value": "ops"}, {"Key": "project", "Value": "old"}]
    merged = _aws.merge_tags(current)
    assert _aws.tags_to_dict(merged) == {"owner": "ops", "project": "price-intelligence"}
    assert _aws.merge_tags(merged) == merged
    assert _aws.tags_to_add(merged) == {}
    assert _aws.merge_tags([], lower=True) == [{"key": "project", "value": "price-intelligence"}]


# --- policies ----------------------------------------------------------------------------------
POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "arn:aws:s3:::b/*"},
        {"Effect": "Allow", "Action": "sns:Publish", "Resource": ["arn:aws:sns:eu-north-1:0:t"],
         "Condition": {"StringEquals": {"aws:SourceAccount": "0"}}},
    ],
}


def test_policies_equal_ignores_order_and_list_forms():
    same = {
        "Statement": [
            {"Resource": "arn:aws:sns:eu-north-1:0:t", "Action": ["sns:Publish"], "Effect": "Allow",
             "Condition": {"StringEquals": {"aws:SourceAccount": ["0"]}}},
            {"Action": ["s3:PutObject", "s3:GetObject"], "Effect": "Allow", "Resource": ["arn:aws:s3:::b/*"]},
        ],
        "Version": "2012-10-17",
    }
    assert _aws.policies_equal(POLICY, same)
    assert _aws.policies_equal(json.dumps(POLICY), same)
    trust_a = {"Version": "2012-10-17", "Statement": {"Effect": "Allow", "Action": "sts:AssumeRole",
                                                      "Principal": {"Service": "scheduler.amazonaws.com"}}}
    trust_b = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["sts:AssumeRole"],
                                                       "Principal": {"Service": ["scheduler.amazonaws.com"]}}]}
    assert _aws.policies_equal(trust_a, trust_b)


@pytest.mark.parametrize("change", [
    lambda p: p["Statement"][0].update(Resource="arn:aws:s3:::other/*"),
    lambda p: p["Statement"][0]["Action"].append("s3:DeleteObject"),
    lambda p: p["Statement"][1]["Condition"]["StringEquals"].update({"aws:SourceAccount": "1"}),
    lambda p: p["Statement"][1].update(Effect="Deny"),
    lambda p: p["Statement"].pop(),
])
def test_policies_equal_detects_real_changes(change):
    other = json.loads(json.dumps(POLICY))
    change(other)
    assert not _aws.policies_equal(POLICY, other)


def test_policy_json_is_stable():
    reordered = json.loads(json.dumps(POLICY))
    reordered = {k: reordered[k] for k in reversed(list(reordered))}
    assert _aws.policy_json(POLICY) == _aws.policy_json(reordered)
    assert json.loads(_aws.policy_json(POLICY)) == POLICY


# --- output ------------------------------------------------------------------------------------
def test_report_dry_run_and_redaction(capsys):
    line = _aws.report(f"create role arn:aws:iam::{ACCOUNT}:role/r", dry_run=True, account=ACCOUNT)
    assert line == "[dry-run] would create role arn:aws:iam::<account>:role/r"
    assert capsys.readouterr().out == line + "\n"
    assert _aws.report("tag bucket") == "tag bucket"


def test_redact_account_without_account_is_a_no_op():
    assert _aws.redact_account("x 000000000000", None) == "x 000000000000"


@pytest.mark.parametrize("address, masked", [
    ("someone@example.com", "s***@example.com"),
    ("a@example.org", "a***@example.org"),
    ("not-an-email", "***"),
])
def test_mask_email(address, masked):
    assert _aws.mask_email(address) == masked


def test_json_diff():
    assert _aws.json_diff({"a": 1}, {"a": 1}) == ""
    diff = _aws.json_diff({"a": 1}, {"a": 2}, label="lifecycle")
    assert "lifecycle (AWS)" in diff and '-  "a": 1' in diff and '+  "a": 2' in diff


# --- errors and CLI ----------------------------------------------------------------------------
def test_error_code():
    err = ClientError({"Error": {"Code": "NoSuchEntity", "Message": "m"}}, "GetRole")
    assert _aws.error_code(err) == "NoSuchEntity"
    assert _aws.error_code(ValueError("x")) is None


def test_base_parser_has_dry_run():
    parser = _aws.base_parser("x")
    assert parser.parse_args([]).dry_run is False
    assert parser.parse_args(["--dry-run"]).dry_run is True
