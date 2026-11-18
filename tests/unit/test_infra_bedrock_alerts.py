"""Unit tests of `infra/bedrock_profiles.py` and `infra/alerts.py`.

No test reaches AWS. Exact operations and parameters are checked with `botocore.stub.Stubber` on
clients built with fake credentials in eu-north-1 (the Stubber also validates every request and
response against the real botocore model); multi-run behaviour (idempotency, no duplicate
subscription) uses the stateful fakes in `tests/unit/_fake_infra_bedrock_alerts.py`. The fake
account is `000000000000` and the email is on the reserved `example.com` domain.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import ANY, Stubber

from infra import alerts, bedrock_profiles as bp, config
from tests.conftest import REPO_ROOT, load_module
from tests.unit._fake_infra_bedrock_alerts import (
    ACCOUNT, PENDING, FakeBedrock, FakeSNS, FakeSTS, FakeSession, ExplodingSession,
    deepseek_models, haiku_models, mutating)

pytestmark = pytest.mark.unit

EMAIL = "someone@example.com"
MASKED = "s***@example.com"
PROJECT_TAG_LOWER = [{"key": "project", "value": "price-intelligence"}]
PROJECT_TAG_UPPER = [{"Key": "project", "Value": "price-intelligence"}]
TOPIC = f"arn:aws:sns:eu-north-1:{ACCOUNT}:price-intelligence-alerts"
DS_SRC = "arn:aws:bedrock:eu-north-1::foundation-model/deepseek.v3.2"
HK_SRC = f"arn:aws:bedrock:eu-north-1:{ACCOUNT}:inference-profile/eu.anthropic.claude-haiku-4-5-20251001-v1:0"
DS_ARN = f"arn:aws:bedrock:eu-north-1:{ACCOUNT}:application-inference-profile/abc123"
HK_ARN = f"arn:aws:bedrock:eu-north-1:{ACCOUNT}:application-inference-profile/def456"

# Every AWS operation each script may call. Anything else (delete, untag, unsubscribe, put_*)
# would be more than the job needs.
BEDROCK_ALLOWED = {"list_inference_profiles", "list_tags_for_resource", "tag_resource",
                   "create_inference_profile"}
SNS_ALLOWED = {"get_topic_attributes", "create_topic", "set_topic_attributes",
               "list_tags_for_resource", "tag_resource", "list_subscriptions_by_topic", "subscribe"}
BEDROCK_READS = {"ListInferenceProfiles", "ListTagsForResource", "GetCallerIdentity"}
SNS_READS = {"GetTopicAttributes", "ListTagsForResource", "ListSubscriptionsByTopic", "GetCallerIdentity"}


# --- Stubber helpers ---------------------------------------------------------------------------
def _client(service):
    return boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                         region_name=config.REGION).client(service, region_name=config.REGION)


class Stubbed:
    """A real botocore client with a Stubber and a log of every operation it was asked to run
    (recorded before the Stubber answers, so an unexpected call is logged too)."""

    def __init__(self, service):
        self.client = _client(service)
        self.ops = []
        self.client.meta.events.register(
            "before-parameter-build", lambda model, **kw: self.ops.append(model.name))
        self.stub = Stubber(self.client)

    def __enter__(self):
        self.stub.activate()
        return self

    def __exit__(self, *exc):
        self.stub.deactivate()


def _sts():
    s = Stubbed("sts")
    s.stub.add_response("get_caller_identity",
                        {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/fake", "UserId": "FAKE"}, {})
    return s


def _summary(name, arn, models):
    return {"inferenceProfileName": name, "inferenceProfileArn": arn, "inferenceProfileId": arn.rsplit("/", 1)[1],
            "status": "ACTIVE", "type": "APPLICATION", "models": models}


def _run(module, argv, service, stubbed, capsys):
    sts = _sts()
    session = FakeSession(sts=sts.client, **{service: stubbed.client})
    with sts, stubbed:
        rc = module.main(argv, session=session)
        stubbed.stub.assert_no_pending_responses()
        sts.stub.assert_no_pending_responses()
    out = capsys.readouterr()
    assert ACCOUNT not in out.out + out.err, "the account ID must always be shown as <account>"
    assert {c[0] for c in session.client_calls} == {"sts", service}
    assert all(kw == {"region_name": "eu-north-1"} for _, kw in session.client_calls)
    return rc, out.out


def _fake_run(module, argv, service, fake):
    """Run a script against a stateful fake; returns (rc, stdout, calls of this run)."""
    import contextlib
    import io

    start = len(fake.calls)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = module.main(argv, session=FakeSession(sts=FakeSTS(), **{service: fake}))
    return rc, buf.getvalue(), fake.calls[start:]


# ===============================================================================================
# bedrock_profiles: pure functions
# ===============================================================================================
def test_desired_profiles_contract():
    primary, fallback = bp.desired_profiles(ACCOUNT)
    assert primary == {
        "name": "price-intelligence-deepseek-v3-2", "model_id": "deepseek.v3.2",
        "source_id": "deepseek.v3.2", "source_arn": DS_SRC,
        "env_var": "PI_RELEVANCE_PRIMARY_MODEL", "default_id": "deepseek.v3.2", "fallback_allowed": True}
    assert fallback == {
        "name": "price-intelligence-haiku-4-5", "model_id": "anthropic.claude-haiku-4-5-20251001-v1:0",
        "source_id": "eu.anthropic.claude-haiku-4-5-20251001-v1:0", "source_arn": HK_SRC,
        "env_var": "PI_RELEVANCE_FALLBACK_MODEL",
        "default_id": "eu.anthropic.claude-haiku-4-5-20251001-v1:0", "fallback_allowed": False}


def test_profile_names_and_descriptions_are_accepted_by_the_api():
    """botocore does not check `pattern` client side: a bad name or description would only fail
    in AWS, at the first real run. Check them against the API model's own constraints."""
    model = _client("bedrock").meta.service_model.operation_model("CreateInferenceProfile").input_shape
    name_meta, desc_meta = model.members["inferenceProfileName"].metadata, model.members["description"].metadata
    for spec in bp.desired_profiles(ACCOUNT):
        assert re.fullmatch(name_meta["pattern"], spec["name"])
        assert name_meta["min"] <= len(spec["name"]) <= name_meta["max"]
        text = bp.profile_description(spec["source_id"])
        assert re.fullmatch(desc_meta["pattern"], text)
        assert desc_meta["min"] <= len(text) <= desc_meta["max"]
        assert spec["source_id"] in text


