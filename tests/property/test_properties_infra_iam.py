"""Property tests of the F3 IAM builders and flows (`infra/instance_role.py`,
`infra/scheduler_role.py`, `infra/deploy_role.py`).

- Least privilege (invariants): no action has a wildcard; `Resource: "*"` appears only in
  `PutPiMetrics` and always with the namespace condition; every other wildcard is one of the
  documented ones; every ARN is in eu-north-1 and this account, or is a Bedrock foundation model
  (no account) in the EU.
- Validation: every account-scoped builder rejects any account that is not 12 ASCII digits
  (Unicode digits, `*`, empty, ints, 11 or 13 digits); the instance-scoped builders reject any
  string that is not one instance ID;
  `instance_policy` rejects non-EU or non-Haiku models, profile ARNs of another account, region or
  kind, and unknown profile names.
- Equivalence: `instance_policy` does not depend on the order or repetition of the Haiku models.
- Deploy trust: `sub` is exactly the `main` branch subject, with `StringEquals`, for any account.
- `redact_instance_ids`: no instance ID survives, and it is idempotent.
- `summarize_audit`: "only PI" implies every signal is PI; records of other issuers never count.
- Flows (stateful fake): from any drifted state, dry-run makes no write; apply then apply again
  makes no write the second time; nothing ever writes on the old roles.

No test reaches AWS. The account is 12 digits made up by Hypothesis or `000000000000`.
"""
from __future__ import annotations

import json
import re

import pytest
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

from infra import _aws, deploy_role, instance_role, scheduler_role
from tests.unit.test_infra_iam import (ACCOUNT, HAIKU_APP, IR, MODULES, OIDC_ARN, OLD_ROLE_ARN, OLD_ROLES,
                                       SR, DR, FakeAws, assume_record, session_record)

pytestmark = pytest.mark.property

HAIKU_MODEL_ID = "anthropic.claude-haiku-4-5-20251001-v1:0"
PROFILE_NAMES = instance_role.APPLICATION_PROFILES
INSTANCE_RE = re.compile(r"i-[0-9a-f]{8,17}")
INSTANCE_IN_TEXT = re.compile(r"\bi-[0-9a-f]{8,17}\b")

accounts = st.from_regex(r"[0-9]{12}", fullmatch=True)
instance_ids = st.from_regex(r"i-[0-9a-f]{8,17}", fullmatch=True)
eu_regions = st.from_regex(r"eu-(north|west|central|south)-[1-9]", fullmatch=True)
non_eu_regions = st.from_regex(r"[a-z]{2,4}-[a-z]{4,9}-[1-9]", fullmatch=True).filter(
    lambda r: not r.startswith("eu-")) | st.sampled_from(["us-east-1", "ap-south-1", "me-central-1", "eusc-de-east-1",
                                                          "eu-*", "*", ""])
profile_ids = st.from_regex(r"[a-z0-9]{12}", fullmatch=True)


def haiku_arn(region, model=HAIKU_MODEL_ID):
    return f"arn:aws:bedrock:{region}::foundation-model/{model}"


def app_arn(account, pid, region="eu-north-1", kind="application-inference-profile"):
    return f"arn:aws:bedrock:{region}:{account}:{kind}/{pid}"


haiku_lists = st.lists(eu_regions.map(haiku_arn), min_size=1, max_size=6)


@st.composite
def instance_inputs(draw):
    account = draw(accounts)
    names = draw(st.lists(st.sampled_from(PROFILE_NAMES), unique=True, max_size=2))
    profiles = {n: app_arn(account, draw(profile_ids)) for n in names}
    return account, profiles, draw(haiku_lists)


def statements(doc):
    return doc["Statement"]


def as_list(value):
    return value if isinstance(value, list) else [value]


def all_policies(account, profiles, models, instance):
    return {
        "instance": instance_role.instance_policy(account, profiles, models),
        "scheduler": scheduler_role.scheduler_policy(account, instance),
        "deploy": deploy_role.deploy_policy(account, instance),
    }


