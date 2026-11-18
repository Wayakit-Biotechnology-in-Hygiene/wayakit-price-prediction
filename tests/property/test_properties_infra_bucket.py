"""Property tests of `infra/bucket.py`: the comparison helpers and the apply loop.

Rules checked over generated inputs:
- every setting matches itself and `diff_settings(desired, desired) == []`, for any account;
- equivalent AWS shapes (rule and statement order, empty or legacy lifecycle Filter, string vs
  one-element list in the policy, AWS omissions in encryption, foreign tags) never cause a write,
  and real changes always do;
- `normalize_lifecycle` and `normalize_encryption` are idempotent and order-independent;
- `write_value("tags", ...)` keeps every foreign tag and sets the project tag;
- from any starting bucket state, dry-run writes nothing, one apply reaches the desired state
  and a second apply writes nothing (idempotence), keeping foreign tags.

No AWS: the apply loop runs against the stateful `FakeS3` of the unit tests.
"""
from __future__ import annotations

import copy
import json

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from infra import _aws, bucket, config
from tests.unit.test_infra_bucket import ACCOUNT, FakeS3, NAME

pytestmark = pytest.mark.property

accounts = st.text("0123456789", min_size=12, max_size=12)  # STS account IDs: 12 ASCII digits
tag_keys = st.text("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:.", min_size=1,
                   max_size=12)
tag_values = st.text("abcdefghijklmnopqrstuvwxyz0123456789-_ ", max_size=12)
tag_dicts = st.dictionaries(tag_keys, tag_values, max_size=5)


def tag_list(tags):
    return [{"Key": k, "Value": v} for k, v in tags.items()]


# --- Self-consistency ---------------------------------------------------------------------------
@given(accounts)
def test_desired_state_matches_itself(account):
    want = bucket.desired_state(account)
    for setting in bucket.SETTINGS:
        assert bucket.setting_matches(setting, want[setting], copy.deepcopy(want[setting]))
    assert bucket.diff_settings(want, copy.deepcopy(want)) == []
    # The policy only names this account's bucket.
    text = json.dumps(want["policy"])
    assert text.count(account) == 2 and f"price-intelligence-{account}" in text


# --- Lifecycle ----------------------------------------------------------------------------------
def _aws_variant(rule, variant):
    """Same rule in one of the shapes AWS may answer for a bucket-wide (Prefix "") rule."""
    rule = copy.deepcopy(rule)
    if rule["Filter"] != {"Prefix": ""}:
        return rule
    if variant == "empty":
        rule["Filter"] = {}
    elif variant == "missing":
        del rule["Filter"]
    elif variant == "legacy":
        del rule["Filter"]
        rule["Prefix"] = ""
    elif variant == "none-values":
        rule["Transitions"] = None
    return rule


lifecycle_variants = st.lists(st.sampled_from(["as-is", "empty", "missing", "legacy", "none-values"]),
                              min_size=3, max_size=3)


@given(st.permutations(bucket.lifecycle_rules()), lifecycle_variants)
def test_lifecycle_aws_shapes_never_cause_a_write(rules, variants):
    current = [_aws_variant(r, v) for r, v in zip(rules, variants)]
    assert bucket.setting_matches("lifecycle", current, bucket.lifecycle_rules())


@given(st.permutations(bucket.lifecycle_rules()), st.integers(0, 2),
       st.sampled_from(["days", "status", "prefix", "drop"]), st.integers(1, 1000))
def test_lifecycle_real_changes_always_cause_a_write(rules, index, change, days):
    current = copy.deepcopy(rules)
    rule = current[index]
    if change == "days":
        if "NoncurrentVersionExpiration" in rule:
            assume(days != config.NONCURRENT_EXPIRATION_DAYS)
            rule["NoncurrentVersionExpiration"]["NoncurrentDays"] = days
        elif "Days" in rule.get("Expiration", {}):
            assume(days != config.LOGS_EXPIRATION_DAYS)
            rule["Expiration"]["Days"] = days
        else:
            assume(days != config.ABORT_MULTIPART_DAYS)
            rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] = days
    elif change == "status":
        rule["Status"] = "Disabled"
    elif change == "prefix":
        rule["Filter"] = {"Prefix": "releases/"}
    else:
        current.pop(index)
    assert not bucket.setting_matches("lifecycle", current, bucket.lifecycle_rules())