@pytest.mark.parametrize("bad", ["model(1)", "a,b", "two  spaces", "x" * 200, "a/b", "end!"])
def test_profile_description_rejects_what_the_api_rejects(bad):
    with pytest.raises(ValueError):
        bp.profile_description(bad)


def test_env_defaults_are_the_ones_the_relevance_code_uses():
    ai = load_module("scraper/services/ai_service.py")
    assert bp.env_assignments({}) == {ai.PRIMARY_MODEL_ENV: ai.DEFAULT_PRIMARY_MODEL,
                                      ai.FALLBACK_MODEL_ENV: ai.DEFAULT_FALLBACK_MODEL}


def test_env_assignments_uses_existing_arns():
    assert bp.env_assignments({config.DEEPSEEK_PROFILE: DS_ARN, "other": "x"}) == {
        "PI_RELEVANCE_PRIMARY_MODEL": DS_ARN,
        "PI_RELEVANCE_FALLBACK_MODEL": "eu.anthropic.claude-haiku-4-5-20251001-v1:0"}
    assert bp.env_assignments({config.DEEPSEEK_PROFILE: DS_ARN, config.HAIKU_PROFILE: HK_ARN}) == {
        "PI_RELEVANCE_PRIMARY_MODEL": DS_ARN, "PI_RELEVANCE_FALLBACK_MODEL": HK_ARN}


def test_model_arns_source_matches_and_regions():
    hk = haiku_models(("eu-west-1", "eu-central-1", "eu-west-1"))
    assert bp.model_arns([{"modelArn": "a"}, {}, {"modelArn": ""}, {"modelArn": "b"}]) == ["a", "b"]
    assert bp.model_arns(None) == []
    assert bp.source_matches(config.HAIKU_MODEL_ID, hk)
    assert bp.source_matches(config.DEEPSEEK_MODEL_ID, deepseek_models())
    assert not bp.source_matches(config.DEEPSEEK_MODEL_ID, hk)
    assert not bp.source_matches(config.DEEPSEEK_MODEL_ID, [])
    assert not bp.source_matches(config.DEEPSEEK_MODEL_ID, None)
    # One stray model is enough to reject the profile.
    assert not bp.source_matches(config.HAIKU_MODEL_ID, hk + deepseek_models())
    # A model whose ID only ends like ours is another model.
    assert not bp.source_matches(config.DEEPSEEK_MODEL_ID,
                                 [{"modelArn": config.foundation_model_arn("x-deepseek.v3.2")}])
    assert bp.regions_of(hk) == ["eu-central-1", "eu-west-1"]
    assert bp.regions_of([]) == []


def test_arns_by_name():
    summaries = {"a": {"inferenceProfileArn": DS_ARN, "models": []}, "b": {"inferenceProfileArn": HK_ARN}}
    assert bp.arns_by_name(summaries) == {"a": DS_ARN, "b": HK_ARN}