# --- Least privilege ---------------------------------------------------------------------------
@given(instance_inputs(), instance_ids)
def test_no_wildcard_actions_and_resource_star_only_for_metrics(inputs, instance):
    account, profiles, models = inputs
    for name, doc in all_policies(account, profiles, models, instance).items():
        for st_ in statements(doc):
            assert st_["Effect"] == "Allow"
            assert not {"NotAction", "NotResource", "NotPrincipal"} & set(st_)
            assert all("*" not in a and "?" not in a for a in as_list(st_["Action"])), (name, st_)
            if "*" in as_list(st_["Resource"]):
                assert (name, st_["Sid"]) == ("instance", "PutPiMetrics")
                assert st_["Condition"] == {"StringEquals": {"cloudwatch:namespace": "PriceIntelligence"}}


@given(instance_inputs(), instance_ids)
def test_every_resource_is_scoped(inputs, instance):
    """Each ARN is this account in eu-north-1 (or S3, which has neither), or an EU Haiku/DeepSeek
    foundation model; wildcards only in the documented places."""
    account, profiles, models = inputs
    bucket = f"arn:aws:s3:::price-intelligence-{account}"
    allowed_wild = {"*", f"{bucket}/*", f"{bucket}/releases/*"}
    secret = f"arn:aws:secretsmanager:eu-north-1:{account}:secret:prod/wayakit-app-??????"
    for doc in all_policies(account, profiles, models, instance).values():
        for st_ in statements(doc):
            for r in as_list(st_["Resource"]):
                assert "*" not in r or r in allowed_wild, r
                assert "?" not in r or r == secret, r
                if r == "*":
                    continue
                if r.startswith("arn:aws:s3:::"):
                    assert r == bucket or r.startswith(bucket + "/")
                elif "::foundation-model/" in r:
                    assert r == "arn:aws:bedrock:eu-north-1::foundation-model/deepseek.v3.2" or (
                        r in models and r.startswith("arn:aws:bedrock:eu-"))
                else:
                    assert r.startswith("arn:aws:") and r.split(":")[3:5] == ["eu-north-1", account], r


@given(instance_inputs())
def test_haiku_models_only_through_haiku_profiles(inputs):
    account, profiles, models = inputs
    doc = instance_role.instance_policy(account, profiles, models)
    haiku = [s for s in statements(doc) if any(HAIKU_MODEL_ID in r and "foundation-model" in r
                                               for r in as_list(s["Resource"]))]
    assert [s["Sid"] for s in haiku] == ["InvokeHaikuThroughProfiles"]
    allowed = haiku[0]["Condition"]["StringEquals"]["bedrock:InferenceProfileArn"]
    expected = {f"arn:aws:bedrock:eu-north-1:{account}:inference-profile/eu.{HAIKU_MODEL_ID}"}
    if "price-intelligence-haiku-4-5" in profiles:
        expected.add(profiles["price-intelligence-haiku-4-5"])
    assert set(allowed) == expected


@given(instance_inputs(), st.data())
def test_instance_policy_ignores_model_order_and_repetition(inputs, data):
    account, profiles, models = inputs
    shuffled = data.draw(st.permutations(models))
    repeated = shuffled + data.draw(st.lists(st.sampled_from(models), max_size=5))
    base = instance_role.instance_policy(account, profiles, models)
    assert instance_role.instance_policy(account, profiles, repeated) == base
    assert instance_role.instance_policy(account, dict(reversed(profiles.items())), models) == base


@given(instance_inputs(), instance_ids)
def test_documents_are_deterministic_and_round_trip_as_json(inputs, instance):
    account, profiles, models = inputs
    first = all_policies(account, profiles, models, instance)
    second = all_policies(account, profiles, models, instance)
    for name in first:
        text = _aws.policy_json(first[name])
        assert text == _aws.policy_json(second[name])
        assert _aws.policies_equal(json.loads(text), first[name])
        assert instance_role.as_policy(text) == first[name]
        assert len(text) <= 10240


# --- Validation --------------------------------------------------------------------------------
@given(instance_inputs(), non_eu_regions, st.data())
def test_instance_policy_rejects_non_eu_haiku_models(inputs, region, data):
    account, profiles, models = inputs
    at = data.draw(st.integers(0, len(models)))
    with pytest.raises(ValueError):
        instance_role.instance_policy(account, profiles, models[:at] + [haiku_arn(region)] + models[at:])


@given(instance_inputs(), eu_regions, st.text(min_size=1, max_size=40))
@example((ACCOUNT, {}, [haiku_arn("eu-west-1")]), "eu-west-1", "*")
@example((ACCOUNT, {}, [haiku_arn("eu-west-1")]), "eu-west-1", "anthropic.claude-sonnet-4-5-20250929-v1:0")
def test_instance_policy_rejects_other_models(inputs, region, model):
    assume(model != HAIKU_MODEL_ID)
    account, profiles, models = inputs
    with pytest.raises(ValueError):
        instance_role.instance_policy(account, profiles, models + [haiku_arn(region, model)])