filters = st.one_of(
    st.just({}),
    st.builds(lambda p: {"Prefix": p}, st.sampled_from(["", "logs/", "data/", "releases/"])),
    st.builds(lambda k, v: {"Tag": {"Key": k, "Value": v}}, tag_keys, tag_values),
)


@st.composite
def lifecycle_rule_lists(draw):
    """Arbitrary lifecycle rules with unique IDs (AWS requires it), in any of the shapes."""
    ids = draw(st.lists(st.text("abcdefghij-", min_size=1, max_size=8), unique=True, max_size=5))
    rules = []
    for rule_id in ids:
        rule = {"ID": rule_id, "Status": draw(st.sampled_from(["Enabled", "Disabled"]))}
        shape = draw(st.sampled_from(["filter", "legacy", "none"]))
        if shape == "filter":
            rule["Filter"] = draw(filters)
        elif shape == "legacy":
            rule["Prefix"] = draw(st.sampled_from(["", "logs/", "state/"]))
        if draw(st.booleans()):
            rule["Expiration"] = {"Days": draw(st.integers(1, 3650))}
        if draw(st.booleans()):
            rule["NoncurrentVersionExpiration"] = None
        rules.append(rule)
    return rules


@given(lifecycle_rule_lists(), st.randoms(use_true_random=False))
def test_normalize_lifecycle_idempotent_and_order_independent(rules, rnd):
    once = bucket.normalize_lifecycle(rules)
    assert bucket.normalize_lifecycle(once) == once
    shuffled = copy.deepcopy(rules)
    rnd.shuffle(shuffled)
    assert bucket.normalize_lifecycle(shuffled) == once
    for rule in once:
        assert "Prefix" not in rule and rule["Filter"]
        assert None not in rule.values()


@given(lifecycle_rule_lists())
def test_normalize_lifecycle_does_not_mutate_its_input(rules):
    before = copy.deepcopy(rules)
    bucket.normalize_lifecycle(rules)
    assert rules == before


# --- Encryption ---------------------------------------------------------------------------------
encryption_rules_st = st.lists(
    st.fixed_dictionaries(
        {"ApplyServerSideEncryptionByDefault": st.fixed_dictionaries(
            {"SSEAlgorithm": st.sampled_from(["AES256", "aws:kms", "aws:kms:dsse"])})},
        optional={
            "BucketKeyEnabled": st.booleans(),
            "BlockedEncryptionTypes": st.fixed_dictionaries({"EncryptionType": st.lists(
                st.sampled_from(["SSE-C", "NONE"]), max_size=2)}),
        }),
    max_size=3)


@given(encryption_rules_st, st.randoms(use_true_random=False))
def test_normalize_encryption_order_independent(rules, rnd):
    shuffled = copy.deepcopy(rules)
    rnd.shuffle(shuffled)
    assert bucket.normalize_encryption(shuffled) == bucket.normalize_encryption(rules)


@given(encryption_rules_st)
@example(rules=[{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                 "BlockedEncryptionTypes": {"EncryptionType": ["SSE-C"]}}])  # used to raise
def test_normalize_encryption_idempotent(rules):
    once = bucket.normalize_encryption(rules)
    assert bucket.normalize_encryption(once) == once