# ===============================================================================================
# bedrock_profiles: main with Stubber (exact operations and parameters)
# ===============================================================================================
def test_bedrock_apply_creates_both_tagged_profiles(capsys):
    # The Stubber checks the parameters before botocore adds `clientRequestToken`, so the
    # expected parameters are exactly what the script passes.
    b = Stubbed("bedrock")
    b.stub.add_response("list_inference_profiles", {"inferenceProfileSummaries": []}, {"typeEquals": "APPLICATION"})
    b.stub.add_response(
        "create_inference_profile", {"inferenceProfileArn": DS_ARN, "status": "ACTIVE"},
        {"inferenceProfileName": "price-intelligence-deepseek-v3-2",
         "description": "Price Intelligence tagged copy of deepseek.v3.2",
         "modelSource": {"copyFrom": DS_SRC}, "tags": PROJECT_TAG_LOWER})
    b.stub.add_response(
        "create_inference_profile", {"inferenceProfileArn": HK_ARN, "status": "ACTIVE"},
        {"inferenceProfileName": "price-intelligence-haiku-4-5",
         "description": "Price Intelligence tagged copy of eu.anthropic.claude-haiku-4-5-20251001-v1:0",
         "modelSource": {"copyFrom": HK_SRC}, "tags": PROJECT_TAG_LOWER})
    rc, out = _run(bp, [], "bedrock", b, capsys)
    assert rc == 0
    assert b.ops == ["ListInferenceProfiles", "CreateInferenceProfile", "CreateInferenceProfile"]
    redacted = lambda arn: arn.replace(ACCOUNT, "<account>")  # noqa: E731
    assert f"  PI_RELEVANCE_PRIMARY_MODEL={redacted(DS_ARN)}" in out.splitlines()
    assert f"  PI_RELEVANCE_FALLBACK_MODEL={redacted(HK_ARN)}" in out.splitlines()
    assert "[dry-run]" not in out


def test_bedrock_dry_run_with_nothing_makes_only_read_calls(capsys):
    b = Stubbed("bedrock")
    b.stub.add_response("list_inference_profiles", {"inferenceProfileSummaries": []}, {"typeEquals": "APPLICATION"})
    rc, out = _run(bp, ["--dry-run"], "bedrock", b, capsys)
    assert rc == 0
    assert b.ops == ["ListInferenceProfiles"]
    assert "[dry-run] would create price-intelligence-deepseek-v3-2 copied from " + DS_SRC in out
    assert ("[dry-run] would create price-intelligence-haiku-4-5 copied from "
            + HK_SRC.replace(ACCOUNT, "<account>")) in out
    assert out.rstrip().endswith("--dry-run: nothing was changed.")
    # The placeholder says what will go there, including the documented fallback.
    assert "deepseek.v3.2 if AWS rejects the copy" in out


def _both_exist(b, deepseek_tags, haiku_tags):
    b.stub.add_response("list_inference_profiles", {"inferenceProfileSummaries": [
        _summary(config.DEEPSEEK_PROFILE, DS_ARN, deepseek_models()),
        _summary(config.HAIKU_PROFILE, HK_ARN, haiku_models())]}, {"typeEquals": "APPLICATION"})
    b.stub.add_response("list_tags_for_resource", {"tags": deepseek_tags}, {"resourceARN": DS_ARN})


def test_bedrock_second_apply_changes_nothing(capsys):
    """Idempotency: both profiles exist with the tag, so no create and no tag call."""
    b = Stubbed("bedrock")
    _both_exist(b, PROJECT_TAG_LOWER, PROJECT_TAG_LOWER)
    b.stub.add_response("list_tags_for_resource", {"tags": PROJECT_TAG_LOWER}, {"resourceARN": HK_ARN})
    rc, out = _run(bp, [], "bedrock", b, capsys)
    assert rc == 0
    assert set(b.ops) <= BEDROCK_READS
    assert "regions: eu-central-1, eu-north-1, eu-west-1, eu-west-3" in out


def test_bedrock_apply_adds_only_the_missing_tag(capsys):
    b = Stubbed("bedrock")
    _both_exist(b, [{"key": "owner", "value": "someone-else"}], None)
    b.stub.add_response("tag_resource", {}, {"resourceARN": DS_ARN, "tags": PROJECT_TAG_LOWER})
    b.stub.add_response("list_tags_for_resource", {"tags": PROJECT_TAG_LOWER + [{"key": "x", "value": "y"}]},
                        {"resourceARN": HK_ARN})
    rc, _ = _run(bp, [], "bedrock", b, capsys)
    assert rc == 0
    assert b.ops.count("TagResource") == 1 and "UntagResource" not in b.ops
    assert "CreateInferenceProfile" not in b.ops


def test_bedrock_dry_run_reports_the_missing_tag_without_tagging(capsys):
    b = Stubbed("bedrock")
    _both_exist(b, [], None)
    b.stub.add_response("list_tags_for_resource", {"tags": PROJECT_TAG_LOWER}, {"resourceARN": HK_ARN})
    rc, out = _run(bp, ["--dry-run"], "bedrock", b, capsys)
    assert rc == 0
    assert set(b.ops) <= BEDROCK_READS
    assert "[dry-run] would tag price-intelligence-deepseek-v3-2 with {'project': 'price-intelligence'}" in out


def test_bedrock_existing_profile_with_another_model_fails_without_touching_it(capsys):
    b = Stubbed("bedrock")
    b.stub.add_response("list_inference_profiles", {"inferenceProfileSummaries": [
        _summary(config.DEEPSEEK_PROFILE, DS_ARN, [{"modelArn": config.foundation_model_arn("deepseek.r1")}]),
        _summary(config.HAIKU_PROFILE, HK_ARN, haiku_models())]}, {"typeEquals": "APPLICATION"})
    # No tag read or write for the wrong profile; the Haiku one is still processed.
    b.stub.add_response("list_tags_for_resource", {"tags": PROJECT_TAG_LOWER}, {"resourceARN": HK_ARN})
    rc, out = _run(bp, [], "bedrock", b, capsys)
    assert rc == 1
    assert b.ops == ["ListInferenceProfiles", "ListTagsForResource"]
    assert "[FAIL] price-intelligence-deepseek-v3-2" in out
    assert "  PI_RELEVANCE_PRIMARY_MODEL=<unusable: fix price-intelligence-deepseek-v3-2 first>" in out