@given(instance_inputs(), st.sampled_from(PROFILE_NAMES), profile_ids, accounts,
       st.sampled_from(["eu-north-1", "eu-west-1", "us-east-1"]),
       st.sampled_from(["application-inference-profile", "inference-profile", "foundation-model"]))
def test_instance_policy_rejects_foreign_profile_arns(inputs, name, pid, other_account, region, kind):
    account, profiles, models = inputs
    assume((other_account, region, kind) != (account, "eu-north-1", "application-inference-profile"))
    with pytest.raises(ValueError):
        instance_role.instance_policy(account, dict(profiles, **{name: app_arn(other_account, pid, region, kind)}),
                                      models)


@given(instance_inputs(), st.text(min_size=0, max_size=40), profile_ids)
def test_instance_policy_rejects_unknown_profile_names(inputs, name, pid):
    assume(name not in PROFILE_NAMES)
    account, profiles, models = inputs
    with pytest.raises(ValueError):
        instance_role.instance_policy(account, dict(profiles, **{name: app_arn(account, pid)}), models)


@given(accounts, st.one_of(st.text(max_size=30), st.none(), st.integers(), instance_ids.map(lambda i: i + "\n"),
                           instance_ids.map(lambda i: " " + i), instance_ids.map(lambda i: i.upper())))
@example(ACCOUNT, "*")
@example(ACCOUNT, "i-*")
@example(ACCOUNT, "")
def test_instance_scoped_builders_reject_non_instance_ids(account, value):
    assume(not (isinstance(value, str) and INSTANCE_RE.fullmatch(value)))
    with pytest.raises(ValueError):
        scheduler_role.scheduler_policy(account, value)
    with pytest.raises(ValueError):
        deploy_role.deploy_policy(account, value)


@given(accounts, instance_ids)
def test_instance_scoped_builders_point_at_that_instance_only(account, instance):
    arn = f"arn:aws:ec2:eu-north-1:{account}:instance/{instance}"
    assert [s["Resource"] for s in statements(scheduler_role.scheduler_policy(account, instance))] == [arn]
    ec2 = [s for s in statements(deploy_role.deploy_policy(account, instance))
           if any(a.startswith("ec2:") for a in as_list(s["Action"]))]
    assert [s["Resource"] for s in ec2] == [arn, arn]
    assert {s["Action"] for s in ec2} == {"ec2:StartInstances", "ec2:CreateTags"}
    tag = next(s for s in ec2 if s["Action"] == "ec2:CreateTags")
    assert tag["Condition"] == {"ForAllValues:StringEquals": {"aws:TagKeys": ["pi-mode"]}}


ACCOUNT_RE = re.compile(r"[0-9]{12}")
account_like = st.one_of(
    st.text(max_size=14), st.none(), st.integers(), st.binary(max_size=12),
    st.text(alphabet=st.characters(categories=["Nd"]), min_size=12, max_size=12),   # Unicode digits
    accounts.map(lambda a: a + "\n"), accounts.map(lambda a: " " + a), accounts.map(lambda a: a[:-1]),
    accounts.map(lambda a: a + "0"), accounts.map(lambda a: a[:6] + "*" + a[7:]))


@given(account_like, instance_ids)
@example("*", "i-0abc1234def567890")
@example("", "i-0abc1234def567890")
@example("00000000000\u0660", "i-0abc1234def567890")  # Arabic-Indic zero: `\d` would accept it
@example(int(ACCOUNT.replace("0", "1")), "i-0abc1234def567890")
def test_account_scoped_builders_reject_anything_but_twelve_ascii_digits(value, instance):
    assume(not (isinstance(value, str) and ACCOUNT_RE.fullmatch(value)))
    for build in (lambda: instance_role.instance_policy(value, {}, [haiku_arn("eu-west-1")]),
                  lambda: scheduler_role.trust_policy(value),
                  lambda: scheduler_role.scheduler_policy(value, instance),
                  lambda: deploy_role.trust_policy(value),
                  lambda: deploy_role.deploy_policy(value, instance)):
        with pytest.raises(ValueError):
            build()


