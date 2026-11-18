"""Property tests of the pure builders in `infra/schedule.py`.

- Idempotence of the diff: `schedule_changes(d, d) == []` for any desired schedule, and it stays
  empty after everything AWS changes on read (compact or reformatted input JSON, the default
  `MaximumEventAgeInSeconds`, read-only fields, a missing `ActionAfterCompletion`).
- Sensitivity: any change to the cron, timezone, state, flexible window, role, target ARN,
  instance or retry attempts is reported, under the right field.
- `disabled_copy`: State always DISABLED, every other update field kept except a StartDate at
  or before `now` (F3 review: UpdateSchedule rejects a past one), only update fields, no None
  values, idempotent, input not mutated.
- `resolve_state`, `retired_action`: equal to an obvious reference over the whole domain.
- `desired_schedule` invariants (window OFF, retries 0, one instance, PI role, start-instances
  target) and `redact` never leaving the account or any instance ID (tag-resolved or previous,
  prefixes of each other, account inside an ID), idempotent, and `str(text)` with no IDs.
- `input_instance_ids` finds every ID in any JSON formatting of the input, and the update path
  of `ensure_schedule` (fake scheduler client, live and dry-run) never prints an instance ID or
  the account.
- Escaped, keyed and glued IDs (F3 follow-up): `input_instance_ids` finds every ID written with
  any mix of `\\uXXXX` escapes (either hex case) and whitespace, and nothing else (no truncated
  prefix of an ID whose tail is escaped); on plain `json.dumps` input it equals the old raw-text
  scan (word boundaries); the update diff never prints an ID that is
  escaped, used as a key, glued to other characters (`xi-...`, `i-...é`, 18+ hex) or written in
  the Description; `schedule_instance_ids` is a superset of `input_instance_ids` and finds IDs
  outside the input; too-short and uppercase tokens are not IDs.

No AWS: only pure functions. The account and instance IDs are generated, never real.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import io
import json
import re
import types

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from infra import config, schedule

INSTANCE_SHAPE_RE = re.compile(r"i-[0-9a-f]{8}")

pytestmark = pytest.mark.property

accounts = st.from_regex(r"\d{12}", fullmatch=True)
instances = st.from_regex(r"i-[0-9a-f]{17}", fullmatch=True) | st.from_regex(
    r"i-[0-9a-f]{8}", fullmatch=True)
states = st.sampled_from(["ENABLED", "DISABLED"])
dates = st.datetimes(min_value=dt.datetime(2020, 1, 1), max_value=dt.datetime(2035, 1, 1),
                     timezones=st.just(dt.timezone.utc))


@st.composite
def desired(draw):
    return schedule.desired_schedule(draw(accounts), draw(instances), state=draw(states))


@st.composite
def as_read_from_aws(draw, request):
    """What GetSchedule could return for a schedule written with `request`."""
    out = copy.deepcopy(request)
    target = out["Target"]
    parsed = json.loads(target["Input"])
    target["Input"] = json.dumps(parsed, indent=draw(st.sampled_from([None, 0, 1, 4])),
                                 separators=draw(st.sampled_from([None, (",", ":"), (", ", ": ")])))
    if draw(st.booleans()):
        target["RetryPolicy"] = dict(target["RetryPolicy"], MaximumEventAgeInSeconds=draw(
            st.integers(min_value=60, max_value=86400)))
    if draw(st.booleans()):
        out.pop("ActionAfterCompletion")
    if draw(st.booleans()):
        out.update(Arn="arn:aws:scheduler:eu-north-1:000000000000:schedule/g/n",
                   CreationDate=draw(dates), LastModificationDate=draw(dates))
    if draw(st.booleans()):
        out["ResponseMetadata"] = {"HTTPStatusCode": 200}
    return out


# --- schedule_changes --------------------------------------------------------------------------
@given(desired())
def test_no_changes_against_itself(d):
    assert schedule.schedule_changes(d, d) == []
    assert schedule.schedule_changes(copy.deepcopy(d), d) == []


@given(st.data(), desired())
def test_no_changes_after_what_aws_adds_on_read(data, d):
    current = data.draw(as_read_from_aws(d))
    assert schedule.schedule_changes(current, d) == []


MUTATIONS = {
    "cron": ("ScheduleExpression", lambda s, v: s.update(ScheduleExpression=v),
             st.sampled_from(["cron(50 21 ? * 5 *)", "cron(0 4 ? * 6 *)", "rate(1 day)",
                              "cron(50 21 ? * 5L 2027)"])),
    "timezone": ("ScheduleExpressionTimezone",
                 lambda s, v: s.update(ScheduleExpressionTimezone=v),
                 st.sampled_from(["UTC", "America/Monterrey", "Asia/Riyadh"])),
    "window": ("FlexibleTimeWindow", lambda s, v: s.update(FlexibleTimeWindow=v),
               st.integers(min_value=1, max_value=1440).map(
                   lambda m: {"Mode": "FLEXIBLE", "MaximumWindowInMinutes": m})),
    "role": ("Target", lambda s, v: s["Target"].update(RoleArn=v),
             st.sampled_from(["arn:aws:iam::000000000000:role/Wayakit-EventBridge-Role",
                              "arn:aws:iam::111111111111:role/price-intelligence-scheduler"])),
    "target": ("Target", lambda s, v: s["Target"].update(Arn=v),
               st.sampled_from(["arn:aws:scheduler:::aws-sdk:ec2:stopInstances",
                                "arn:aws:scheduler:::aws-sdk:ec2:rebootInstances"])),
    "instance": ("Target", lambda s, v: s["Target"].update(Input=json.dumps({"InstanceIds": v})),
                 st.lists(instances, min_size=0, max_size=3)),
    "retries": ("Target", lambda s, v: s["Target"].update(RetryPolicy={"MaximumRetryAttempts": v}),
                st.integers(min_value=1, max_value=185)),
}


@given(st.data(), desired(), st.sampled_from(sorted(MUTATIONS)))
def test_any_relevant_change_is_reported(data, d, which):
    field, mutate, values = MUTATIONS[which]
    current = data.draw(as_read_from_aws(d))
    value = data.draw(values)
    mutate(current, value)
    if which == "instance":
        assume(json.loads(current["Target"]["Input"]) != json.loads(d["Target"]["Input"]))
    if which == "role":
        assume(value != d["Target"]["RoleArn"])
    assert schedule.schedule_changes(current, d) == [field]


@given(desired())
def test_a_state_flip_is_reported(d):
    flipped = dict(d, State="ENABLED" if d["State"] == "DISABLED" else "DISABLED")
    assert schedule.schedule_changes(flipped, d) == ["State"]
    assert schedule.schedule_changes(d, flipped) == ["State"]


# --- disabled_copy -----------------------------------------------------------------------------
maybe = lambda strategy: st.none() | strategy  # noqa: E731  (None = AWS did not return it)

get_schedule_responses = st.fixed_dictionaries(
    {"Name": st.sampled_from(config.RETIRED_SCHEDULES), "GroupName": st.just("default"),
     "ScheduleExpression": st.sampled_from(["cron(50 21 ? * 5 *)", "rate(7 days)"]),
     "FlexibleTimeWindow": st.just({"Mode": "OFF"}),
     "Target": st.fixed_dictionaries(
         {"Arn": st.just(config.SCHEDULE_TARGET_ARN), "RoleArn": st.just("arn:aws:iam::000000000000:role/r")},
         optional={"Input": st.text(max_size=40),
                   "RetryPolicy": st.fixed_dictionaries({"MaximumRetryAttempts": st.integers(0, 185)}),
                   "DeadLetterConfig": st.just({"Arn": "arn:aws:sqs:eu-north-1:000000000000:q"})})},
    optional={"ScheduleExpressionTimezone": maybe(st.sampled_from(["UTC", "America/Mexico_City"])),
              "State": maybe(states), "Description": maybe(st.text(max_size=40)),
              "StartDate": maybe(dates), "EndDate": maybe(dates),
              "KmsKeyArn": maybe(st.just("arn:aws:kms:eu-north-1:000000000000:key/k")),
              "ActionAfterCompletion": maybe(st.sampled_from(["NONE", "DELETE"])),
              "Arn": st.just("arn:aws:scheduler:eu-north-1:000000000000:schedule/default/x"),
              "CreationDate": dates, "LastModificationDate": dates,
              "ResponseMetadata": st.just({"HTTPStatusCode": 200})})


@given(get_schedule_responses, dates)
@example({"Name": config.RETIRED_SCHEDULES[0], "GroupName": "default", "ScheduleExpression": "rate(7 days)",
          "FlexibleTimeWindow": {"Mode": "OFF"},
          "Target": {"Arn": config.SCHEDULE_TARGET_ARN, "RoleArn": "arn:aws:iam::000000000000:role/r"},
          "StartDate": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)},
         dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))  # StartDate == now: dropped
def test_disabled_copy_only_flips_the_state(current, now):
    """Everything but the state is kept; the only other change is a StartDate at or before `now`
    being left out."""
    before = copy.deepcopy(current)
    out = schedule.disabled_copy(current, now=now)
    assert current == before
    assert out["State"] == "DISABLED"
    assert set(out) <= set(schedule.UPDATE_FIELDS)
    assert None not in out.values()
    for key in schedule.UPDATE_FIELDS:
        if key == "State":
            continue
        if current.get(key) is None:
            assert key not in out
        elif key == "StartDate" and current[key] <= now:
            assert key not in out
        else:
            assert out[key] == current[key]


@given(get_schedule_responses, st.none() | dates)
def test_disabled_copy_is_idempotent(current, now):
    once = schedule.disabled_copy(current, now=now)
    assert schedule.disabled_copy(once, now=now) == once


# --- resolve_state, retired_action ---------------------------------------------------------------
@given(st.none() | st.fixed_dictionaries({}, optional={"State": states}), st.booleans(),
       st.booleans())
def test_resolve_state_matches_reference(current, enable, disable):
    if enable and disable:
        with pytest.raises(ValueError):
            schedule.resolve_state(current, enable=enable, disable=disable)
        return
    expected = ("ENABLED" if enable else "DISABLED" if disable
                else (current or {}).get("State") or "DISABLED")
    assert schedule.resolve_state(current, enable=enable, disable=disable) == expected
    if current is None and not enable:
        assert expected == "DISABLED"  # a new schedule never starts runs without --enable


@given(st.none() | st.fixed_dictionaries({}, optional={"State": maybe(states)}), st.booleans())
def test_retired_action_never_deletes_without_the_flag(current, delete):
    action = schedule.retired_action(current, delete=delete)
    if current is None:
        assert action == "missing"
    elif delete:
        assert action == "delete"
    else:
        assert action == ("already-disabled" if current.get("State") == "DISABLED" else "disable")


# --- desired_schedule, redact, instance_ids -----------------------------------------------------
@given(accounts, instances, states)
def test_desired_schedule_invariants(account, instance, state):
    d = schedule.desired_schedule(account, instance, state=state)
    assert d["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert d["Target"]["RetryPolicy"] == {"MaximumRetryAttempts": 0}
    assert d["Target"]["Arn"] == "arn:aws:scheduler:::aws-sdk:ec2:startInstances"
    assert d["Target"]["RoleArn"] == f"arn:aws:iam::{account}:role/price-intelligence-scheduler"
    assert json.loads(d["Target"]["Input"]) == {"InstanceIds": [instance]}
    assert (d["ScheduleExpression"], d["ScheduleExpressionTimezone"]) == (
        "cron(50 21 ? * 5L *)", "America/Mexico_City")
    assert (d["Name"], d["GroupName"], d["State"]) == (
        "price-intelligence-monthly", "price-intelligence", state)


@given(accounts, instances, st.lists(st.text(max_size=15), max_size=6),
       st.lists(st.integers(min_value=0, max_value=10), min_size=4, max_size=4))
@example("000000000000", "i-000000000000abcde", ["x"], [0, 0, 0, 0])  # account inside the ID
def test_redact_never_leaves_the_ids(account, instance, chunks, positions):
    pieces = list(chunks)
    tokens = (account, instance, f"arn:aws:iam::{account}:role/x", json.dumps([instance]))
    for token, position in zip(tokens, positions):
        pieces.insert(min(position, len(pieces)), token)
    out = schedule.redact(" ".join(pieces), account, instance)
    assert account not in out and instance not in out


@given(st.lists(st.lists(instances, max_size=3), max_size=4))
def test_instance_ids_flattens_every_reservation(groups):
    response = {"Reservations": [{"Instances": [{"InstanceId": i} for i in g]} for g in groups]}
    assert schedule.instance_ids(response) == [i for g in groups for i in g]


@given(accounts, instances, st.lists(instances, max_size=4),
       st.lists(st.text(max_size=15), max_size=6), st.randoms(use_true_random=False))
@example("000000000000", "i-0abc1234", ["i-0abc1234def567890"], ["x"], None)  # tag ID prefix of old
@example("000000000000", "i-0abc1234def567890", ["i-0abc1234"], ["x"], None)  # old prefix of tag ID
@example("000000000000", "i-000000000000abcde", ["i-00000000000012345"], [], None)  # account inside
def test_redact_never_leaves_any_id_and_is_idempotent(account, instance, previous, chunks, rnd):
    pieces = list(chunks)
    tokens = [account, instance, f"arn:aws:iam::{account}:role/x", json.dumps([instance]),
              *previous, json.dumps({"InstanceIds": previous})]
    for token in tokens:
        position = rnd.randint(0, len(pieces)) if rnd else len(pieces)
        pieces.insert(position, token)
    text = " ".join(pieces)
    out = schedule.redact(text, account, instance, previous)
    for value in (account, instance, *previous):
        assert value not in out
    assert schedule.redact(out, account, instance, previous) == out


@given(st.text(max_size=60) | st.integers())
def test_redact_without_ids_is_str(text):
    assert schedule.redact(text) == str(text)
    assert schedule.redact(text, None, None, ()) == str(text)


json_formats = st.fixed_dictionaries({
    "indent": st.sampled_from([None, 0, 1, 4]),
    "separators": st.sampled_from([None, (",", ":"), (", ", ": ")]),
    "ensure_ascii": st.booleans(), "sort_keys": st.booleans()})


@given(st.lists(instances, max_size=4), json_formats,
       st.dictionaries(st.sampled_from(["note", "x", "Ids"]), st.text(max_size=10), max_size=2))
def test_input_instance_ids_finds_every_id_in_any_json_format(ids, fmt, extra):
    assume(not any(INSTANCE_SHAPE_RE.search(v) for v in extra.values()))
    current = {"Target": {"Input": json.dumps({"InstanceIds": ids, **extra}, **fmt)}}
    assert schedule.input_instance_ids(current) == sorted(set(ids))


class _FakeScheduler:
    """Only what `ensure_schedule` calls; records the update instead of reaching AWS."""

    def __init__(self, current):
        self.current, self.updates = current, []

    def get_schedule(self, Name, GroupName):  # noqa: N803 (boto3 keyword names)
        return copy.deepcopy(self.current)

    def update_schedule(self, **kwargs):
        self.updates.append(kwargs)

    def create_schedule(self, **kwargs):  # pragma: no cover (the current schedule always exists)
        raise AssertionError("create_schedule must not be called when the schedule exists")


@given(accounts, instances, st.lists(instances, min_size=1, max_size=3), json_formats, states,
       st.booleans())
@example("000000000000", "i-0abc1234def567890", ["i-0fedcba9876543210"],
         {"indent": None, "separators": (",", ":"), "ensure_ascii": True, "sort_keys": False},
         "DISABLED", False)
def test_update_output_never_prints_an_instance_id(account, instance, previous, fmt, state, dry_run):
    assume(previous != [instance])  # otherwise there is no update to print
    current = schedule.desired_schedule(account, instance, state=state)
    current["Target"]["Input"] = json.dumps({"InstanceIds": previous}, **fmt)
    sch = _FakeScheduler(current)
    args = types.SimpleNamespace(enable=False, disable=False, dry_run=dry_run)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        action, new_state = schedule.ensure_schedule(sch, account, instance, args)
    out = buffer.getvalue()
    assert (action, new_state) == ("updated", state)
    assert len(sch.updates) == (0 if dry_run else 1)
    assert account not in out
    assert not INSTANCE_SHAPE_RE.search(out)
    assert "<previous-instance-id>" in out or set(previous) <= {instance}


# --- escaped, keyed and glued instance IDs (F3 follow-up C) -------------------------------------
# The ID pattern the scan used before the follow-up (word boundaries, raw text only): reference
# for plain input.
OLD_SCAN_RE = re.compile(r"\bi-[0-9a-f]{8,17}\b")
WHITESPACE = st.sampled_from(["", " ", "\n  ", "\t", "\n"])


@st.composite
def json_literal(draw, text):
    """`text` as a JSON string literal, each character written plainly or as a \\uXXXX escape
    (hex digits in either case). Always valid JSON that decodes back to `text`."""
    out = []
    for ch in text:
        if draw(st.booleans()):
            esc = f"{ord(ch):04x}"
            out.append("\\u" + (esc.upper() if draw(st.booleans()) else esc))
        else:
            out.append(json.dumps(ch)[1:-1])
    return '"' + "".join(out) + '"'


@st.composite
def escaped_input(draw, ids, extra_pairs=()):
    """A target Input (JSON text) holding `ids` under InstanceIds, written with any mix of escapes
    and whitespace, plus `extra_pairs` (key, value), also escaped at random."""
    ws = draw(WHITESPACE)
    items = [draw(json_literal(i)) for i in ids]
    members = [f'{draw(json_literal("InstanceIds"))}{ws}:{ws}[{ws}' + f",{ws}".join(items) + f"{ws}]"]
    members += [f"{draw(json_literal(k))}{ws}:{ws}{draw(json_literal(v))}" for k, v in extra_pairs]
    return "{" + ws + f",{ws}".join(members) + ws + "}"


ESCAPED_INPUTS = st.lists(instances, max_size=4).flatmap(
    lambda ids: st.tuples(st.just(ids), escaped_input(ids)))


@given(ESCAPED_INPUTS)
@example((["i-0fedcba9876543210"], r'{"InstanceIds": ["\u0069-0fedcba9876543210"]}'))  # unit case
@example((["i-0abc1234", "i-0abc1234"], r'{"InstanceIds":["\u0069\u002D0abc1234","i-0abc1234"]}'))
# Tail escaped: the raw scan stops at the escape; its truncated prefix must not be returned.
@example((["i-00000000000000000"], r'{"InstanceIds":["i-0000000000000000\u0030"]}'))
@example((["i-0abc1234def567890"], r'{"InstanceIds":["i-0abc1234\u0064ef567890"]}'))
def test_input_instance_ids_finds_escaped_ids(case):
    """Exactly the IDs in the list (sorted, no duplicates), whatever is escaped: no truncated
    prefix of an ID whose tail is escaped, nothing else extra."""
    ids, text = case
    assert json.loads(text) == {"InstanceIds": ids}
    found = schedule.input_instance_ids({"Target": {"Input": text}})
    assert found == sorted(set(ids)), (found, ids)


def test_partially_escaped_id_adds_no_truncated_prefix():
    """Bug fixed in the F3 follow-up (was a strict xfail): an escaped tail made the raw scan also
    return the truncated prefix `i-0000000000000000`."""
    text = r'{"InstanceIds":["i-0000000000000000\u0030"]}'
    assert json.loads(text) == {"InstanceIds": ["i-00000000000000000"]}
    assert schedule.input_instance_ids({"Target": {"Input": text}}) == ["i-00000000000000000"]


@given(st.lists(instances, max_size=4), json_formats,
       st.dictionaries(st.sampled_from(["note", "x", "Ids"]), st.text(max_size=10), max_size=2))
def test_input_instance_ids_equals_the_old_scan_on_plain_input(ids, fmt, extra):
    """Same results as before the follow-up for plain `json.dumps` input with no glued tokens."""
    assume(not any("i-" in v for v in extra.values()))
    text = json.dumps({"InstanceIds": ids, **extra}, **fmt)
    current = {"Target": {"Input": text}}
    assert schedule.input_instance_ids(current) == sorted(set(OLD_SCAN_RE.findall(text)))


GLUE_BEFORE = st.sampled_from(["", "x", "\u00e9", "\n", "\\", "-", "0"])
GLUE_AFTER = st.sampled_from(["", "x", "\u00e9", "\n", "a", "0", "f00", '"'])


@st.composite
def glued_update_cases(draw):
    """(previous IDs, another ID, what is glued after it, Input text): the previous IDs escaped
    at random in the list, and the other ID glued to other characters as a key and as a value."""
    previous = draw(st.lists(instances, min_size=1, max_size=3))
    other = draw(instances)
    after = draw(GLUE_AFTER)
    glued = f"{draw(GLUE_BEFORE)}{other}{after}"
    text = draw(escaped_input(previous, [(glued, "y"), ("note", glued)]))
    return previous, other, after, text


@given(accounts, instances, glued_update_cases(), states, st.booleans(), st.text(max_size=20))
@example("000000000000", "i-0abc1234def567890",
         (["i-0fedcba9876543210"], "i-0fedcba98", "\u00e9",
          r'{"InstanceIds": ["\u0069-0fedcba9876543210"], "x\u0069-0fedcba98\u00e9": "y"}'),
         "DISABLED", True, "")
def test_update_output_never_prints_an_escaped_keyed_or_glued_id(
        account, instance, case, state, dry_run, words):
    """Escaped IDs in the list, an ID glued to other characters as a key and as a value, and an
    ID in the Description: none of them is printed by the update diff, live or dry-run."""
    previous, other, after, text = case
    assume(any(p != instance for p in previous))
    current = schedule.desired_schedule(account, instance, state=state)
    current["Target"]["Input"] = text
    current["Description"] = f"{words} was {other}{after} {words}"
    sch = _FakeScheduler(current)
    args = types.SimpleNamespace(enable=False, disable=False, dry_run=dry_run)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        action, new_state = schedule.ensure_schedule(sch, account, instance, args)
    out = buffer.getvalue()
    assert (action, new_state) == ("updated", state)
    assert len(sch.updates) == (0 if dry_run else 1)
    assert account not in out, out
    assert not INSTANCE_SHAPE_RE.search(out), out
    assert "<previous-instance-id>" in out, out


@given(accounts, instances, st.lists(instances, max_size=3), st.text(max_size=20),
       st.text(max_size=20), st.sampled_from(["Description", "KmsKeyArn"]))
def test_schedule_instance_ids_is_a_superset_and_finds_ids_outside_the_input(
        account, instance, previous, before, after, field):
    current = schedule.desired_schedule(account, instance)
    current["Target"]["Input"] = json.dumps({"InstanceIds": previous})
    other = previous[0] if previous else instance
    current[field] = f"{before} {other} {after}"
    found = set(schedule.schedule_instance_ids(current))
    assert set(schedule.input_instance_ids(current)) <= found
    assert other in found
    assert schedule.schedule_instance_ids(None) == [] and schedule.schedule_instance_ids({}) == []


@given(st.one_of(st.from_regex(r"i-[0-9a-f]{0,7}", fullmatch=True), instances.map(str.upper)),
       st.sampled_from(["", " ", "g", "\u00e9", "-"]))
@example("i-0abc123", "")       # one hex short
@example("I-0ABC1234DEF567890", "")
def test_input_without_an_id_shape_gives_no_ids(token, after):
    """Too short (fewer than 8 hex) or uppercase: not an EC2 instance ID."""
    raw = json.dumps({"InstanceIds": [token + after]})
    assert schedule.input_instance_ids({"Target": {"Input": raw}}) == []
    for empty in (None, ""):
        assert schedule.input_instance_ids({"Target": {"Input": empty}}) == []