def test_bedrock_deepseek_copy_rejected_falls_back_to_the_plain_model_id(capsys):
    b = Stubbed("bedrock")
    b.stub.add_response("list_inference_profiles", {"inferenceProfileSummaries": []}, {"typeEquals": "APPLICATION"})
    b.stub.add_client_error("create_inference_profile", "ValidationException", "not supported", 400,
                            expected_params={"inferenceProfileName": config.DEEPSEEK_PROFILE,
                                             "description": ANY, "modelSource": {"copyFrom": DS_SRC},
                                             "tags": PROJECT_TAG_LOWER})
    b.stub.add_response("create_inference_profile", {"inferenceProfileArn": HK_ARN, "status": "ACTIVE"},
                        {"inferenceProfileName": config.HAIKU_PROFILE, "description": ANY,
                         "modelSource": {"copyFrom": HK_SRC}, "tags": PROJECT_TAG_LOWER})
    rc, out = _run(bp, [], "bedrock", b, capsys)
    assert rc == 0
    assert "[FALLBACK] price-intelligence-deepseek-v3-2" in out
    assert "  PI_RELEVANCE_PRIMARY_MODEL=deepseek.v3.2" in out.splitlines()
    assert "not supported" not in out


def test_bedrock_haiku_copy_rejected_fails(capsys):
    b = Stubbed("bedrock")
    b.stub.add_response("list_inference_profiles", {"inferenceProfileSummaries": [
        _summary(config.DEEPSEEK_PROFILE, DS_ARN, deepseek_models())]}, {"typeEquals": "APPLICATION"})
    b.stub.add_response("list_tags_for_resource", {"tags": PROJECT_TAG_LOWER}, {"resourceARN": DS_ARN})
    b.stub.add_client_error("create_inference_profile", "ValidationException", "no", 400)
    rc, out = _run(bp, [], "bedrock", b, capsys)
    assert rc == 1
    assert "ERROR: CreateInferenceProfile failed (ValidationException)" in out
    assert "[FALLBACK]" not in out


@pytest.mark.parametrize("code", ["AccessDeniedException", "ServiceQuotaExceededException", "ThrottlingException"])
def test_bedrock_other_errors_exit_1_and_stop(capsys, code):
    """Any other error on the first create exits 1, prints only operation and code (the AWS
    message can carry the account) and does not go on to create the second profile."""
    b = Stubbed("bedrock")
    b.stub.add_response("list_inference_profiles", {"inferenceProfileSummaries": []}, {"typeEquals": "APPLICATION"})
    b.stub.add_client_error("create_inference_profile", code,
                            f"User: arn:aws:iam::{ACCOUNT}:user/fake is not authorized", 400)
    rc, out = _run(bp, [], "bedrock", b, capsys)
    assert rc == 1
    assert out.strip().splitlines()[-1] == (
        f"ERROR: CreateInferenceProfile failed ({code}); nothing else was changed after it.")
    assert b.ops == ["ListInferenceProfiles", "CreateInferenceProfile"]
    assert "not authorized" not in out


def test_bedrock_list_error_exits_1(capsys):
    b = Stubbed("bedrock")
    b.stub.add_client_error("list_inference_profiles", "AccessDeniedException", f"arn:aws:iam::{ACCOUNT}:x", 403)
    rc, out = _run(bp, ["--dry-run"], "bedrock", b, capsys)
    assert rc == 1
    assert "ERROR: ListInferenceProfiles failed (AccessDeniedException)" in out


def test_bedrock_client_error_without_network_exits_1(capsys):
    class Broken(FakeBedrock):
        def get_paginator(self, operation):
            raise EndpointConnectionError(endpoint_url="https://bedrock.eu-north-1.amazonaws.com")

    rc = bp.main([], session=FakeSession(sts=FakeSTS(), bedrock=Broken()))
    out = capsys.readouterr().out
    assert rc == 1
    assert out.strip() == "ERROR: AWS client error (EndpointConnectionError)."


def test_resolve_model_ids_reads_only_and_returns_real_arns():
    fake = FakeBedrock()
    ds = fake.add(config.DEEPSEEK_PROFILE, deepseek_models())
    session = FakeSession(bedrock=fake)
    assert bp.resolve_model_ids(session) == {"PI_RELEVANCE_PRIMARY_MODEL": ds,
                                            "PI_RELEVANCE_FALLBACK_MODEL": config.HAIKU_SYSTEM_PROFILE_ID}
    assert ACCOUNT in ds  # real value for the env file; only printing redacts it
    assert mutating(fake.calls) == []
    assert session.client_calls == [("bedrock", {"region_name": "eu-north-1"})]