# --- Trusts ------------------------------------------------------------------------------------
@given(accounts)
def test_trust_policies(account):
    deploy = deploy_role.trust_policy(account)["Statement"]
    assert len(deploy) == 1
    assert deploy[0]["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert deploy[0]["Principal"] == {
        "Federated": f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com"}
    assert deploy[0]["Condition"] == {"StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub":
            "repo:Wayakit-Biotechnology-in-Hygiene/wayakit-price-prediction:ref:refs/heads/main"}}
    sched = scheduler_role.trust_policy(account)["Statement"]
    assert sched == [{"Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"},
                      "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]
    assert instance_role.trust_policy()["Statement"][0]["Principal"] == {"Service": "ec2.amazonaws.com"}


# --- Redaction ---------------------------------------------------------------------------------
texts_with_ids = st.lists(st.one_of(st.text(max_size=20), instance_ids,
                                    instance_ids.map(lambda i: f"arn:aws:ec2:eu-north-1:{ACCOUNT}:instance/{i}"),
                                    st.sampled_from(["i-", "-", "/", "\"", " ", "\n", "i-<instance>"])),
                          max_size=10).map("".join)


@given(texts_with_ids)
@example("i-0abc1234def567890i-0abc1234def567890")
@example("xi-0abc1234def567890 i-0abc1234def567890")
def test_redact_instance_ids_leaves_none_and_is_idempotent(text):
    once = instance_role.redact_instance_ids(text)
    assert not INSTANCE_IN_TEXT.search(once)
    assert instance_role.redact_instance_ids(once) == once


@given(instance_ids, st.text(alphabet=" /:\"'=,()[]\n", max_size=3), st.text(alphabet=" /:\"'=,()[]\n", max_size=3))
def test_every_delimited_instance_id_is_redacted(instance, before, after):
    assert instance not in instance_role.redact_instance_ids(before + instance + after)


# --- Audit summary -----------------------------------------------------------------------------
OTHER_ROLE = OLD_ROLE_ARN.replace("Wayakit-EC2-Secrets-Role", "another-role")
PI_SECRETS = ["prod/wayakit-app", "prod/wayakit-app-AbC123",
              f"arn:aws:secretsmanager:eu-north-1:{ACCOUNT}:secret:prod/wayakit-app-AbC123"]
secret_ids = st.sampled_from(PI_SECRETS + ["prod/other", "prod/wayakit-app-extra", "prod/wayakit-application",
                                           "dev/wayakit-app"])
principals = st.sampled_from(["ec2.amazonaws.com", "ec2.amazonaws.com", "lambda.amazonaws.com", "ssm.amazonaws.com"])
event_names = st.sampled_from(["AssumeRole", "AssumeRole", "AssumeRoleWithWebIdentity", "GetRole", "TagRole"])
ids_pool = st.sampled_from(["i-0abc1234def567890", "i-0fedcba9876543210", "i-0123456789abcdef0", "someone@example.com"])


@st.composite
def audit_inputs(draw):
    assume_records = [assume_record(draw(ids_pool), by=draw(principals), event=draw(event_names), eid=f"a{n}")
                      for n in range(draw(st.integers(0, 6)))]
    session_records = [session_record(draw(st.sampled_from(["GetSecretValue", "GetObject"])),
                                      secret=draw(secret_ids), issuer=draw(st.sampled_from([OLD_ROLE_ARN, OTHER_ROLE])),
                                      eid=f"s{n}")
                       for n in range(draw(st.integers(0, 6)))]
    pi = draw(st.lists(ids_pool, max_size=2, unique=True))
    assoc = draw(st.lists(ids_pool, max_size=3, unique=True))
    return assume_records, session_records, pi, assoc


@given(audit_inputs())
def test_only_pi_implies_every_signal_is_pi(inputs):
    assume_records, session_records, pi, assoc = inputs
    s = instance_role.summarize_audit(OLD_ROLE_ARN, assume_records, session_records, pi, assoc)
    real_assume = [r for r in assume_records if r["eventName"].startswith("AssumeRole")]
    own = [r for r in session_records if r["userIdentity"]["sessionContext"]["sessionIssuer"]["arn"] == OLD_ROLE_ARN]
    if not real_assume or not pi:
        assert s["only_pi"] is False
    if s["only_pi"]:
        assert s["evidence"] is True
        assert all(r["requestParameters"]["roleSessionName"] in pi for r in real_assume)
        assert set(assoc) <= set(pi)
        assert all(r["userIdentity"]["invokedBy"] == "ec2.amazonaws.com" for r in real_assume)
        assert all(r["requestParameters"]["secretId"] in PI_SECRETS for r in own
                   if r["eventSource"].startswith("secretsmanager."))
    else:
        assert not real_assume or not pi or s["other_sessions"] or s["other_instances"] or \
            s["other_principals"] or s["other_secrets"]
    assert s["assume_events"] == len(real_assume)


@given(audit_inputs())
def test_records_of_other_issuers_never_count(inputs):
    assume_records, session_records, pi, assoc = inputs
    own = [r for r in session_records if r["userIdentity"]["sessionContext"]["sessionIssuer"]["arn"] == OLD_ROLE_ARN]
    with_others = instance_role.summarize_audit(OLD_ROLE_ARN, assume_records, session_records, pi, assoc)
    without = instance_role.summarize_audit(OLD_ROLE_ARN, assume_records, own, pi, assoc)
    assert with_others == without


# --- Flows on the stateful fake ----------------------------------------------------------------
role_drift = st.fixed_dictionaries({
    "trust": st.sampled_from([None, json.dumps({"Statement": []})]),
    "description": st.sampled_from([None, "old"]),
    "max": st.sampled_from([None, 7200, 43200]),
    "tags": st.sampled_from([None, [], [{"Key": "owner", "Value": "x"}]]),
    "inline": st.sampled_from([None, {}, {"legacy": "{}"}]),
    "attached": st.sampled_from([None, set(), {"arn:aws:iam::aws:policy/Extra"}]),
})


@st.composite
def drifted_accounts(draw):
    """An account where all three roles were applied, then any field of any role drifted, and
    the instance profile and OIDC provider may have lost their tags or audience. Some roles may
    also be missing."""
    aws = FakeAws()
    for module in MODULES:
        module.main([], session=aws)
    for name in (IR, SR, DR):
        if draw(st.booleans()) and draw(st.booleans()):
            del aws.roles[name]
            continue
        for key, value in draw(role_drift).items():
            if value is not None:
                aws.roles[name][key] = json.loads(json.dumps(value)) if key != "attached" else set(value)
    if draw(st.booleans()):
        aws.instance_profiles[IR] = {"roles": [] if IR not in aws.roles else draw(st.sampled_from([[], [IR]])),
                                     "tags": draw(st.sampled_from([[], [{"Key": "project",
                                                                         "Value": "price-intelligence"}]]))}
    if draw(st.booleans()):
        aws.oidc[OIDC_ARN].update(ClientIDList=draw(st.sampled_from([[], ["sts.amazonaws.com"]])),
                                  Tags=draw(st.sampled_from([[], [{"Key": "project", "Value": "voz-del-cliente"}]])))
    aws.calls.clear()
    return aws


@settings(deadline=None)  # each example runs the three scripts end to end on the fake (deterministic)
@given(drifted_accounts())
def test_dry_run_never_writes_from_any_state(aws):
    import contextlib
    import io
    before = aws.state()
    with contextlib.redirect_stdout(io.StringIO()):
        for module in MODULES:
            assert module.main(["--dry-run"], session=aws) == 0
    assert aws.writes() == []
    assert aws.state() == before


@settings(deadline=None)  # same as above
@given(drifted_accounts())
def test_apply_converges_and_the_second_apply_writes_nothing(aws):
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()) as out:
        for module in MODULES:
            assert module.main([], session=aws) == 0
        mark = len(aws.calls)
        for module in MODULES:
            assert module.main([], session=aws) == 0
    assert aws.writes(mark) == []
    for name in (IR, SR, DR):
        assert {"Key": "project", "Value": "price-intelligence"} in aws.roles[name]["tags"]
    # Extra policies are reported, never removed (a person decides).
    for name, role in aws.roles.items():
        if "legacy" in role["inline"]:
            assert f"another inline policy 'legacy'" in out.getvalue()
    assert ACCOUNT not in out.getvalue()
    for _, op, params in aws.calls:
        assert not set(OLD_ROLES) & {params.get("RoleName"), params.get("InstanceProfileName")} or \
            op.startswith(("Get", "List", "Describe", "Lookup"))
    assert HAIKU_APP in json.dumps(json.loads(aws.roles[IR]["inline"][instance_role.POLICY_NAME]))