@given(st.booleans(), st.booleans())
@example(with_false_key=True, with_none=True)
def test_encryption_aws_omissions_never_cause_a_write(with_false_key, with_none):
    """`BucketKeyEnabled: false` == absent and `NONE` in blocked types == nothing blocked."""
    base = {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
    variant = dict(base)
    if with_false_key:
        variant["BucketKeyEnabled"] = False
    if with_none:
        variant["BlockedEncryptionTypes"] = {"EncryptionType": ["NONE"]}
    assert bucket.setting_matches("encryption", [variant], [base])
    assert not bucket.setting_matches("encryption", [variant], bucket.encryption_rules())


# --- Policy -------------------------------------------------------------------------------------
@given(accounts, st.booleans(), st.booleans(), st.booleans())
def test_policy_equivalent_shapes_never_cause_a_write(account, reverse, action_list, as_text):
    want = bucket.bucket_policy(account)
    current = copy.deepcopy(want)
    statement = current["Statement"][0]
    if reverse:
        statement["Resource"].reverse()
    if action_list:
        statement["Action"] = [statement["Action"]]
    if as_text:
        current = json.loads(json.dumps(current, indent=2))
    assert bucket.setting_matches("policy", current, want)


@given(accounts, accounts)
def test_policy_of_another_account_causes_a_write(account, other):
    assume(account != other)
    assert not bucket.setting_matches("policy", bucket.bucket_policy(other),
                                      bucket.bucket_policy(account))


# --- Tags ---------------------------------------------------------------------------------------
@given(tag_dicts)
def test_write_value_tags_keeps_foreign_and_sets_project(current):
    desired = _aws.project_tags()
    written = _aws.tags_to_dict(bucket.write_value("tags", tag_list(current), desired))
    for key, value in current.items():
        if key != config.PROJECT_TAG_KEY:
            assert written[key] == value
    assert written[config.PROJECT_TAG_KEY] == config.PROJECT_TAG_VALUE
    assert set(written) == set(current) | {config.PROJECT_TAG_KEY}
    assert bucket.setting_matches("tags", tag_list(written), desired)


# --- Apply loop over any starting state ---------------------------------------------------------
def _broken(setting):
    return {
        "ownership": st.sampled_from(["ObjectWriter", "BucketOwnerPreferred"]),
        "public_access_block": st.fixed_dictionaries(
            {k: st.booleans() for k in bucket.PUBLIC_ACCESS_BLOCK}),
        "encryption": encryption_rules_st.filter(bool),
        "versioning": st.sampled_from(["Enabled", "Suspended"]),
        "policy": st.just({"Version": "2012-10-17", "Statement": [{
            "Effect": "Deny", "Principal": "*", "Action": "s3:DeleteBucket",
            "Resource": config.bucket_arn(ACCOUNT)}]}),
        "lifecycle": lifecycle_rule_lists().filter(bool),
        "tags": tag_dicts.filter(bool).map(tag_list),
    }[setting]


@st.composite
def bucket_states(draw):
    """Each setting: not configured, already as desired, or something else."""
    want = bucket.desired_state(ACCOUNT)
    state = {}
    for setting in bucket.SETTINGS:
        kind = draw(st.sampled_from(["unset", "desired", "other"]))
        if kind == "desired":
            state[setting] = copy.deepcopy(want[setting])
        elif kind == "other":
            state[setting] = draw(_broken(setting))
        elif setting == "ownership":
            state[setting] = None  # buckets with ACLs enabled and no ownership controls
    return state


@given(st.one_of(st.none(), bucket_states()), st.booleans())
@example(state=None, empty_filter=True)
def test_apply_reaches_the_desired_state_and_a_second_apply_writes_nothing(state, empty_filter):
    s3 = FakeS3(state, exists=state is not None, empty_filter=empty_filter)
    foreign = {k: v for k, v in _aws.tags_to_dict((state or {}).get("tags")).items()
               if k != config.PROJECT_TAG_KEY}

    planned = bucket.ensure_bucket(s3, ACCOUNT, dry_run=True)
    assert s3.mutating_calls() == []

    s3.calls.clear()
    assert bucket.ensure_bucket(s3, ACCOUNT) == planned
    writes = s3.mutating_calls()
    assert len(writes) == len(planned)  # one write per planned action, nothing else
    assert bucket.diff_settings(bucket.read_state(s3, NAME, ACCOUNT),
                                bucket.desired_state(ACCOUNT)) == []
    tags = _aws.tags_to_dict(s3.state["tags"])
    assert all(tags[k] == v for k, v in foreign.items())

    s3.calls.clear()
    assert bucket.ensure_bucket(s3, ACCOUNT) == []
    assert s3.mutating_calls() == []
