"""Property tests of the pure parts of `infra/check_pi.py`.

- `wildcard_match` is equal to an obvious reference matcher (dynamic programming over `*` and
  `?`, everything else literal) and every literal pattern matches itself, regex characters
  included.
- `allows` is invariant under statement order and under wrapping a single string into a
  one-element list; adding an Allow never removes a grant; adding a matching Deny without a
  condition always removes it.
- `broad_grants` flags every Allow with a wildcard action and never a Deny.
- `redact` never leaves the account or the instance ID; `summary` and `has_failures` agree.
- Check and apply agree for any account and instance: what `instance_role`, `scheduler_role`,
  `deploy_role` and `schedule` build passes with no FAIL, and any other cron or time zone fails.
- Secret pattern (`prod/wayakit-app-??????`): it matches the secret's ARN for exactly the
  6-character suffixes, never a secret whose name only starts with PI's, a look-alike or another
  account; `prod/wayakit-app-*` and `prod/*` fail "least privilege".
- Any Allow action outside the spec (any PI role, inline or customer-managed) is a FAIL and is
  named in the "actions" line.

No AWS: only pure functions. Accounts and instance IDs are generated, never real.
"""
from __future__ import annotations

import copy

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from infra import check_pi, config, deploy_role, instance_role, schedule, scheduler_role

pytestmark = pytest.mark.property

# [0-9], not \d: \d also matches non-ASCII digits, which no account ID has (check_account rejects them).
accounts = st.from_regex(r"[0-9]{12}", fullmatch=True)
instances = st.from_regex(r"i-[0-9a-f]{17}", fullmatch=True)
PATTERN_ALPHABET = "ab*?/:.(+[\\"
VALUE_ALPHABET = "ab/:.(+[\\"
patterns = st.text(alphabet=PATTERN_ALPHABET, max_size=8)
values = st.text(alphabet=VALUE_ALPHABET, max_size=8)


def reference_match(pattern, value):
    """Glob match with only `*` (any run) and `?` (one character); DP, no regex."""
    n, m = len(pattern), len(value)
    dp = [[False] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = True
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] and pattern[i - 1] == "*"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            p = pattern[i - 1]
            if p == "*":
                dp[i][j] = dp[i - 1][j] or dp[i][j - 1]
            else:
                dp[i][j] = dp[i - 1][j - 1] and (p == "?" or p == value[j - 1])
    return dp[n][m]


@given(patterns, values)
@example("a*", "a/b:c")
@example("?", "")
@example("a.b", "axb")
def test_wildcard_match_equals_reference(pattern, value):
    assert check_pi.wildcard_match(pattern, value) is reference_match(pattern, value)


@given(st.text(max_size=20).filter(lambda t: "*" not in t and "?" not in t))
@example(".+()[]{}^$|\\")
@example("a\nb")
def test_literal_pattern_matches_itself(text):
    assert check_pi.wildcard_match(text, text)


@given(st.text(alphabet="abAB*?:", max_size=8), st.text(alphabet="abAB:", max_size=8))
def test_ignore_case_is_matching_on_lowercase(pattern, value):
    assert check_pi.wildcard_match(pattern, value, ignore_case=True) is check_pi.wildcard_match(
        pattern.lower(), value.lower())


# --- allows ------------------------------------------------------------------------------------
ACTIONS = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "sns:Publish", "ec2:StartInstances",
           "ec2:StopInstances", "cloudwatch:PutMetricData"]
ACTION_PATTERNS = ACTIONS + ["s3:*", "*", "ec2:Start*", "S3:GETOBJECT", "s3:?etObject"]
RESOURCES = ["arn:aws:s3:::b", "arn:aws:s3:::b/data/x", "arn:aws:s3:::other/x",
             "arn:aws:sns:eu-north-1:000000000000:t", "arn:aws:ec2:eu-north-1:000000000000:instance/i-1"]
RESOURCE_PATTERNS = RESOURCES + ["*", "arn:aws:s3:::b/*", "arn:aws:s3:::*", "arn:aws:ec2:*:*:instance/*"]


def one_or_list(items):
    return st.one_of(st.sampled_from(items), st.lists(st.sampled_from(items), min_size=1, max_size=3))


@st.composite
def statement(draw, effect=None):
    s = {"Effect": effect or draw(st.sampled_from(["Allow", "Deny"]))}
    s["NotAction" if draw(st.integers(0, 9)) == 0 else "Action"] = draw(one_or_list(ACTION_PATTERNS))
    s["NotResource" if draw(st.integers(0, 9)) == 0 else "Resource"] = draw(one_or_list(RESOURCE_PATTERNS))
    if draw(st.booleans()):
        s["Condition"] = {"Bool": {"aws:SecureTransport": draw(st.sampled_from(["true", "false"]))}}
    return s