# --- bedrock_profiles: several runs (stateful fake) --------------------------------------------
@pytest.mark.parametrize("reject_deepseek", [False, True])
def test_bedrock_two_applies_converge_and_the_second_changes_nothing(reject_deepseek):
    fake = FakeBedrock(reject_copy_from={DS_SRC} if reject_deepseek else ())
    rc1, out1, calls1 = _fake_run(bp, [], "bedrock", fake)
    state = fake.state()
    rc2, out2, calls2 = _fake_run(bp, [], "bedrock", fake)
    assert rc1 == rc2 == 0
    assert fake.state() == state
    assert all(dict(p["tags"]).get("project") == "price-intelligence" for p in fake.profiles.values())
    if reject_deepseek:
        # Nothing exists to keep, so the copy is attempted (and rejected) again; nothing else.
        assert mutating(calls2) == ["create_inference_profile"]
        assert set(fake.profiles) == {config.HAIKU_PROFILE}
    else:
        assert mutating(calls2) == []
        assert set(fake.profiles) == {config.DEEPSEEK_PROFILE, config.HAIKU_PROFILE}
    # Same instance values both times.
    env = lambda out: [l for l in out.splitlines() if l.startswith("  PI_")]  # noqa: E731
    assert env(out1) == env(out2)
    assert ACCOUNT not in out1 + out2


def test_bedrock_reads_every_page_of_profiles():
    """The fake splits the list in two pages: a profile on the second page must be found, or the
    script would try to create a duplicate."""
    fake = FakeBedrock()
    fake.add(config.DEEPSEEK_PROFILE, deepseek_models(), {"project": "price-intelligence"})
    fake.add(config.HAIKU_PROFILE, haiku_models(), {"project": "price-intelligence"})
    rc, _, calls = _fake_run(bp, [], "bedrock", fake)
    assert rc == 0 and mutating(calls) == []


# ===============================================================================================
# alerts: pure functions
# ===============================================================================================
def test_desired_and_changed_attributes():
    assert alerts.desired_attributes() == {"KmsMasterKeyId": "alias/aws/sns"}
    assert alerts.attribute_changes(None) == {"KmsMasterKeyId": "alias/aws/sns"}
    assert alerts.attribute_changes({}) == {"KmsMasterKeyId": "alias/aws/sns"}
    assert alerts.attribute_changes({"KmsMasterKeyId": "alias/other"}) == {"KmsMasterKeyId": "alias/aws/sns"}
    assert alerts.attribute_changes({"KmsMasterKeyId": "alias/aws/sns", "DisplayName": "x"}) == {}


@pytest.mark.parametrize("raw, clean", [
    (EMAIL, EMAIL), (f"  {EMAIL}\n", EMAIL), ("First.Last+pi@sub.example.org", "First.Last+pi@sub.example.org")])
def test_normalize_email_valid(raw, clean):
    assert alerts.normalize_email(raw) == clean


@pytest.mark.parametrize("raw", [
    "", "   ", None, "someone", "someone@", "@example.com", "someone@example", "a b@example.com",
    "a@b@example.com", "someone@exa mple.com", "someone@example.com,other@example.com"])
def test_normalize_email_invalid_never_echoes_the_input(raw):
    with pytest.raises(ValueError) as info:
        alerts.normalize_email(raw)
    if raw and raw.strip():
        assert raw.strip() not in str(info.value)
    assert str(info.value) == "--email is not a valid email address."


def _sub(endpoint, protocol="email", pending=False):
    return {"SubscriptionArn": PENDING if pending else f"{TOPIC}:abc", "Protocol": protocol,
            "Endpoint": endpoint, "TopicArn": TOPIC, "Owner": ACCOUNT}


def test_find_email_subscription():
    subs = [_sub(EMAIL, "email-json"), _sub(EMAIL, "sms"), _sub(" SomeOne@Example.COM ", pending=True)]
    assert alerts.find_email_subscription(subs, EMAIL) is subs[2]
    assert alerts.find_email_subscription(subs, "  SOMEONE@example.com ") is subs[2]
    assert alerts.find_email_subscription(subs[:2], EMAIL) is None
    assert alerts.find_email_subscription([_sub("other@example.com")], EMAIL) is None
    assert alerts.find_email_subscription(None, EMAIL) is None
    assert alerts.find_email_subscription([{"Protocol": "email"}], EMAIL) is None


def test_subscription_state_and_counts():
    assert alerts.subscription_state(_sub(EMAIL, pending=True)) == "pending"
    assert alerts.subscription_state(_sub(EMAIL)) == "confirmed"
    assert alerts.subscription_counts({"SubscriptionsConfirmed": "2", "SubscriptionsPending": "1"}) == (2, 1)
    assert alerts.subscription_counts(None) == (0, 0)
    assert alerts.subscription_counts({"SubscriptionsConfirmed": None}) == (0, 0)


