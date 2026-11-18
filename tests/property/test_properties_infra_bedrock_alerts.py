"""Property tests of `infra/bedrock_profiles.py` and `infra/alerts.py`.

- Pure helpers: `source_matches`, `regions_of`, `env_assignments`, `attribute_changes`
  (idempotence), `find_email_subscription` (case and space invariance, email protocol only) and
  `normalize_email` (trimmed or ValueError without the input).
- Whole scripts on stateful in-memory fakes (no AWS), from any starting state:
  - dry-run makes only read calls and changes nothing;
  - a second apply makes no change at all (idempotency) and leaves the project tag everywhere;
  - alerts never creates a second email subscription for the same address and never prints the
    address in full; neither script ever prints the account ID.
"""
from __future__ import annotations

import contextlib
import io

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from infra import alerts, bedrock_profiles as bp, config
from tests.unit._fake_infra_bedrock_alerts import (
    ACCOUNT, ExplodingSession, FakeBedrock, FakeSNS, FakeSTS, FakeSession, deepseek_models, haiku_models,
    mutating)

pytestmark = pytest.mark.property

ENV_KEYS = {"PI_RELEVANCE_PRIMARY_MODEL", "PI_RELEVANCE_FALLBACK_MODEL"}
regions = st.sampled_from(["eu-north-1", "eu-west-1", "eu-west-3", "eu-central-1", "eu-south-2", "us-east-1"])
model_ids = st.sampled_from([config.DEEPSEEK_MODEL_ID, config.HAIKU_MODEL_ID, "deepseek.r1",
                             "anthropic.claude-sonnet-4-5", "x-deepseek.v3.2"])
models = st.lists(st.builds(lambda m, r: {"modelArn": config.foundation_model_arn(m, r)}, model_ids, regions),
                  max_size=6)

# A local part starting with "-" is excluded: argparse reads `--email -x@...` as an option (pass
# it as `--email=-x@...`); that mistake is covered by test_alerts_cli_mistake_exits_2_without_the_address.
_local = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJ0123456789._+-", min_size=1,
                 max_size=15).filter(lambda s: not s.startswith("-"))
_label = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10)
emails = st.builds(lambda l, d, t: f"{l}@{d}.{t}", _local, _label, st.sampled_from(["com", "org", "example"]))
spaces = st.sampled_from(["", " ", "  ", "\t", "\n"])


def _run(module, argv, service, fake):
    start = len(fake.calls)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = module.main(argv, session=FakeSession(sts=FakeSTS(), **{service: fake}))
    return rc, buf.getvalue(), fake.calls[start:]


# --- bedrock_profiles: pure --------------------------------------------------------------------
@given(model_ids, st.lists(regions, min_size=1, max_size=6))
def test_source_matches_any_regions_of_the_same_model(model_id, rs):
    assert bp.source_matches(model_id, [{"modelArn": config.foundation_model_arn(model_id, r)} for r in rs])


@given(model_ids, models)
def test_source_matches_iff_every_model_is_that_model(model_id, ms):
    expected = bool(ms) and all(m["modelArn"].split("/", 1)[1] == model_id for m in ms)
    assert bp.source_matches(model_id, ms) == expected


@given(models)
def test_regions_of_is_sorted_unique_subset(ms):
    out = bp.regions_of(ms)
    assert out == sorted(set(out))
    assert set(out) == {m["modelArn"].split(":")[3] for m in ms}


@given(st.dictionaries(st.sampled_from([config.DEEPSEEK_PROFILE, config.HAIKU_PROFILE, "other", "x"]),
                       st.text(min_size=1, max_size=40)))
def test_env_assignments_always_has_the_two_keys(existing):
    env = bp.env_assignments(existing)
    assert set(env) == ENV_KEYS
    assert env["PI_RELEVANCE_PRIMARY_MODEL"] == existing.get(config.DEEPSEEK_PROFILE, config.DEEPSEEK_MODEL_ID)
    assert env["PI_RELEVANCE_FALLBACK_MODEL"] == existing.get(config.HAIKU_PROFILE, config.HAIKU_SYSTEM_PROFILE_ID)