stmt_lists = st.lists(statement(), max_size=6)
probes = st.tuples(st.sampled_from(ACTIONS), st.sampled_from(RESOURCES))


@given(stmt_lists, probes, st.randoms(use_true_random=False))
def test_allows_does_not_depend_on_statement_order(stmts, probe, rnd):
    shuffled = list(stmts)
    rnd.shuffle(shuffled)
    assert check_pi.allows(stmts, *probe) is check_pi.allows(shuffled, *probe)


def _wrap_strings(stmt):
    out = dict(stmt)
    for key in ("Action", "NotAction", "Resource", "NotResource"):
        if isinstance(out.get(key), str):
            out[key] = [out[key]]
    return out


@given(stmt_lists, probes)
def test_allows_single_string_equals_one_element_list(stmts, probe):
    assert check_pi.allows(stmts, *probe) is check_pi.allows([_wrap_strings(s) for s in stmts], *probe)


@given(stmt_lists, statement(effect="Allow"), probes)
def test_adding_an_allow_never_removes_a_grant(stmts, extra, probe):
    if check_pi.allows(stmts, *probe):
        assert check_pi.allows(stmts + [extra], *probe)


@given(stmt_lists, probes)
def test_matching_unconditional_deny_always_wins(stmts, probe):
    action, resource = probe
    deny = {"Effect": "Deny", "Action": action, "Resource": resource}
    assert not check_pi.allows(stmts + [deny], action, resource)
    assert not check_pi.allows([deny] + stmts, action, resource)


@given(stmt_lists)
def test_broad_grants_flags_every_wildcard_allow_and_no_deny(stmts):
    problems = check_pi.broad_grants(stmts)
    for i, s in enumerate(stmts):
        actions = s.get("Action") or []
        actions = [actions] if isinstance(actions, str) else actions
        mentioned = [p for p in problems if f"statement #{i} " in p]
        if s["Effect"] == "Deny":
            assert mentioned == []
        elif any("*" in a or "?" in a for a in actions) or "NotAction" in s or "NotResource" in s:
            assert mentioned


# --- Results -----------------------------------------------------------------------------------
findings = st.lists(st.builds(check_pi.Finding, st.sampled_from(["OK", "WARN", "FAIL"]),
                              st.text(max_size=5), st.text(max_size=5)), max_size=10)


@given(findings)
def test_summary_and_has_failures_agree(fs):
    ok, warn, fail = (int(part.split()[0]) for part in check_pi.summary(fs).split(", "))
    assert ok + warn + fail == len(fs)
    assert check_pi.has_failures(fs) is (fail > 0)


@given(st.text(alphabet="0123456789i-abcdef <>:/", max_size=60), accounts, instances)
@example("000000000000000000000000", "000000000000", "i-00000000000000000")
def test_redact_never_leaves_the_account_or_instance(text, account, instance_id):
    text = f"{text}{account}{text}{instance_id}{text}"
    out = check_pi.redact(text, account, instance_id)
    assert account not in out and instance_id not in out


# --- Check and apply agree for any account and instance ----------------------------------------
EU_REGIONS = ["eu-central-1", "eu-north-1", "eu-south-1", "eu-west-1", "eu-west-3"]


@st.composite
def instance_inputs(draw):
    account = draw(accounts)
    regions = draw(st.lists(st.sampled_from(EU_REGIONS), min_size=1, max_size=5, unique=True))
    models = tuple(config.foundation_model_arn(config.HAIKU_MODEL_ID, r) for r in regions)
    prefix = f"arn:aws:bedrock:{config.REGION}:{account}:application-inference-profile/"
    names = draw(st.lists(st.sampled_from([config.DEEPSEEK_PROFILE, config.HAIKU_PROFILE]), unique=True))
    profiles = {n: prefix + draw(st.from_regex(r"[a-z0-9]{12}", fullmatch=True)) for n in names}
    return account, profiles, models


@given(instance_inputs())
def test_instance_role_as_built_passes(inputs):
    account, profiles, models = inputs
    policy = instance_role.instance_policy(account, profiles, models)
    state = check_pi.RoleState(config.INSTANCE_ROLE, instance_role.trust_policy(), [config.PROJECT_TAG],
                               [config.SSM_MANAGED_POLICY_ARN], {"p": policy})
    exp = check_pi.instance_role_expectation(account, tuple(profiles.values()), models)
    out = check_pi.evaluate_role(state, exp, check_pi.evaluate_service_trust(
        "role", state.trust, "ec2.amazonaws.com"))
    assert [f for f in out if f.status == "FAIL"] == []
    # The only possible WARN is the skipped profile probes when no application profile exists.
    assert [f.label for f in out if f.status == "WARN"] == ([] if profiles else
                                                             [f"role {config.INSTANCE_ROLE} probes skipped"])