def test_get_topic_attributes_missing_and_other_errors():
    for code in ("NotFound", "NotFoundException"):
        s = Stubbed("sns")
        s.stub.add_client_error("get_topic_attributes", code, "x", 404, expected_params={"TopicArn": TOPIC})
        with s:
            assert alerts.get_topic_attributes(s.client, TOPIC) is None
    s = Stubbed("sns")
    s.stub.add_client_error("get_topic_attributes", "AuthorizationError", "x", 403)
    with s, pytest.raises(ClientError):
        alerts.get_topic_attributes(s.client, TOPIC)


def test_list_subscriptions_reads_every_page():
    s = Stubbed("sns")
    s.stub.add_response("list_subscriptions_by_topic", {"Subscriptions": [_sub("a@example.com")], "NextToken": "n"},
                        {"TopicArn": TOPIC})
    s.stub.add_response("list_subscriptions_by_topic", {"Subscriptions": [_sub("b@example.com")]},
                        {"TopicArn": TOPIC, "NextToken": "n"})
    with s:
        subs = alerts.list_subscriptions(s.client, TOPIC)
        s.stub.assert_no_pending_responses()
    assert [x["Endpoint"] for x in subs] == ["a@example.com", "b@example.com"]


# ===============================================================================================
# alerts: main with Stubber
# ===============================================================================================
def _topic_attrs(confirmed="0", pending="0", kms="alias/aws/sns"):
    attrs = {"TopicArn": TOPIC, "Owner": ACCOUNT, "SubscriptionsConfirmed": confirmed,
             "SubscriptionsPending": pending}
    if kms is not None:
        attrs["KmsMasterKeyId"] = kms
    return {"Attributes": attrs}


def _assert_email_hidden(out):
    assert EMAIL.lower() not in out.lower()
    assert "someone" not in out.lower()


def test_alerts_apply_creates_topic_and_subscribes(capsys):
    s = Stubbed("sns")
    s.stub.add_client_error("get_topic_attributes", "NotFound", "x", 404, expected_params={"TopicArn": TOPIC})
    s.stub.add_response("create_topic", {"TopicArn": TOPIC}, {
        "Name": "price-intelligence-alerts", "Attributes": {"KmsMasterKeyId": "alias/aws/sns"},
        "Tags": PROJECT_TAG_UPPER})
    s.stub.add_response("subscribe", {"SubscriptionArn": "pending confirmation"},
                        {"TopicArn": TOPIC, "Protocol": "email", "Endpoint": EMAIL})
    rc, out = _run(alerts, ["--email", f"  {EMAIL} "], "sns", s, capsys)
    assert rc == 0
    # A topic that did not exist has no subscriptions to list.
    assert s.ops == ["GetTopicAttributes", "CreateTopic", "Subscribe"]
    _assert_email_hidden(out)
    assert f"[OK] {MASKED}: subscription requested" in out
    assert "Subscriptions: 0 confirmed, 1 pending confirmation." in out
    assert "[WARN] no confirmed subscription" in out
    assert "  PI_ALERTS_TOPIC_ARN=arn:aws:sns:eu-north-1:<account>:price-intelligence-alerts" in out.splitlines()


def test_alerts_dry_run_missing_topic_reads_only(capsys):
    s = Stubbed("sns")
    s.stub.add_client_error("get_topic_attributes", "NotFound", "x", 404, expected_params={"TopicArn": TOPIC})
    rc, out = _run(alerts, ["--dry-run", "--email", EMAIL], "sns", s, capsys)
    assert rc == 0
    assert s.ops == ["GetTopicAttributes"]
    assert "[dry-run] would create SNS topic price-intelligence-alerts" in out
    assert f"[dry-run] would subscribe {MASKED} by email" in out
    assert out.rstrip().endswith("--dry-run: nothing was changed.")
    _assert_email_hidden(out)


@pytest.mark.parametrize("pending, state", [(False, "confirmed"), (True, "pending")])
def test_alerts_already_subscribed_is_not_subscribed_again(capsys, pending, state):
    s = Stubbed("sns")
    s.stub.add_response("get_topic_attributes", _topic_attrs("0" if pending else "1", "1" if pending else "0"),
                        {"TopicArn": TOPIC})
    s.stub.add_response("list_tags_for_resource", {"Tags": PROJECT_TAG_UPPER}, {"ResourceArn": TOPIC})
    s.stub.add_response("list_subscriptions_by_topic",
                        {"Subscriptions": [_sub("other@example.com"), _sub(" SomeOne@EXAMPLE.com", pending=pending)]},
                        {"TopicArn": TOPIC})
    rc, out = _run(alerts, ["--email", "someone@example.COM"], "sns", s, capsys)
    assert rc == 0
    assert set(s.ops) <= SNS_READS, s.ops
    assert f"[OK] s***@example.COM: already subscribed ({state})" in out
    _assert_email_hidden(out)
    assert ("[WARN]" in out) == pending