# --- alerts: pure ------------------------------------------------------------------------------
attrs = st.dictionaries(st.sampled_from(["KmsMasterKeyId", "DisplayName", "Policy", "FifoTopic"]),
                        st.sampled_from(["alias/aws/sns", "alias/other", "", "x"]), max_size=4)


@given(attrs)
def test_attribute_changes_idempotent_and_only_about_the_key(current):
    changes = alerts.attribute_changes(current)
    assert alerts.attribute_changes({**current, **changes}) == {}
    only_key = {k: v for k, v in current.items() if k == "KmsMasterKeyId"}
    assert changes == alerts.attribute_changes(only_key)


@given(emails, st.sampled_from([str.lower, str.upper, str.title, lambda s: s]), spaces, spaces,
       st.sampled_from(["email", "email-json", "sms", "https", "sqs"]))
def test_find_email_subscription_case_space_and_protocol(addr, case, left, right, protocol):
    sub = {"Protocol": protocol, "Endpoint": f"{left}{case(addr)}{right}", "SubscriptionArn": "PendingConfirmation"}
    other = {"Protocol": "email", "Endpoint": "nobody@other.invalid", "SubscriptionArn": "x"}
    found = alerts.find_email_subscription([other, sub], f"{right}{addr}{left}")
    assert found is (sub if protocol == "email" else None)


@given(st.text(max_size=40))
@example("  someone@example.com  ")
@example("someone@example")
def test_normalize_email_trimmed_or_error_without_the_input(raw):
    try:
        out = alerts.normalize_email(raw)
    except ValueError as exc:
        # A fixed message: it cannot carry any part of the input.
        assert str(exc) == "--email is not a valid email address."
        return
    assert out == out.strip() == raw.strip()
    assert out.count("@") == 1


@given(st.text(max_size=80))
@example("unrecognized arguments: --emial someone@example.com")
@example("argument --dry-run: ignored explicit argument 'someone@example.com'")
@example("@@ a@ @b\t@\n@")
def test_redact_cli_message_no_at_idempotent_identity(message):
    out = alerts.redact_cli_message(message)
    assert "@" not in out
    assert alerts.redact_cli_message(out) == out
    if "@" not in message:
        assert out == message
    # Only "@" tokens change: the whitespace layout and the other tokens are kept as they were.
    assert out.split() == ["<redacted>" if "@" in t else t for t in message.split()]


_typos = st.sampled_from([["--emial", "{a}"], ["--email", "{a}", "{a}"], ["--dry-run={a}", "--email", "{a}"],
                          ["--email"], ["--email", "-{a}"], ["{a}"], ["--email={a}", "--bogus={a}"]])


@given(emails, _typos)
def test_alerts_cli_mistake_exits_2_without_the_address(addr, template):
    """Any command-line mistake: exit 2 before any AWS call and no "@" anywhere in the output."""
    argv = [t.format(a=addr) for t in template]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), pytest.raises(SystemExit) as exc:
        alerts.main(argv, session=ExplodingSession())
    assert exc.value.code == 2
    printed = out.getvalue() + err.getvalue()
    assert "@" not in printed and addr not in printed
    assert err.getvalue().startswith("usage: ")


# --- whole scripts on stateful fakes -----------------------------------------------------------
profile_state = st.one_of(
    st.none(),  # missing
    st.builds(lambda right, tagged: (right, tagged), st.booleans(), st.booleans()))


def _bedrock(ds, hk, reject):
    fake = FakeBedrock(reject_copy_from={config.foundation_model_arn(config.DEEPSEEK_MODEL_ID)} if reject else ())
    for name, state, good in ((config.DEEPSEEK_PROFILE, ds, deepseek_models()),
                              (config.HAIKU_PROFILE, hk, haiku_models())):
        if state is not None:
            right, tagged = state
            fake.add(name, good if right else [{"modelArn": config.foundation_model_arn("deepseek.r1")}],
                     {"project": "price-intelligence", "owner": "x"} if tagged else {"owner": "x"})
    return fake