@given(accounts, instances)
def test_scheduler_and_deploy_roles_as_built_pass(account, instance_id):
    # check_pi's look-alike "other instance" is i- plus 17 zeros: the real instance cannot be it.
    assume(instance_id != check_pi.OTHER_INSTANCE_ID)
    sched = check_pi.RoleState(config.SCHEDULER_ROLE, scheduler_role.trust_policy(account), [config.PROJECT_TAG],
                               [], {"p": scheduler_role.scheduler_policy(account, instance_id)})
    out = check_pi.evaluate_role(sched, check_pi.scheduler_role_expectation(account, instance_id),
                                 check_pi.evaluate_service_trust("role", sched.trust, "scheduler.amazonaws.com",
                                                                 account))
    assert all(f.status == "OK" for f in out)
    dep = check_pi.RoleState(config.DEPLOY_ROLE, deploy_role.trust_policy(account), [config.PROJECT_TAG], [],
                             {"p": deploy_role.deploy_policy(account, instance_id)})
    out = check_pi.evaluate_role(dep, check_pi.deploy_role_expectation(account, instance_id),
                                 check_pi.evaluate_deploy_trust(dep.trust, account))
    assert all(f.status == "OK" for f in out)


@given(accounts, instances, accounts)
def test_scheduler_trust_for_another_account_fails(account, instance_id, other):
    assume(other != account)
    out = check_pi.evaluate_service_trust("role", scheduler_role.trust_policy(other), "scheduler.amazonaws.com",
                                          account)
    assert [f.status for f in out] == ["FAIL"]


@given(accounts, instances, st.sampled_from(["ENABLED", "DISABLED"]))
def test_schedule_as_built_has_no_fail_and_warns_only_when_disabled(account, instance_id, state):
    out = check_pi.evaluate_schedule(schedule.desired_schedule(account, instance_id, state=state), account,
                                     instance_id)
    assert [f for f in out if f.status == "FAIL"] == []
    assert [f.label for f in out if f.status == "WARN"] == (
        [] if state == "ENABLED" else [f"schedule {config.SCHEDULE_NAME} state"])


@given(accounts, instances, st.text(max_size=30), st.sampled_from(["", "UTC", "America/Monterrey"]))
@example("000000000000", "i-00000000000000000", "cron(50 21 ? * 5L *)", "UTC")
def test_any_other_cron_or_timezone_fails(account, instance_id, cron, tz):
    desired = copy.deepcopy(schedule.desired_schedule(account, instance_id, state="ENABLED"))
    desired["ScheduleExpression"] = cron
    desired["ScheduleExpressionTimezone"] = tz or config.SCHEDULE_TIMEZONE
    assume(cron != config.SCHEDULE_CRON or (tz or config.SCHEDULE_TIMEZONE) != config.SCHEDULE_TIMEZONE)
    out = check_pi.evaluate_schedule(desired, account, instance_id)
    assert [f.label for f in out if f.status == "FAIL"] == [f"schedule {config.SCHEDULE_NAME}"]


principal_values = st.lists(st.sampled_from(["*", "arn:aws:iam::000000000000:root", "000000000000", ""]),
                            max_size=4)


@given(principal_values)
@example(["*"])
def test_is_everyone_list_and_string_forms_agree(values):
    """`{"AWS": [...]}` covers everyone iff it lists "*"; a one-element list means the same as the string."""
    assert check_pi.is_everyone({"AWS": values}) is ("*" in values)
    if len(values) == 1:
        assert check_pi.is_everyone({"AWS": values[0]}) is check_pi.is_everyone({"AWS": values})


@given(accounts)
def test_lookalike_buckets_are_never_pi_bucket(account):
    name = config.bucket_name(account)
    others = check_pi.lookalike_buckets(account)
    assert name not in others
    assert len(set(others)) == len(others)


# --- secret pattern and extra actions (F3 follow-up D) ------------------------------------------
SUFFIX_CHARS = st.characters(min_codepoint=33, max_codepoint=126)  # printable ASCII, no blank
HAIKU_HERE = (config.foundation_model_arn(config.HAIKU_MODEL_ID),)  # the Haiku model in REGION


@given(accounts, st.text(SUFFIX_CHARS, max_size=12))
@example("000000000000", "AbCdEf")
@example("000000000000", "AbCdE")
@example("000000000000", "AbCdEfG")
@example("000000000000", "other-AbCdEf")
def test_secret_pattern_matches_exactly_the_six_character_suffixes(account, suffix):
    pattern = config.secret_arn_pattern(account)
    arn = f"arn:aws:secretsmanager:eu-north-1:{account}:secret:{config.SECRET_NAME}-{suffix}"
    assert check_pi.wildcard_match(pattern, arn) is (len(suffix) == config.SECRET_ARN_SUFFIX_LENGTH == 6)


@given(accounts, st.text(st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="*?"),
                         min_size=1, max_size=12))