def test_alerts_existing_topic_fixes_encryption_and_tag(capsys):
    s = Stubbed("sns")
    s.stub.add_response("get_topic_attributes", _topic_attrs("1", kms=None), {"TopicArn": TOPIC})
    s.stub.add_response("set_topic_attributes", {}, {
        "TopicArn": TOPIC, "AttributeName": "KmsMasterKeyId", "AttributeValue": "alias/aws/sns"})
    s.stub.add_response("list_tags_for_resource", {"Tags": [{"Key": "owner", "Value": "x"}]}, {"ResourceArn": TOPIC})
    s.stub.add_response("tag_resource", {}, {"ResourceArn": TOPIC, "Tags": PROJECT_TAG_UPPER})
    rc, out = _run(alerts, [], "sns", s, capsys)
    assert rc == 0
    assert s.ops == ["GetTopicAttributes", "SetTopicAttributes", "ListTagsForResource", "TagResource"]
    assert "Subscriptions: 1 confirmed, 0 pending confirmation." in out
    assert "[WARN]" not in out


def test_alerts_dry_run_existing_topic_only_reads(capsys):
    s = Stubbed("sns")
    s.stub.add_response("get_topic_attributes", _topic_attrs(kms="alias/other"), {"TopicArn": TOPIC})
    s.stub.add_response("list_tags_for_resource", {"Tags": []}, {"ResourceArn": TOPIC})
    s.stub.add_response("list_subscriptions_by_topic", {"Subscriptions": []}, {"TopicArn": TOPIC})
    rc, out = _run(alerts, ["--dry-run", "--email", EMAIL], "sns", s, capsys)
    assert rc == 0
    assert set(s.ops) <= SNS_READS, s.ops
    assert "[dry-run] would set KmsMasterKeyId=alias/aws/sns on price-intelligence-alerts (now: alias/other)" in out
    assert "[dry-run] would tag price-intelligence-alerts" in out
    assert f"[dry-run] would subscribe {MASKED}" in out
    assert "Subscriptions: 0 confirmed, 1 pending confirmation." in out
    _assert_email_hidden(out)


def test_alerts_same_address_on_another_protocol_still_gets_an_email_subscription(capsys):
    s = Stubbed("sns")
    s.stub.add_response("get_topic_attributes", _topic_attrs("1"), {"TopicArn": TOPIC})
    s.stub.add_response("list_tags_for_resource", {"Tags": PROJECT_TAG_UPPER}, {"ResourceArn": TOPIC})
    s.stub.add_response("list_subscriptions_by_topic", {"Subscriptions": [_sub(EMAIL, "email-json")]},
                        {"TopicArn": TOPIC})
    s.stub.add_response("subscribe", {"SubscriptionArn": "pending confirmation"},
                        {"TopicArn": TOPIC, "Protocol": "email", "Endpoint": EMAIL})
    rc, out = _run(alerts, ["--email", EMAIL], "sns", s, capsys)
    assert rc == 0
    _assert_email_hidden(out)


def test_alerts_without_email_touches_no_subscription(capsys):
    s = Stubbed("sns")
    s.stub.add_response("get_topic_attributes", _topic_attrs(), {"TopicArn": TOPIC})
    s.stub.add_response("list_tags_for_resource", {"Tags": PROJECT_TAG_UPPER}, {"ResourceArn": TOPIC})
    rc, out = _run(alerts, [], "sns", s, capsys)
    assert rc == 0
    assert s.ops == ["GetTopicAttributes", "ListTagsForResource"]
    assert "Subscriptions: 0 confirmed, 0 pending confirmation." in out
    assert "[WARN] no confirmed subscription" in out


@pytest.mark.parametrize("bad", ["someone", "someone@example", "x y@example.com", "   "])
def test_alerts_invalid_email_exits_1_before_any_aws_call(capsys, bad):
    rc = alerts.main(["--email", bad], session=ExplodingSession())
    out = capsys.readouterr()
    assert rc == 1
    assert out.out.strip() == "ERROR: --email is not a valid email address."
    if bad.strip():
        assert bad.strip() not in out.out + out.err


def test_alerts_subscribe_error_hides_the_aws_message(capsys):
    """The AWS message can echo the address and the account: only operation and code are shown."""
    s = Stubbed("sns")
    s.stub.add_response("get_topic_attributes", _topic_attrs("1"), {"TopicArn": TOPIC})
    s.stub.add_response("list_tags_for_resource", {"Tags": PROJECT_TAG_UPPER}, {"ResourceArn": TOPIC})
    s.stub.add_response("list_subscriptions_by_topic", {"Subscriptions": []}, {"TopicArn": TOPIC})
    s.stub.add_client_error("subscribe", "InvalidParameter", f"Invalid endpoint {EMAIL} for {TOPIC}", 400)
    rc, out = _run(alerts, ["--email", EMAIL], "sns", s, capsys)
    assert rc == 1
    assert out.strip().splitlines()[-1] == "ERROR: Subscribe failed (InvalidParameter); nothing else was changed after it."
    _assert_email_hidden(out)