@given(profile_state, profile_state, st.booleans())
def test_bedrock_dry_run_then_two_applies(ds, hk, reject):
    fake = _bedrock(ds, hk, reject)
    before = fake.state()
    rc0, out0, calls0 = _run(bp, ["--dry-run"], "bedrock", fake)
    assert mutating(calls0) == []
    assert fake.state() == before
    assert out0.rstrip().endswith("--dry-run: nothing was changed.")

    rc1, out1, _ = _run(bp, [], "bedrock", fake)
    after1 = fake.state()
    rc2, out2, calls2 = _run(bp, [], "bedrock", fake)
    assert fake.state() == after1
    # Only a rejected DeepSeek copy is attempted again (nothing exists to keep).
    assert all(op == "create_inference_profile" for op in mutating(calls2))
    assert mutating(calls2) == (["create_inference_profile"] if reject and ds is None else [])

    wrong = any(s is not None and not s[0] for s in (ds, hk))
    assert rc0 == rc1 == rc2 == (1 if wrong else 0)
    for p in fake.profiles.values():
        right = bp.source_matches(config.DEEPSEEK_MODEL_ID, p["models"]) or bp.source_matches(
            config.HAIKU_MODEL_ID, p["models"])
        if right:
            assert p["tags"]["project"] == "price-intelligence"
        assert p["tags"].get("owner", "x") == "x"  # other tags are never removed or changed
    for out in (out0, out1, out2):
        assert ACCOUNT not in out
        env_lines = [line for line in out.splitlines() if line.startswith("  PI_")]
        assert [line.split("=", 1)[0].strip() for line in env_lines] == [
            "PI_RELEVANCE_PRIMARY_MODEL", "PI_RELEVANCE_FALLBACK_MODEL"]


sub_states = st.lists(st.tuples(st.sampled_from(["email", "email-json", "sms"]),
                                st.sampled_from(["target", "other"]), st.booleans()), max_size=5)


@given(emails, st.booleans(), st.sampled_from([None, "alias/aws/sns", "alias/other"]), st.booleans(),
       sub_states, st.sampled_from([str.lower, str.upper, lambda s: s]), spaces)
def test_alerts_dry_run_then_two_applies(addr, exists, kms, tagged, subs, case, pad):
    assume("example.org" not in addr)
    initial = [{"Protocol": proto, "Endpoint": case(addr) if who == "target" else "user@example.org",
                "confirmed": confirmed} for proto, who, confirmed in subs]
    fake = FakeSNS(exists=exists, attributes={} if kms is None else {"KmsMasterKeyId": kms},
                   tags={"project": "price-intelligence", "owner": "x"} if tagged else {"owner": "x"},
                   subs=initial if exists else [])
    argv = ["--email", f"{pad}{addr}{pad}"]
    before = fake.state()
    rc0, out0, calls0 = _run(alerts, ["--dry-run", *argv], "sns", fake)
    assert mutating(calls0) == []
    assert fake.state() == before

    rc1, out1, _ = _run(alerts, argv, "sns", fake)
    after1 = fake.state()
    rc2, out2, calls2 = _run(alerts, argv, "sns", fake)
    assert rc0 == rc1 == rc2 == 0
    assert mutating(calls2) == []
    assert fake.state() == after1

    target = addr.strip().lower()
    email_subs = [s for s in fake.subs if s["Protocol"] == "email" and s["Endpoint"].strip().lower() == target]
    had = sum(1 for s in initial if s["Protocol"] == "email" and s["Endpoint"].strip().lower() == target)
    # Exactly one email subscription for the address unless it already had more (never a new one).
    assert len(email_subs) == (max(had, 1) if exists else 1)
    assert fake.attributes["KmsMasterKeyId"] == "alias/aws/sns"
    assert fake.tags["project"] == "price-intelligence"
    assert fake.tags.get("owner") == ("x" if exists else None)  # other tags are never removed
    for out in (out0, out1, out2):
        assert addr not in out and addr.lower() not in out.lower()
        assert ACCOUNT not in out