@example("000000000000", "other")
def test_no_secret_whose_name_only_starts_with_pi_name_matches(account, rest):
    """`prod/wayakit-app-<rest>-<6 chars>` is another secret: the pattern never reaches it."""
    pattern = config.secret_arn_pattern(account)
    other = check_pi.secret_arn(account, f"{config.SECRET_NAME}-{rest}")
    assert not check_pi.wildcard_match(pattern, other)


@given(accounts, accounts)
def test_secret_probe_matches_the_pattern_and_no_lookalike_or_other_account_does(account, other_account):
    pattern = config.secret_arn_pattern(account)
    assert check_pi.wildcard_match(pattern, check_pi.secret_arn(account))
    for lookalike in check_pi.lookalike_secrets(account):
        assert not check_pi.wildcard_match(pattern, lookalike), lookalike
    if other_account != account:
        assert not check_pi.wildcard_match(pattern, check_pi.secret_arn(other_account))


@given(accounts)
def test_secret_name_prefix_wildcards_fail_least_privilege(account):
    for resource in (f"arn:aws:secretsmanager:eu-north-1:{account}:secret:{config.SECRET_NAME}-*",
                     f"arn:aws:secretsmanager:eu-north-1:{account}:secret:prod/*"):
        policy = copy.deepcopy(instance_role.instance_policy(account, {}, HAIKU_HERE))
        for s in policy["Statement"]:
            if s["Sid"] == "ReadPiSecret":
                s["Resource"] = resource
        state = check_pi.RoleState(config.INSTANCE_ROLE, instance_role.trust_policy(), [config.PROJECT_TAG],
                                   [config.SSM_MANAGED_POLICY_ARN], {"p": policy})
        out = check_pi.evaluate_role(state, check_pi.instance_role_expectation(account, (), HAIKU_HERE), [])
        assert [f.label for f in out if f.status == "FAIL"] == [
            f"role {config.INSTANCE_ROLE} least privilege"], resource


SERVICES_FOR_EXTRA = ["ec2", "iam", "s3", "sns", "kms", "logs", "lambda", "secretsmanager", "bedrock"]
VERBS = ["Get", "Put", "Delete", "Create", "Describe", "List", "Update", "Invoke", "Stop", "Attach"]
NOUNS = ["Object", "Instances", "RolePolicy", "Topic", "Key", "Function", "SecretValue", "Model",
         "BucketPolicy", "Tags"]


@given(accounts, instances, st.sampled_from(["instance", "scheduler", "deploy"]),
       st.lists(st.tuples(st.sampled_from(SERVICES_FOR_EXTRA), st.sampled_from(VERBS),
                          st.sampled_from(NOUNS)), min_size=1, max_size=3),
       st.booleans())
def test_any_extra_allowed_action_is_a_failure(account, instance_id, role, parts, managed):
    assume(instance_id != check_pi.OTHER_INSTANCE_ID)
    if role == "instance":
        exp = check_pi.instance_role_expectation(account, (), HAIKU_HERE)
        state = check_pi.RoleState(config.INSTANCE_ROLE, instance_role.trust_policy(), [config.PROJECT_TAG],
                                   [config.SSM_MANAGED_POLICY_ARN],
                                   {"p": instance_role.instance_policy(account, {}, HAIKU_HERE)})
    elif role == "scheduler":
        exp = check_pi.scheduler_role_expectation(account, instance_id)
        state = check_pi.RoleState(config.SCHEDULER_ROLE, scheduler_role.trust_policy(account),
                                   [config.PROJECT_TAG], [],
                                   {"p": scheduler_role.scheduler_policy(account, instance_id)})
    else:
        exp = check_pi.deploy_role_expectation(account, instance_id)
        state = check_pi.RoleState(config.DEPLOY_ROLE, deploy_role.trust_policy(account), [config.PROJECT_TAG],
                                   [], {"p": deploy_role.deploy_policy(account, instance_id)})
    actions = sorted({f"{svc}:{verb}{noun}" for svc, verb, noun in parts})
    extra = [a for a in actions if a.lower() not in {e.lower() for e in exp.expected_actions}]
    assume(extra)
    stmt = {"Effect": "Allow", "Action": actions, "Resource": config.instance_arn(account, instance_id)}
    if managed:
        arn = f"arn:aws:iam::{account}:policy/pi-extra"
        state.attached.append(arn)
        state.documents[arn] = {"Version": "2012-10-17", "Statement": [stmt]}
    else:
        state.documents["p"]["Statement"].append(stmt)
    out = check_pi.evaluate_role(state, exp, [])
    assert check_pi.has_failures(out)
    line = next(f for f in out if f.label == f"role {state.name} actions")
    assert line.status == "FAIL"
    assert all(a.lower() in line.detail for a in extra), (line.detail, extra)