def test_alerts_email_is_never_written_to_disk_or_logs(tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.chdir(tmp_path)
    fake = FakeSNS()
    with caplog.at_level("DEBUG"):
        assert alerts.main(["--email", EMAIL], session=FakeSession(sts=FakeSTS(), sns=fake)) == 0
        assert alerts.main(["--dry-run", "--email", EMAIL], session=FakeSession(sts=FakeSTS(), sns=fake)) == 0
    out = capsys.readouterr()
    _assert_email_hidden(out.out + out.err + caplog.text)
    assert list(tmp_path.iterdir()) == []
    # The only place the address goes is the Subscribe call itself.
    assert [p for op, p in fake.calls if EMAIL in repr(p)] == [
        {"TopicArn": TOPIC, "Protocol": "email", "Endpoint": EMAIL}]


@pytest.mark.parametrize("argv, echoes", [
    # argparse would echo the offending argv in these three: the token must come out redacted.
    (["--emial", EMAIL], True),                         # typo in the option name (the found bug)
    (["--email", EMAIL, EMAIL], True),                  # extra positional address
    ([f"--dry-run={EMAIL}", "--email", EMAIL], True),   # "ignored explicit argument '<addr>'"
    # These do not echo the value; still exit 2 with nothing in full.
    (["--email"], False),                               # missing value
    (["--email", f"-{EMAIL}"], False),                  # value read as an option
])
def test_alerts_cli_error_never_echoes_the_email(argv, echoes, capsys):
    """Was xfail (pi-dev fixed it with `redact_cli_message`): a command-line mistake exits 2 with
    the argparse usage line, before any AWS call, and never prints the address in full."""
    with pytest.raises(SystemExit) as exc:
        alerts.main(argv, session=ExplodingSession())
    assert exc.value.code == 2
    out = capsys.readouterr()
    _assert_email_hidden(out.out + out.err)
    assert "@" not in out.out + out.err
    assert out.err.startswith("usage: ")
    assert "error: " in out.err
    assert ("<redacted>" in out.err) is echoes


# --- alerts: several runs (stateful fake) ------------------------------------------------------
def test_alerts_two_applies_never_duplicate_the_subscription():
    fake = FakeSNS()
    rc1, _, calls1 = _fake_run(alerts, ["--email", EMAIL], "sns", fake)
    state = fake.state()
    # Same address, different case and spaces: still the same subscription.
    rc2, out2, calls2 = _fake_run(alerts, ["--email", " SomeOne@Example.com "], "sns", fake)
    assert rc1 == rc2 == 0
    assert mutating(calls1) == ["create_topic", "subscribe"]
    assert mutating(calls2) == []
    assert fake.state() == state
    assert [s["Endpoint"] for s in fake.subs] == [EMAIL]
    assert "already subscribed (pending)" in out2
    assert fake.tags == {"project": "price-intelligence"}
    assert fake.attributes == {"KmsMasterKeyId": "alias/aws/sns"}


def test_alerts_finds_the_subscription_on_a_later_page():
    subs = [{"Protocol": "email", "Endpoint": f"user{i}@example.com", "confirmed": True} for i in range(5)]
    subs.append({"Protocol": "email", "Endpoint": EMAIL, "confirmed": True})
    fake = FakeSNS(exists=True, attributes={"KmsMasterKeyId": "alias/aws/sns"},
                   tags={"project": "price-intelligence"}, subs=subs)
    rc, _, calls = _fake_run(alerts, ["--email", EMAIL], "sns", fake)
    assert rc == 0 and mutating(calls) == []


# ===============================================================================================
# Both scripts: least privilege, imports, sources
# ===============================================================================================
def _client_methods(path, names):
    """Methods called on the variables `names` (the AWS clients) anywhere in the module."""
    tree = ast.parse((REPO_ROOT / path).read_text())
    found = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id in names):
            found.add(node.func.attr)
    return found


def _paginated(path):
    tree = ast.parse((REPO_ROOT / path).read_text())
    return {n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get_paginator"}


def test_scripts_call_only_the_operations_they_need():
    """Static check over every code path: no delete, untag, unsubscribe or policy call exists."""
    bed = (_client_methods("infra/bedrock_profiles.py", {"bedrock"}) - {"get_paginator"}) | _paginated(
        "infra/bedrock_profiles.py")
    sns = (_client_methods("infra/alerts.py", {"sns"}) - {"get_paginator"}) | _paginated("infra/alerts.py")
    assert bed == BEDROCK_ALLOWED
    assert sns == SNS_ALLOWED
    for path in ("infra/bedrock_profiles.py", "infra/alerts.py"):
        src = (REPO_ROOT / path).read_text()
        assert not re.search(r"\.(delete_\w+|untag_resource|unsubscribe|put_\w+|"
                             r"set_subscription_attributes|add_permission)\(", src), path


def test_importing_the_scripts_does_not_load_boto3_or_call_aws():
    code = ("import sys; import infra.bedrock_profiles, infra.alerts; "
            "print('boto3' in sys.modules)")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    res = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=env, timeout=60)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "False"


def test_sources_hold_no_account_ids_or_emails():
    for path in ("infra/bedrock_profiles.py", "infra/alerts.py"):
        src = (REPO_ROOT / path).read_text()
        assert not re.search(r"(?<!\d)\d{12}(?!\d)", src), path
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", src), path


@pytest.mark.parametrize("module", [bp, alerts])
def test_help_runs_without_aws(module, capsys):
    with pytest.raises(SystemExit) as info:
        module.main(["--help"], session=ExplodingSession())
    assert info.value.code == 0
    assert "--dry-run" in capsys.readouterr().out
