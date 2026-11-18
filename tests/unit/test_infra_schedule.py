"""Unit tests of `infra/schedule.py` (EventBridge Scheduler: group, monthly schedule, retired ones).

No test reaches AWS. Two kinds of double are used:
- `botocore.stub.Stubber` on real clients built with fake credentials in eu-north-1: every call
  must be queued in order with its exact parameters, and botocore validates both the parameters
  and the responses against the service model. An unexpected call (for example a write in
  dry-run) fails the test.
- `FakeAws`: a small stateful fake of the four services, for the "apply twice" flows. It
  validates every request against the botocore service model too and returns what AWS returns on
  read (compact input, `MaximumEventAgeInSeconds` added, read-only fields).

The fake account is `000000000000` and the instance ID is made up. Expected request parameters are
written out literally (not built with `schedule.desired_schedule`), so a wrong builder fails here.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
import subprocess
import sys

import boto3
import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from botocore.validate import validate_parameters

from infra import config, schedule
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.unit

ACCOUNT = "000000000000"
INSTANCE = "i-0abc1234def567890"
OTHER_INSTANCE = "i-0fedcba9876543210"
SERVICES = ("sts", "ec2", "iam", "scheduler")
GROUP = "price-intelligence"
NAME = "price-intelligence-monthly"
GROUP_ARN = f"arn:aws:scheduler:eu-north-1:{ACCOUNT}:schedule-group/{GROUP}"
SCHEDULER_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/price-intelligence-scheduler"
OLD_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/Wayakit-EventBridge-Role"
RETIRED = ("Wayakit-Start-Thursday-Night", "Wayakit-Start-Friday-Night")
PROJECT_TAG = {"Key": "project", "Value": "price-intelligence"}
DESCRIBE_FILTERS = [
    {"Name": "tag:Name", "Values": ["wayakit-app-server"]},
    {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
]
WHEN = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

# Every operation the script may call. Read-only ones are the only ones allowed in dry-run.
READ_OPS = {"GetCallerIdentity", "DescribeInstances", "GetRole", "GetScheduleGroup",
            "ListTagsForResource", "GetSchedule"}
WRITE_OPS = {"CreateScheduleGroup", "TagResource", "CreateSchedule", "UpdateSchedule",
             "DeleteSchedule"}


def expected_monthly(state="DISABLED", instance=INSTANCE):
    """The CreateSchedule/UpdateSchedule request we expect, written out by hand."""
    return {
        "Name": "price-intelligence-monthly",
        "GroupName": "price-intelligence",
        "ScheduleExpression": "cron(50 21 ? * 5L *)",
        "ScheduleExpressionTimezone": "America/Mexico_City",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "State": state,
        "Description": schedule.DESCRIPTION,
        "ActionAfterCompletion": "NONE",
        "Target": {
            "Arn": "arn:aws:scheduler:::aws-sdk:ec2:startInstances",
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": json.dumps({"InstanceIds": [instance]}),
            "RetryPolicy": {"MaximumRetryAttempts": 0},
        },
    }


def as_aws_returns(request):
    """What GetSchedule returns after a Create/UpdateSchedule with `request`: compact input,
    AWS default `MaximumEventAgeInSeconds`, read-only fields added."""
    out = copy.deepcopy(request)
    target = out["Target"]
    target["Input"] = json.dumps(json.loads(target["Input"]), separators=(",", ":"))
    target["RetryPolicy"] = dict(target.get("RetryPolicy") or {}, MaximumEventAgeInSeconds=86400)
    out.update(Arn=f"arn:aws:scheduler:eu-north-1:{ACCOUNT}:schedule/{out['GroupName']}/{out['Name']}",
               CreationDate=WHEN, LastModificationDate=WHEN)
    return out


def old_schedule(name, state="ENABLED", **extra):
    """A retired schedule in the `default` group, as GetSchedule returns it."""
    sched = {
        "Name": name, "GroupName": "default",
        "ScheduleExpression": "cron(50 21 ? * 5 *)",
        "ScheduleExpressionTimezone": "America/Mexico_City",
        "FlexibleTimeWindow": {"Mode": "OFF"}, "State": state,
        "Target": {"Arn": "arn:aws:scheduler:::aws-sdk:ec2:startInstances",
                   "RoleArn": OLD_ROLE_ARN,
                   "Input": json.dumps({"InstanceIds": [INSTANCE]}),
                   "RetryPolicy": {"MaximumEventAgeInSeconds": 86400, "MaximumRetryAttempts": 185}},
        "ActionAfterCompletion": "NONE",
        "Arn": f"arn:aws:scheduler:eu-north-1:{ACCOUNT}:schedule/default/{name}",
        "CreationDate": WHEN, "LastModificationDate": WHEN,
    }
    sched.update(extra)
    return sched


def expected_disable(name, **extra):
    """UpdateSchedule request that disables an old schedule keeping everything else."""
    req = old_schedule(name, **extra)
    for key in ("Arn", "CreationDate", "LastModificationDate"):
        req.pop(key)
    req["State"] = "DISABLED"
    return req


# --- Stubber session ---------------------------------------------------------------------------
class StubbedSession:
    """Session double with real botocore clients (fake credentials, eu-north-1) and a Stubber per
    service. Records every client request and every operation name, in call order."""

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
        def record(model, **_):
            self.ops.append((service, model.name))
        return record

    def client(self, service, **kwargs):
        self.client_requests.append((service, kwargs))
        return self.clients[service]

    @property
    def sch(self):
        return self.stubs["scheduler"]

    def op_names(self, service=None):
        return [op for svc, op in self.ops if service in (None, svc)]


def prelude(s, *, role=True, pages=((INSTANCE,),), role_error="NoSuchEntity"):
    """STS, EC2 lookup (one page per tuple) and the scheduler-role check (`role=False`: GetRole
    fails with `role_error`)."""
    s.stubs["sts"].add_response("get_caller_identity",
                                {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/x",
                                 "UserId": "AIDAEXAMPLE"}, {})
    for i, ids in enumerate(pages):
        params = {"Filters": DESCRIBE_FILTERS}
        if i:
            params["NextToken"] = f"token-{i}"
        response = {"Reservations": [{"Instances": [{"InstanceId": x} for x in ids]}]}
        if i + 1 < len(pages):
            response["NextToken"] = f"token-{i + 1}"
        s.stubs["ec2"].add_response("describe_instances", response, params)
    if len(set().union(*pages) if pages else ()) != 1:
        return
    if role:
        s.stubs["iam"].add_response(
            "get_role",
            {"Role": {"Path": "/", "RoleName": "price-intelligence-scheduler",
                      "RoleId": "AROAEXAMPLEEXAMPLE1", "Arn": SCHEDULER_ROLE_ARN,
                      "CreateDate": WHEN}},
            {"RoleName": "price-intelligence-scheduler"})
    else:
        s.stubs["iam"].add_client_error("get_role", role_error, http_status_code=404,
                                        expected_params={"RoleName": "price-intelligence-scheduler"})


def not_found(s, op, params):
    s.sch.add_client_error(op, "ResourceNotFoundException", http_status_code=404,
                           expected_params=params)


def group_missing(s, *, create=True):
    not_found(s, "get_schedule_group", {"Name": GROUP})
    if create:
        s.sch.add_response("create_schedule_group", {"ScheduleGroupArn": GROUP_ARN},
                           {"Name": GROUP, "Tags": [PROJECT_TAG]})


def group_exists(s, tags=(PROJECT_TAG,)):
    s.sch.add_response("get_schedule_group",
                       {"Arn": GROUP_ARN, "Name": GROUP, "State": "ACTIVE",
                        "CreationDate": WHEN, "LastModificationDate": WHEN}, {"Name": GROUP})
    s.sch.add_response("list_tags_for_resource", {"Tags": list(tags)}, {"ResourceArn": GROUP_ARN})


def get_monthly(s, current):
    if current is None:
        not_found(s, "get_schedule", {"Name": NAME, "GroupName": GROUP})
    else:
        s.sch.add_response("get_schedule", current, {"Name": NAME, "GroupName": GROUP})


def get_retired(s, name, current):
    if current is None:
        not_found(s, "get_schedule", {"Name": name, "GroupName": "default"})
    else:
        s.sch.add_response("get_schedule", current, {"Name": name, "GroupName": "default"})


def run(s, argv, capsys):
    """Run `schedule.main` on the stubbed session; every queued response must be consumed and the
    output must never carry the account or the instance ID."""
    for stub in s.stubs.values():
        stub.activate()
    try:
        summary = schedule.main(argv, session=s)
    finally:
        out, err = capsys.readouterr()
        for stub in s.stubs.values():
            stub.deactivate()
    for stub in s.stubs.values():
        stub.assert_no_pending_responses()
    assert ACCOUNT not in out + err
    assert INSTANCE not in out + err and OTHER_INSTANCE not in out + err
    assert all(kw == {"region_name": "eu-north-1"} for _, kw in s.client_requests)
    return summary, out


# --- Pure builders -----------------------------------------------------------------------------
def test_desired_schedule_matches_the_spec():
    assert schedule.desired_schedule(ACCOUNT, INSTANCE) == expected_monthly("DISABLED")
    assert schedule.desired_schedule(ACCOUNT, INSTANCE, state="ENABLED") == expected_monthly("ENABLED")
    assert json.loads(schedule.target_input(INSTANCE)) == {"InstanceIds": [INSTANCE]}


@pytest.mark.parametrize("state", ["enabled", "PAUSED", "", None])
def test_desired_schedule_rejects_unknown_states(state):
    with pytest.raises(ValueError):
        schedule.desired_schedule(ACCOUNT, INSTANCE, state=state)


def test_cron_is_last_thursday_at_2150_mexico_city():
    """EventBridge cron fields: minutes hours day-of-month month day-of-week year, where the
    day of week runs 1=SUN..7=SAT, so `5L` is the last Thursday."""
    fields = re.fullmatch(r"cron\((.*)\)", config.SCHEDULE_CRON).group(1).split()
    assert fields == ["50", "21", "?", "*", "5L", "*"]
    aws_days = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
    assert aws_days[int(fields[4][:-1]) - 1] == "THU"
    assert config.SCHEDULE_TIMEZONE == "America/Mexico_City"


def test_desired_requests_are_valid_for_the_api():
    model = botocore.session.get_session().get_service_model("scheduler")
    for op in ("CreateSchedule", "UpdateSchedule"):
        validate_parameters(schedule.desired_schedule(ACCOUNT, INSTANCE),
                            model.operation_model(op).input_shape)


def test_update_fields_are_every_update_schedule_member():
    """UpdateSchedule resets what is omitted: disabling an old schedule must resend every
    member except the idempotency token, and nothing GetSchedule returns beyond them may be
    lost except the read-only fields."""
    model = botocore.session.get_session().get_service_model("scheduler")
    update = set(model.operation_model("UpdateSchedule").input_shape.members)
    assert set(schedule.UPDATE_FIELDS) == update - {"ClientToken"}
    get_out = set(model.operation_model("GetSchedule").output_shape.members)
    assert get_out - set(schedule.UPDATE_FIELDS) == {"Arn", "CreationDate", "LastModificationDate"}


@pytest.mark.parametrize("current, flags, expected", [
    (None, {}, "DISABLED"),
    ({"State": "ENABLED"}, {}, "ENABLED"),
    ({"State": "DISABLED"}, {}, "DISABLED"),
    ({}, {}, "DISABLED"),
    (None, {"enable": True}, "ENABLED"),
    ({"State": "DISABLED"}, {"enable": True}, "ENABLED"),
    ({"State": "ENABLED"}, {"disable": True}, "DISABLED"),
    (None, {"disable": True}, "DISABLED"),
])
def test_resolve_state(current, flags, expected):
    assert schedule.resolve_state(current, **flags) == expected


def test_enable_and_disable_are_exclusive():
    with pytest.raises(ValueError):
        schedule.resolve_state(None, enable=True, disable=True)
    with pytest.raises(SystemExit) as exc:
        schedule.parse_args(["--enable", "--disable"])
    assert exc.value.code == 2


@pytest.mark.parametrize("current, delete, expected", [
    (None, False, "missing"),
    (None, True, "missing"),
    ({"State": "ENABLED"}, False, "disable"),
    ({"State": "DISABLED"}, False, "already-disabled"),
    ({"State": "ENABLED"}, True, "delete"),
    ({"State": "DISABLED"}, True, "delete"),
])
def test_retired_action(current, delete, expected):
    assert schedule.retired_action(current, delete=delete) == expected


def test_schedule_changes_ignores_what_aws_adds_on_read():
    desired = expected_monthly("ENABLED")
    current = as_aws_returns(desired)
    current.pop("ActionAfterCompletion")  # missing counts as NONE
    current["ResponseMetadata"] = {"HTTPStatusCode": 200}
    assert schedule.schedule_changes(current, desired) == []


@pytest.mark.parametrize("mutate, field", [
    (lambda s: s.update(ScheduleExpression="cron(50 21 ? * 5 *)"), "ScheduleExpression"),
    (lambda s: s.update(ScheduleExpressionTimezone="UTC"), "ScheduleExpressionTimezone"),
    (lambda s: s.update(State="ENABLED"), "State"),
    (lambda s: s.update(FlexibleTimeWindow={"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 15}),
     "FlexibleTimeWindow"),
    (lambda s: s.update(StartDate=WHEN), "StartDate"),
    (lambda s: s["Target"].update(RoleArn=OLD_ROLE_ARN), "Target"),
    (lambda s: s["Target"].update(Input=json.dumps({"InstanceIds": [OTHER_INSTANCE]})), "Target"),
    (lambda s: s["Target"].update(RetryPolicy={"MaximumRetryAttempts": 185}), "Target"),
    (lambda s: s["Target"].update(DeadLetterConfig={"Arn": "arn:aws:sqs:eu-north-1:000000000000:q"}),
     "Target"),
])
def test_schedule_changes_detects_real_differences(mutate, field):
    desired = expected_monthly("DISABLED")
    current = as_aws_returns(desired)
    mutate(current)
    assert schedule.schedule_changes(current, desired) == [field]


def test_disabled_copy_keeps_everything_but_the_state():
    current = old_schedule(RETIRED[0], StartDate=WHEN, Description="old",
                           KmsKeyArn="arn:aws:kms:eu-north-1:000000000000:key/fake",
                           EndDate=None)
    before = copy.deepcopy(current)
    out = schedule.disabled_copy(current)
    assert out == expected_disable(RETIRED[0], StartDate=WHEN, Description="old",
                                   KmsKeyArn="arn:aws:kms:eu-north-1:000000000000:key/fake")
    assert "EndDate" not in out  # None values are not sent
    assert current == before  # input not mutated


def test_redact_and_instance_ids():
    text = f"role arn:aws:iam::{ACCOUNT}:role/x on {INSTANCE}"
    assert schedule.redact(text, ACCOUNT, INSTANCE) == "role arn:aws:iam::<account>:role/x on <instance-id>"
    assert schedule.redact(text) == text
    resp = {"Reservations": [{"Instances": [{"InstanceId": "i-1"}, {"InstanceId": "i-2"}]},
                             {"Instances": []}, {"Instances": [{"InstanceId": "i-3"}]}]}
    assert schedule.instance_ids(resp) == ["i-1", "i-2", "i-3"]
    assert schedule.instance_ids({}) == []


def test_redact_previous_ids():
    text = f"{OTHER_INSTANCE} -> {INSTANCE} in {ACCOUNT}"
    assert schedule.redact(text, ACCOUNT, INSTANCE, [OTHER_INSTANCE]) == (
        "<previous-instance-id> -> <instance-id> in <account>")
    # The same instance as before and now: the tag-resolved placeholder wins.
    assert schedule.redact(INSTANCE, None, INSTANCE, [INSTANCE]) == "<instance-id>"
    # An ID that is a prefix of another (old 8-hex vs 17-hex) leaves no tail of the longer one.
    short, long_ = "i-0abc1234", "i-0abc1234def567890"
    assert schedule.redact(f"{short} {long_}", None, short, [long_]) == (
        "<instance-id> <previous-instance-id>")
    assert schedule.redact(f"{long_} {short}", None, long_, [short]) == (
        "<instance-id> <previous-instance-id>")
    # The account inside an instance ID: the ID goes first, so no "<account>" fragment remains.
    inside = "i-0000000000000abcd"
    assert schedule.redact(inside, "000000000000", inside) == "<instance-id>"
    # Empty previous IDs are ignored; nothing to redact = the text as str.
    assert schedule.redact(text, None, None, ["", None]) == text
    assert schedule.redact(42) == "42"


@pytest.mark.parametrize("current, expected", [
    (None, []),
    ({}, []),
    ({"Target": None}, []),
    ({"Target": {"Arn": "x"}}, []),
    ({"Target": {"Input": None}}, []),
    ({"Target": {"Input": ""}}, []),
    ({"Target": {"Input": json.dumps({"InstanceIds": [INSTANCE]})}}, [INSTANCE]),
    ({"Target": {"Input": json.dumps({"InstanceIds": [OTHER_INSTANCE, INSTANCE, OTHER_INSTANCE]},
                                     indent=4)}}, sorted([INSTANCE, OTHER_INSTANCE])),
    ({"Target": {"Input": '{"InstanceIds":["i-0abc1234"]}'}}, ["i-0abc1234"]),  # old 8-hex format
    ({"Target": {"Input": "start i-0abc1234def567890 please"}}, [INSTANCE]),     # not JSON
    # Tail escaped: the decoded ID only, not the truncated prefix the raw scan stops at.
    ({"Target": {"Input": r'{"InstanceIds":["i-0abc1234def567890"]}'}}, [INSTANCE]),
    ({"Target": {"Input": '{"InstanceIds": ["i-0abc123"]}'}}, []),               # too short
    ({"Target": {"Input": '{"InstanceIds": ["i-0ABC1234DEF567890"]}'}}, []),     # not an EC2 ID
])
def test_input_instance_ids(current, expected):
    assert schedule.input_instance_ids(current) == expected


# --- main(): Stubber flows ---------------------------------------------------------------------
def test_dry_run_with_nothing_existing_makes_only_read_calls(capsys):
    s = StubbedSession()
    prelude(s)
    group_missing(s, create=False)
    get_monthly(s, None)
    for name in RETIRED:
        get_retired(s, name, old_schedule(name))
    summary, out = run(s, ["--dry-run"], capsys)
    assert set(s.op_names()) <= READ_OPS
    assert summary == {"group": "created", "schedule": "created", "state": "DISABLED",
                       "retired": {RETIRED[0]: "disable", RETIRED[1]: "disable"},
                       "enabled_schedules": [], "dry_run": True}
    assert "[dry-run] would create schedule group price-intelligence" in out
    assert "[dry-run] would create schedule price-intelligence-monthly (DISABLED)" in out
    assert out.count("[dry-run] would disable retired schedule default/") == 2
    assert "<instance-id>" in out and "<account>" in out
    assert "WARNING: no Price Intelligence schedule is enabled" in out


def test_apply_creates_tagged_group_and_disabled_schedule_and_disables_retired(capsys):
    s = StubbedSession()
    prelude(s)
    group_missing(s)
    get_monthly(s, None)
    s.sch.add_response("create_schedule", {"ScheduleArn": "arn:x"}, expected_monthly("DISABLED"))
    for name in RETIRED:
        get_retired(s, name, old_schedule(name))
        s.sch.add_response("update_schedule", {"ScheduleArn": "arn:x"}, expected_disable(name))
    summary, out = run(s, [], capsys)
    assert s.op_names("scheduler") == [
        "GetScheduleGroup", "CreateScheduleGroup", "GetSchedule", "CreateSchedule",
        "GetSchedule", "UpdateSchedule", "GetSchedule", "UpdateSchedule"]
    assert "DeleteSchedule" not in s.op_names()
    assert summary == {"group": "created", "schedule": "created", "state": "DISABLED",
                       "retired": {RETIRED[0]: "disable", RETIRED[1]: "disable"},
                       "enabled_schedules": [], "dry_run": False}
    assert "WARNING: no Price Intelligence schedule is enabled" in out


def test_apply_with_enable_creates_it_enabled(capsys):
    s = StubbedSession()
    prelude(s)
    group_missing(s)
    get_monthly(s, None)
    s.sch.add_response("create_schedule", {"ScheduleArn": "arn:x"}, expected_monthly("ENABLED"))
    for name in RETIRED:
        get_retired(s, name, old_schedule(name))
        s.sch.add_response("update_schedule", {"ScheduleArn": "arn:x"}, expected_disable(name))
    summary, out = run(s, ["--enable"], capsys)
    assert summary["state"] == "ENABLED"
    assert summary["enabled_schedules"] == [NAME]
    assert "WARNING" not in out


def test_second_apply_with_everything_in_place_writes_nothing(capsys):
    s = StubbedSession()
    prelude(s)
    group_exists(s, tags=[PROJECT_TAG, {"Key": "owner", "Value": "someone-else"}])
    get_monthly(s, as_aws_returns(expected_monthly("DISABLED")))
    for name in RETIRED:
        get_retired(s, name, old_schedule(name, state="DISABLED"))
    summary, _ = run(s, [], capsys)
    assert not set(s.op_names()) & WRITE_OPS
    assert summary["group"] == "unchanged" and summary["schedule"] == "unchanged"
    assert summary["retired"] == {RETIRED[0]: "already-disabled", RETIRED[1]: "already-disabled"}


def test_existing_enabled_schedule_stays_enabled_without_flags(capsys):
    s = StubbedSession()
    prelude(s)
    group_exists(s)
    get_monthly(s, as_aws_returns(expected_monthly("ENABLED")))
    for name in RETIRED:
        get_retired(s, name, None)
    summary, out = run(s, [], capsys)
    assert not set(s.op_names()) & WRITE_OPS
    assert summary["state"] == "ENABLED" and summary["enabled_schedules"] == [NAME]
    assert summary["retired"] == {RETIRED[0]: "missing", RETIRED[1]: "missing"}
    assert "WARNING" not in out


def test_existing_schedule_pointing_elsewhere_is_updated_keeping_its_state(capsys):
    s = StubbedSession()
    prelude(s)
    group_exists(s)
    stale = as_aws_returns(expected_monthly("ENABLED"))
    stale["ScheduleExpression"] = "cron(0 4 ? * 6 *)"
    stale["Target"]["RoleArn"] = OLD_ROLE_ARN
    get_monthly(s, stale)
    s.sch.add_response("update_schedule", {"ScheduleArn": "arn:x"}, expected_monthly("ENABLED"))
    for name in RETIRED:
        get_retired(s, name, old_schedule(name, state="DISABLED"))
    summary, out = run(s, [], capsys)
    assert summary["schedule"] == "updated" and summary["state"] == "ENABLED"
    assert "update schedule price-intelligence-monthly (ScheduleExpression, Target)" in out


def test_schedule_targeting_another_instance_is_updated_to_the_current_one(capsys):
    s = StubbedSession()
    prelude(s)
    group_exists(s)
    get_monthly(s, as_aws_returns(expected_monthly("DISABLED", instance=OTHER_INSTANCE)))
    s.sch.add_response("update_schedule", {"ScheduleArn": "arn:x"}, expected_monthly("DISABLED"))
    for name in RETIRED:
        get_retired(s, name, None)
    summary, out = run(s, [], capsys)  # run() also checks no account or instance ID is printed
    assert summary["schedule"] == "updated"
    assert "update schedule price-intelligence-monthly (Target)" in out


@pytest.mark.parametrize("argv", [[], ["--dry-run"]])
def test_update_diff_never_prints_the_previous_instance_id(argv, capsys):
    """Was a strict xfail (the diff printed the old target's ID); fixed in `redact`/
    `input_instance_ids`. The retarget must still be visible, with placeholders only."""
    s = StubbedSession()
    prelude(s)
    group_exists(s)
    get_monthly(s, as_aws_returns(expected_monthly("DISABLED", instance=OTHER_INSTANCE)))
    if not argv:
        s.sch.add_response("update_schedule", {"ScheduleArn": "arn:x"},
                           expected_monthly("DISABLED"))
    for name in RETIRED:
        get_retired(s, name, None)
    summary, out = run(s, argv, capsys)  # asserts neither instance ID nor the account is printed
    assert summary["schedule"] == "updated"
    assert not re.search(r"i-[0-9a-f]{8}", out)
    assert '-        "<previous-instance-id>"' in out
    assert '+        "<instance-id>"' in out
    if argv:
        assert set(s.op_names()) <= READ_OPS


@pytest.mark.parametrize("argv", [[], ["--dry-run"]])
def test_update_diff_never_prints_an_escaped_previous_instance_id(argv, capsys):
    """Was a strict xfail: an Input spelling the previous ID with a JSON escape
    (`\\u0069-0fedcba9876543210`) was printed decoded in the diff. Fixed in the F3 follow-up
    (`schedule_instance_ids` also scans the decoded JSON strings)."""
    current = as_aws_returns(expected_monthly("DISABLED", instance=OTHER_INSTANCE))
    current["Target"]["Input"] = '{"InstanceIds": ["\\u0069-0fedcba9876543210"]}'
    assert json.loads(current["Target"]["Input"]) == {"InstanceIds": [OTHER_INSTANCE]}
    assert OTHER_INSTANCE not in current["Target"]["Input"], "precondition: the raw text hides it"
    s = StubbedSession()
    prelude(s)
    group_exists(s)
    get_monthly(s, current)
    if not argv:
        s.sch.add_response("update_schedule", {"ScheduleArn": "arn:x"},
                           expected_monthly("DISABLED"))
    for name in RETIRED:
        get_retired(s, name, None)
    summary, out = run(s, argv, capsys)  # asserts neither instance ID nor the account is printed
    assert summary["schedule"] == "updated"
    assert not re.search(r"i-[0-9a-f]{8}", out)
    assert '-        "<previous-instance-id>"' in out
    assert '+        "<instance-id>"' in out
    if argv:
        assert set(s.op_names()) <= READ_OPS


def test_update_diff_never_prints_an_id_in_the_description_or_glued_to_text(capsys):
    """The diff prints the current Description and the decoded Input: an ID there (hand-edited,
    glued to other characters, or as a JSON key) is redacted too."""
    current = as_aws_returns(expected_monthly("DISABLED", instance=OTHER_INSTANCE))
    current["Description"] = "moved from i-0fedcba98 on 2026-11-01"
    current["Target"]["Input"] = json.dumps(
        {"InstanceIds": [OTHER_INSTANCE], "xi-0fedcba98\u00e9": "i-0fedcba9876543210ff"},
        ensure_ascii=False)
    s = StubbedSession()
    prelude(s)
    group_exists(s)
    get_monthly(s, current)
    for name in RETIRED:
        get_retired(s, name, None)
    summary, out = run(s, ["--dry-run"], capsys)
    assert summary["schedule"] == "updated"
    assert not re.search(r"i-[0-9a-f]{8}", out), out
    assert "<previous-instance-id>" in out


def test_disable_pauses_an_enabled_schedule_and_dry_run_only_reads(capsys):
    current = as_aws_returns(expected_monthly("ENABLED"))
    s = StubbedSession()
    prelude(s)
    group_exists(s)
    get_monthly(s, current)
    s.sch.add_response("update_schedule", {"ScheduleArn": "arn:x"}, expected_monthly("DISABLED"))
    for name in RETIRED:
        get_retired(s, name, None)
    summary, _ = run(s, ["--disable"], capsys)
    assert summary["schedule"] == "updated" and summary["state"] == "DISABLED"

    s = StubbedSession()
    prelude(s)
    group_exists(s)
    get_monthly(s, current)
    for name in RETIRED:
        get_retired(s, name, None)
    summary, out = run(s, ["--disable", "--dry-run"], capsys)
    assert set(s.op_names()) <= READ_OPS
    assert summary["schedule"] == "updated" and summary["dry_run"] is True
    assert "[dry-run] would update schedule price-intelligence-monthly (State)" in out


@pytest.mark.parametrize("tags, added", [
    ([], [PROJECT_TAG]),
    ([{"Key": "owner", "Value": "x"}], [PROJECT_TAG]),
    ([{"Key": "project", "Value": "voz-del-cliente"}], [PROJECT_TAG]),
])
def test_untagged_group_gets_only_the_missing_tag(tags, added, capsys):
    s = StubbedSession()
    prelude(s)
    group_exists(s, tags=tags)
    s.sch.add_response("tag_resource", {}, {"ResourceArn": GROUP_ARN, "Tags": added})
    get_monthly(s, as_aws_returns(expected_monthly("DISABLED")))
    for name in RETIRED:
        get_retired(s, name, None)
    summary, _ = run(s, [], capsys)
    assert summary["group"] == "tagged"
    assert "UntagResource" not in s.op_names()


def test_untagged_group_in_dry_run_is_not_tagged(capsys):
    s = StubbedSession()
    prelude(s)
    group_exists(s, tags=[])
    get_monthly(s, as_aws_returns(expected_monthly("DISABLED")))
    for name in RETIRED:
        get_retired(s, name, None)
    summary, out = run(s, ["--dry-run"], capsys)
    assert set(s.op_names()) <= READ_OPS
    assert summary["group"] == "tagged"
    assert "[dry-run] would tag schedule group price-intelligence" in out


def test_retired_schedules_keep_every_field_when_disabled(capsys):
    extra = {"StartDate": WHEN, "Description": "old weekly start",
             "KmsKeyArn": "arn:aws:kms:eu-north-1:000000000000:key/fake"}
    s = StubbedSession()
    prelude(s)
    group_exists(s)
    get_monthly(s, as_aws_returns(expected_monthly("DISABLED")))
    get_retired(s, RETIRED[0], old_schedule(RETIRED[0], **extra))
    s.sch.add_response("update_schedule", {"ScheduleArn": "arn:x"},
                       expected_disable(RETIRED[0], **extra))
    get_retired(s, RETIRED[1], old_schedule(RETIRED[1], state="DISABLED"))
    summary, _ = run(s, [], capsys)
    assert summary["retired"] == {RETIRED[0]: "disable", RETIRED[1]: "already-disabled"}
    assert "DeleteSchedule" not in s.op_names()


def test_delete_retired_deletes_them_whatever_their_state(capsys):
    s = StubbedSession()
    prelude(s)
    group_exists(s)
    get_monthly(s, as_aws_returns(expected_monthly("DISABLED")))
    get_retired(s, RETIRED[0], old_schedule(RETIRED[0]))
    s.sch.add_response("delete_schedule", {}, {"Name": RETIRED[0], "GroupName": "default"})
    get_retired(s, RETIRED[1], old_schedule(RETIRED[1], state="DISABLED"))
    s.sch.add_response("delete_schedule", {}, {"Name": RETIRED[1], "GroupName": "default"})
    summary, _ = run(s, ["--delete-retired"], capsys)
    assert summary["retired"] == {RETIRED[0]: "delete", RETIRED[1]: "delete"}
    assert "UpdateSchedule" not in s.op_names()


def test_delete_retired_with_missing_schedules_does_nothing(capsys):
    s = StubbedSession()
    prelude(s)
    group_exists(s)
    get_monthly(s, as_aws_returns(expected_monthly("DISABLED")))
    for name in RETIRED:
        get_retired(s, name, None)
    summary, _ = run(s, ["--delete-retired", "--dry-run"], capsys)
    assert summary["retired"] == {RETIRED[0]: "missing", RETIRED[1]: "missing"}
    assert set(s.op_names()) <= READ_OPS


def test_missing_scheduler_role_stops_apply_before_any_scheduler_call(capsys):
    s = StubbedSession()
    prelude(s, role=False)
    for stub in s.stubs.values():
        stub.activate()
    with pytest.raises(SystemExit) as exc:
        schedule.main([], session=s)
    assert "price-intelligence-scheduler" in str(exc.value)
    assert s.op_names("scheduler") == []
    for stub in s.stubs.values():
        stub.assert_no_pending_responses()


def test_missing_scheduler_role_only_warns_in_dry_run(capsys):
    s = StubbedSession()
    prelude(s, role=False)
    group_missing(s, create=False)
    get_monthly(s, None)
    for name in RETIRED:
        get_retired(s, name, None)
    summary, out = run(s, ["--dry-run"], capsys)
    assert "[WARN] Role price-intelligence-scheduler not found" in out
    assert set(s.op_names()) <= READ_OPS
    assert summary["schedule"] == "created"


@pytest.mark.parametrize("argv", [[], ["--dry-run"]])
@pytest.mark.parametrize("pages", [
    ((),),                                  # no instance
    ((INSTANCE, OTHER_INSTANCE),),          # two in one page
    ((INSTANCE,), (OTHER_INSTANCE,)),       # two across pages (NextToken followed)
])
def test_instance_lookup_needs_exactly_one_live_instance(argv, pages):
    s = StubbedSession()
    prelude(s, pages=pages)
    for stub in s.stubs.values():
        stub.activate()
    with pytest.raises(SystemExit) as exc:
        schedule.main(argv, session=s)
    assert "Expected exactly 1 live instance" in str(exc.value)
    assert INSTANCE not in str(exc.value)
    assert s.op_names() == ["GetCallerIdentity"] + ["DescribeInstances"] * len(pages)
    for stub in s.stubs.values():
        stub.assert_no_pending_responses()


def test_instance_lookup_follows_next_token(capsys):
    s = StubbedSession()
    prelude(s, pages=((), (INSTANCE,)))
    group_exists(s)
    get_monthly(s, as_aws_returns(expected_monthly("DISABLED")))
    for name in RETIRED:
        get_retired(s, name, None)
    summary, _ = run(s, [], capsys)
    assert summary["schedule"] == "unchanged"


def test_other_scheduler_errors_propagate_and_nothing_is_written(capsys):
    s = StubbedSession()
    prelude(s)
    s.sch.add_client_error("get_schedule_group", "AccessDeniedException", http_status_code=403,
                           expected_params={"Name": GROUP})
    for stub in s.stubs.values():
        stub.activate()
    with pytest.raises(ClientError) as exc:
        schedule.main([], session=s)
    assert exc.value.response["Error"]["Code"] == "AccessDeniedException"
    assert not set(s.op_names()) & WRITE_OPS


def test_other_iam_errors_propagate():
    s = StubbedSession()
    prelude(s, role=False, role_error="AccessDenied")
    for stub in s.stubs.values():
        stub.activate()
    with pytest.raises(ClientError):
        schedule.main(["--dry-run"], session=s)
    assert s.op_names("scheduler") == []


# --- CLI ---------------------------------------------------------------------------------------
def test_cli_prints_only_the_error_code(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise ClientError({"Error": {"Code": "ValidationException",
                                     "Message": f"bad role arn:aws:iam::{ACCOUNT}:role/x"}},
                          "UpdateSchedule")
    monkeypatch.setattr(schedule, "main", boom)
    with pytest.raises(SystemExit) as exc:
        schedule._cli()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "AWS error ValidationException in UpdateSchedule" in err
    assert ACCOUNT not in err and "bad role" not in err


def test_cli_lets_system_exit_and_other_errors_through(monkeypatch):
    monkeypatch.setattr(schedule, "main", lambda *a, **k: (_ for _ in ()).throw(SystemExit("x")))
    with pytest.raises(SystemExit, match="x"):
        schedule._cli()
    monkeypatch.setattr(schedule, "main", lambda *a, **k: (_ for _ in ()).throw(KeyError("k")))
    with pytest.raises(KeyError):
        schedule._cli()


def test_importing_schedule_loads_no_boto3_and_runs_nothing():
    code = "import sys, infra.schedule; print('boto3' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip() == "False"


def test_source_holds_no_account_or_instance_ids():
    text = (REPO_ROOT / "infra" / "schedule.py").read_text()
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", text)
    assert not re.search(r"\bi-[0-9a-f]{8,17}\b", text)
    assert "@" not in text.replace("@example", "")


def test_docstring_says_what_it_configures_cost_and_usage():
    doc = schedule.__doc__
    for needle in ("price-intelligence-monthly", "cron(50 21 ? * 5L *)", "America/Mexico_City",
                   "DISABLED", "--dry-run", "--enable", "--delete-retired", "Cost",
                   "python -m infra.schedule"):
        assert needle in doc


# --- Stateful fake: apply twice ----------------------------------------------------------------
class FakeAws:
    """Stateful fake of STS, EC2, IAM and EventBridge Scheduler. Every request is validated
    against the botocore model; reads return what AWS returns (see `as_aws_returns`)."""

    def __init__(self, retired_state="ENABLED"):
        self.model = {n: botocore.session.get_session().get_service_model(n) for n in SERVICES}
        self.calls = []
        self.groups = {}
        self.schedules = {("default", n): old_schedule(n, state=retired_state) for n in RETIRED}

    def client(self, service, **kwargs):
        assert kwargs == {"region_name": "eu-north-1"}
        return _FakeClient(self, service)

    def call(self, service, op, params):
        model = self.model[service]
        api = {model.operation_model(o).name: o for o in model.operation_names}
        camel = "".join(w.capitalize() for w in op.split("_"))
        assert camel in api, (service, op)
        validate_parameters(params, model.operation_model(camel).input_shape)
        self.calls.append((service, camel, copy.deepcopy(params)))
        return getattr(self, f"_{service}_{op}")(**params)

    def ops(self, start=0):
        return [op for _, op, _ in self.calls[start:]]

    @staticmethod
    def _nf(op):
        return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "nf"}}, op)

    def _sts_get_caller_identity(self):
        return {"Account": ACCOUNT}

    def _ec2_describe_instances(self, Filters, NextToken=None):
        assert Filters == DESCRIBE_FILTERS
        return {"Reservations": [{"Instances": [{"InstanceId": INSTANCE}]}]}

    def _iam_get_role(self, RoleName):
        return {"Role": {"RoleName": RoleName, "Arn": SCHEDULER_ROLE_ARN}}

    def _scheduler_get_schedule_group(self, Name):
        if Name not in self.groups:
            raise self._nf("GetScheduleGroup")
        return {"Arn": GROUP_ARN, "Name": Name, "State": "ACTIVE"}

    def _scheduler_create_schedule_group(self, Name, Tags=()):
        assert Name not in self.groups
        self.groups[Name] = list(Tags)
        return {"ScheduleGroupArn": GROUP_ARN}

    def _scheduler_list_tags_for_resource(self, ResourceArn):
        return {"Tags": self.groups[ResourceArn.rsplit("/", 1)[1]]}

    def _scheduler_tag_resource(self, ResourceArn, Tags):
        self.groups[ResourceArn.rsplit("/", 1)[1]] += Tags
        return {}

    def _scheduler_get_schedule(self, Name, GroupName="default"):
        if (GroupName, Name) not in self.schedules:
            raise self._nf("GetSchedule")
        return copy.deepcopy(self.schedules[(GroupName, Name)])

    def _scheduler_create_schedule(self, **kw):
        key = (kw.get("GroupName", "default"), kw["Name"])
        assert key not in self.schedules, "duplicate create"
        assert kw["GroupName"] in self.groups, "schedule created before its group"
        self.schedules[key] = as_aws_returns(kw)
        return {"ScheduleArn": "arn:x"}

    def _scheduler_update_schedule(self, **kw):
        key = (kw.get("GroupName", "default"), kw["Name"])
        assert key in self.schedules
        self.schedules[key] = as_aws_returns(kw)
        return {"ScheduleArn": "arn:x"}

    def _scheduler_delete_schedule(self, Name, GroupName="default"):
        del self.schedules[(GroupName, Name)]
        return {}


class _FakeClient:
    def __init__(self, fake, service):
        self._fake, self._service = fake, service

    def __getattr__(self, op):
        return lambda **params: self._fake.call(self._service, op, params)


def test_apply_twice_then_enable_then_apply_again(capsys):
    aws = FakeAws()
    first = schedule.main([], session=aws)
    assert first["schedule"] == "created" and first["state"] == "DISABLED"
    assert aws.schedules[(GROUP, NAME)]["State"] == "DISABLED"
    assert all(aws.schedules[("default", n)]["State"] == "DISABLED" for n in RETIRED)
    assert aws.groups[GROUP] == [PROJECT_TAG]

    mark = len(aws.calls)
    second = schedule.main([], session=aws)
    assert not set(aws.ops(mark)) & WRITE_OPS, aws.ops(mark)
    assert second["group"] == "unchanged" and second["schedule"] == "unchanged"
    assert second["retired"] == {n: "already-disabled" for n in RETIRED}

    mark = len(aws.calls)
    enabled = schedule.main(["--enable"], session=aws)
    assert [op for op in aws.ops(mark) if op in WRITE_OPS] == ["UpdateSchedule"]
    assert enabled["state"] == "ENABLED"
    assert aws.schedules[(GROUP, NAME)]["State"] == "ENABLED"

    mark = len(aws.calls)
    again = schedule.main([], session=aws)
    assert not set(aws.ops(mark)) & WRITE_OPS
    assert again["state"] == "ENABLED" and again["enabled_schedules"] == [NAME]

    out = capsys.readouterr().out
    assert ACCOUNT not in out and INSTANCE not in out


def test_dry_run_on_the_fake_changes_no_state():
    aws = FakeAws()
    before = copy.deepcopy((aws.groups, aws.schedules))
    for argv in (["--dry-run"], ["--dry-run", "--enable", "--delete-retired"]):
        schedule.main(argv, session=aws)
    assert (aws.groups, aws.schedules) == before
    assert set(aws.ops()) <= READ_OPS


def test_least_privilege_across_every_flow():
    """Every flow together touches only the operations the operator policy needs: no EC2 or IAM
    write (it never starts the instance or edits a role), deletes only in the `default` group and
    only the retired names, and every target is ec2:startInstances on one instance with the
    PI scheduler role (never the old role)."""
    aws = FakeAws()
    for argv in ([], ["--enable"], ["--disable"], ["--delete-retired"], ["--dry-run"]):
        schedule.main(argv, session=aws)
    assert {op for _, op, _ in aws.calls} <= READ_OPS | WRITE_OPS
    assert {svc for svc, op, _ in aws.calls if op in WRITE_OPS} == {"scheduler"}
    for _, op, params in aws.calls:
        if op == "DeleteSchedule":
            assert params["GroupName"] == "default" and params["Name"] in RETIRED
        if op == "CreateSchedule" or (op == "UpdateSchedule" and params["Name"] == NAME):
            target = params["Target"]
            assert target["Arn"] == "arn:aws:scheduler:::aws-sdk:ec2:startInstances"
            assert target["RoleArn"] == SCHEDULER_ROLE_ARN
            assert json.loads(target["Input"]) == {"InstanceIds": [INSTANCE]}
            assert params["GroupName"] == GROUP
        if op == "UpdateSchedule" and params["Name"] in RETIRED:
            assert params["State"] == "DISABLED" and params["GroupName"] == "default"
        if op == "GetRole":
            assert params == {"RoleName": "price-intelligence-scheduler"}
    assert ("default", RETIRED[0]) not in aws.schedules  # --delete-retired ran
