"""Create or update the monthly Price Intelligence schedule. Safe to run more than once.

What it leaves configured, in EventBridge Scheduler (eu-north-1):
- Schedule group `price-intelligence`, tagged `project=price-intelligence` (single schedules do
  not accept tags, so the group carries them).
- Schedule `price-intelligence-monthly`: `cron(50 21 ? * 5L *)` in `America/Mexico_City` (last
  Thursday of each month, 21:50), flexible window OFF. Target: the universal target
  `arn:aws:scheduler:::aws-sdk:ec2:startInstances` with input `{"InstanceIds": ["<id>"]}`, where
  the instance is found by its Name tag `wayakit-app-server`, run with the role
  `price-intelligence-scheduler`. Retry policy 0: a missed start is reported by
  `infra.check_pi`, it is not retried (a late start would fall outside the boot-guard window).
- State: a NEW schedule is created DISABLED, so applying this script never starts a run before
  Mafer decides. An existing schedule keeps its current state. `--enable` / `--disable` set it.
- Retired schedules in the `default` group (`Wayakit-Start-Thursday-Night`,
  `Wayakit-Start-Friday-Night`): DISABLED by default (everything else in them is kept, so
  re-enabling them in the console is the rollback); `--delete-retired` deletes them instead. The
  old role `Wayakit-EventBridge-Role` is not touched.

WARNING: today the retired schedules are what start the production runs. Without `--enable`,
applying this script leaves NO enabled schedule: the summary says so.

Requires: the role `price-intelligence-scheduler` (`python -m infra.scheduler_role`) and the
instance tagged `Name=wayakit-app-server`. The operator's SSO role also needs `iam:PassRole` on
`price-intelligence-scheduler` (CreateSchedule/UpdateSchedule pass it to EventBridge Scheduler);
without it the apply stops with AccessDenied.

Output never shows the account or instance IDs: the tagged instance is printed as
`<instance-id>` and any ID found in the existing schedule's input as `<previous-instance-id>`.

Cost: none (EventBridge Scheduler free tier: 14 million invocations per month; this is one per
month).

Usage (from the repo root; profile `wayakit-vdc`, or `AWS_PROFILE`):
    python -m infra.schedule --dry-run             # only read calls; prints the plan
    python -m infra.schedule                       # apply; new schedule DISABLED, retired disabled
    python -m infra.schedule --enable              # turn the monthly run on
    python -m infra.schedule --disable             # pause it without deleting it
    python -m infra.schedule --delete-retired      # delete the two old schedules instead
"""
import json
import re
import sys

from infra import _aws, config

# Fields UpdateSchedule accepts (it replaces the whole schedule, so an update must resend them).
UPDATE_FIELDS = ("Name", "GroupName", "ScheduleExpression", "ScheduleExpressionTimezone",
                 "StartDate", "EndDate", "Description", "State", "KmsKeyArn", "Target",
                 "FlexibleTimeWindow", "ActionAfterCompletion")
DESCRIPTION = ("Price Intelligence: start wayakit-app-server for the monthly run "
               "(last Thursday, 21:50 Mexico City)")
NOT_FOUND = "ResourceNotFoundException"
# The instance is resolved among these states; a terminated instance is never a target.
LIVE_STATES = ("pending", "running", "stopping", "stopped")
INSTANCE_PLACEHOLDER = "<instance-id>"
PREVIOUS_INSTANCE_PLACEHOLDER = "<previous-instance-id>"
# EC2 instance IDs: `i-` + 8 (old format) or 17 hex characters. No word boundaries on purpose:
# this pattern decides what gets redacted before printing, so an ID glued to other characters
# (`xi-...`, `i-...é`, a longer hex run) must still be found; over-redacting output is harmless.
INSTANCE_ID_PATTERN = re.compile(r"i-[0-9a-f]{8,17}")


# --- Pure builders -----------------------------------------------------------------------------
def target_input(instance_id):
    """JSON input of the universal `ec2:startInstances` target."""
    return json.dumps({"InstanceIds": [instance_id]})


def resolve_state(current, *, enable=False, disable=False):
    """State to send. `--enable`/`--disable` win; otherwise an existing schedule keeps its state
    (re-applying never flips it) and a new one (`current` None) starts DISABLED."""
    if enable and disable:
        raise ValueError("--enable and --disable are mutually exclusive.")
    if enable:
        return "ENABLED"
    if disable:
        return "DISABLED"
    return (current or {}).get("State") or "DISABLED"


def desired_schedule(account, instance_id, *, state="DISABLED"):
    """Full CreateSchedule/UpdateSchedule kwargs for the monthly schedule."""
    if state not in ("ENABLED", "DISABLED"):
        raise ValueError(f"Unknown schedule state {state!r}.")
    return {
        "Name": config.SCHEDULE_NAME,
        "GroupName": config.SCHEDULE_GROUP,
        "ScheduleExpression": config.SCHEDULE_CRON,
        "ScheduleExpressionTimezone": config.SCHEDULE_TIMEZONE,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "State": state,
        "Description": DESCRIPTION,
        "ActionAfterCompletion": "NONE",
        "Target": {
            "Arn": config.SCHEDULE_TARGET_ARN,
            "RoleArn": config.role_arn(account, config.SCHEDULER_ROLE),
            "Input": target_input(instance_id),
            "RetryPolicy": {"MaximumRetryAttempts": 0},
        },
    }


def _parsed(text):
    try:
        return json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return text


def comparable_schedule(schedule):
    """What decides whether an update is needed, normalized: AWS defaults that we do not set
    (`MaximumEventAgeInSeconds`, a missing `ActionAfterCompletion`) are ignored, the input is
    compared as JSON, and any extra target setting (DLQ, ECS, ...) or start/end date or KMS key
    counts as a difference because our update would remove it."""
    target = schedule.get("Target") or {}
    extra_target = sorted(k for k in target if k not in ("Arn", "RoleArn", "Input", "RetryPolicy"))
    return {
        "ScheduleExpression": schedule.get("ScheduleExpression"),
        "ScheduleExpressionTimezone": schedule.get("ScheduleExpressionTimezone"),
        "FlexibleTimeWindow": schedule.get("FlexibleTimeWindow"),
        "State": schedule.get("State"),
        "Description": schedule.get("Description"),
        "ActionAfterCompletion": schedule.get("ActionAfterCompletion") or "NONE",
        "StartDate": schedule.get("StartDate"),
        "EndDate": schedule.get("EndDate"),
        "KmsKeyArn": schedule.get("KmsKeyArn"),
        "Target": {
            "Arn": target.get("Arn"),
            "RoleArn": target.get("RoleArn"),
            "Input": _parsed(target.get("Input")),
            "MaximumRetryAttempts": (target.get("RetryPolicy") or {}).get("MaximumRetryAttempts"),
            "OtherSettings": extra_target,
        },
    }


def schedule_changes(current, desired):
    """Sorted list of top-level fields that differ (`Target` as one field). Empty = no update."""
    a, b = comparable_schedule(current), comparable_schedule(desired)
    return sorted(k for k in b if a.get(k) != b.get(k))


def retired_action(current, *, delete=False):
    """What to do with a retired schedule: 'missing', 'delete', 'disable' or 'already-disabled'."""
    if current is None:
        return "missing"
    if delete:
        return "delete"
    return "already-disabled" if current.get("State") == "DISABLED" else "disable"


def disabled_copy(current):
    """UpdateSchedule kwargs that keep everything of `current` (a GetSchedule response) except
    the state, which becomes DISABLED. UpdateSchedule resets omitted fields, so all are resent."""
    kwargs = {k: current[k] for k in UPDATE_FIELDS if current.get(k) is not None}
    kwargs["State"] = "DISABLED"
    return kwargs


def instance_ids(describe_response):
    """Instance IDs in a DescribeInstances response (all reservations)."""
    return [inst["InstanceId"]
            for res in describe_response.get("Reservations", [])
            for inst in res.get("Instances", [])]


def _strings(value):
    """Every string inside a parsed JSON value (dict keys and values, list items), recursively."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _ids_in(texts):
    return {match for text in texts for match in INSTANCE_ID_PATTERN.findall(text)}


def input_instance_ids(schedule):
    """Every instance-ID-shaped token in the target input of a GetSchedule response (`None` = no
    schedule). Read from the raw text (a non-JSON or hand-edited input is covered) and from the
    decoded JSON strings: an ID spelled with JSON escapes (`\\u0069-...`) is invisible in the raw
    text but is printed decoded in the diff."""
    raw = ((schedule or {}).get("Target") or {}).get("Input")
    if raw is None:
        return []
    raw_text = raw if isinstance(raw, str) else json.dumps(raw, default=str)
    decoded = _ids_in(_strings(_parsed(raw)))
    # When only the tail of an ID is escaped (`i-0...00`), the raw scan stops at the escape
    # and yields a truncated prefix that is not an ID. Drop raw-only matches that are strict
    # prefixes of a decoded ID: the diff prints the decoded ID, so redacting it is enough.
    raw_only = {m for m in _ids_in([raw_text]) - decoded
                if not any(d.startswith(m) for d in decoded)}
    return sorted(decoded | raw_only)


def schedule_instance_ids(schedule):
    """Instance-ID-shaped tokens anywhere in what the update diff prints for `schedule`: the
    target input (see `input_instance_ids`) plus every string of `comparable_schedule` (a
    hand-edited description, for example). These are redacted as `<previous-instance-id>`."""
    if not schedule:
        return []
    found = set(input_instance_ids(schedule))
    found |= _ids_in(_strings(comparable_schedule(schedule)))
    return sorted(found)


def redact(text, account=None, instance_id=None, previous_ids=()):
    """Remove the account and instance IDs from text that will be printed. `instance_id` (the one
    resolved by tag) becomes `<instance-id>`; `previous_ids` (e.g. the IDs in the schedule that is
    being replaced) become `<previous-instance-id>`, so a retarget is still visible in the diff."""
    placeholders = {pid: PREVIOUS_INSTANCE_PLACEHOLDER for pid in previous_ids if pid}
    if instance_id:
        placeholders[instance_id] = INSTANCE_PLACEHOLDER
    text = str(text)
    # Longest first, so an ID that is a prefix of another never leaves the tail of the longer one.
    # Instance IDs before the account: an account ID can appear inside an instance ID.
    for value in sorted(placeholders, key=len, reverse=True):
        text = text.replace(value, placeholders[value])
    return _aws.redact_account(text, account)


# --- AWS reads ---------------------------------------------------------------------------------
def _get_or_none(call, **kwargs):
    try:
        return call(**kwargs)
    except Exception as exc:  # botocore ClientError; anything else propagates
        if _aws.error_code(exc) == NOT_FOUND:
            return None
        raise


def find_instance_id(ec2):
    """ID of the only live instance tagged `Name=wayakit-app-server`; SystemExit otherwise."""
    ids, token = [], None
    while True:
        kwargs = {"Filters": [{"Name": "tag:Name", "Values": [config.INSTANCE_NAME]},
                              {"Name": "instance-state-name", "Values": list(LIVE_STATES)}]}
        if token:
            kwargs["NextToken"] = token
        response = ec2.describe_instances(**kwargs)
        ids += instance_ids(response)
        token = response.get("NextToken")
        if not token:
            break
    if len(ids) != 1:
        raise SystemExit(f"Expected exactly 1 live instance tagged Name={config.INSTANCE_NAME}, "
                         f"found {len(ids)}. Nothing was changed.")
    return ids[0]


def scheduler_role_exists(iam):
    try:
        iam.get_role(RoleName=config.SCHEDULER_ROLE)
        return True
    except Exception as exc:
        if _aws.error_code(exc) == "NoSuchEntity":
            return False
        raise


# --- Steps -------------------------------------------------------------------------------------
def ensure_group(sch, dry_run, account):
    """Create the group with the project tag, or add the tag if it is missing."""
    group = _get_or_none(sch.get_schedule_group, Name=config.SCHEDULE_GROUP)
    if group is None:
        _aws.report(f"create schedule group {config.SCHEDULE_GROUP} (tag project)",
                    dry_run=dry_run, account=account)
        if not dry_run:
            sch.create_schedule_group(Name=config.SCHEDULE_GROUP, Tags=_aws.project_tags())
        return "created"
    current = sch.list_tags_for_resource(ResourceArn=group["Arn"]).get("Tags", [])
    missing = _aws.tags_to_add(current)
    if not missing:
        _aws.report(f"Schedule group {config.SCHEDULE_GROUP}: exists, tagged", account=account)
        return "unchanged"
    _aws.report(f"tag schedule group {config.SCHEDULE_GROUP} with {sorted(missing)}",
                dry_run=dry_run, account=account)
    if not dry_run:
        sch.tag_resource(ResourceArn=group["Arn"], Tags=_aws.to_tag_list(missing))
    return "tagged"


def ensure_schedule(sch, account, instance_id, args):
    """Create or update the monthly schedule. Returns (action, state)."""
    current = _get_or_none(sch.get_schedule, Name=config.SCHEDULE_NAME,
                           GroupName=config.SCHEDULE_GROUP)
    state = resolve_state(current, enable=args.enable, disable=args.disable)
    desired = desired_schedule(account, instance_id, state=state)
    shown = redact(json.dumps(desired, indent=2), account, instance_id)
    if current is None:
        _aws.report(f"create schedule {config.SCHEDULE_NAME} ({state}):\n{shown}",
                    dry_run=args.dry_run, account=account)
        if not args.dry_run:
            sch.create_schedule(**desired)
        return "created", state
    changes = schedule_changes(current, desired)
    if not changes:
        _aws.report(f"Schedule {config.SCHEDULE_NAME}: up to date ({state})", account=account)
        return "unchanged", state
    diff = _aws.json_diff(comparable_schedule(current), comparable_schedule(desired),
                          config.SCHEDULE_NAME)
    # The current schedule may point at another (e.g. replaced) instance: redact that ID as well,
    # wherever it appears in what the diff prints.
    shown_diff = redact(diff, account, instance_id, schedule_instance_ids(current))
    _aws.report(f"update schedule {config.SCHEDULE_NAME} ({', '.join(changes)}):\n{shown_diff}",
                dry_run=args.dry_run, account=account)
    if not args.dry_run:
        sch.update_schedule(**desired)
    return "updated", state


def retire(sch, name, args, account):
    """Disable (default) or delete one retired schedule in the `default` group."""
    current = _get_or_none(sch.get_schedule, Name=name, GroupName=config.RETIRED_SCHEDULE_GROUP)
    action = retired_action(current, delete=args.delete_retired)
    where = f"{config.RETIRED_SCHEDULE_GROUP}/{name}"
    if action == "missing":
        _aws.report(f"Retired schedule {where}: not found, nothing to do", account=account)
    elif action == "already-disabled":
        _aws.report(f"Retired schedule {where}: already DISABLED", account=account)
    elif action == "disable":
        _aws.report(f"disable retired schedule {where}", dry_run=args.dry_run, account=account)
        if not args.dry_run:
            sch.update_schedule(**disabled_copy(current))
    else:
        _aws.report(f"delete retired schedule {where}", dry_run=args.dry_run, account=account)
        if not args.dry_run:
            sch.delete_schedule(Name=name, GroupName=config.RETIRED_SCHEDULE_GROUP)
    return action


# --- CLI ---------------------------------------------------------------------------------------
def parse_args(argv=None):
    parser = _aws.base_parser(__doc__)
    state = parser.add_mutually_exclusive_group()
    state.add_argument("--enable", action="store_true", help="leave the monthly schedule ENABLED")
    state.add_argument("--disable", action="store_true", help="leave it DISABLED (pause)")
    parser.add_argument("--delete-retired", action="store_true",
                        help="delete the old `default` schedules instead of disabling them")
    return parser.parse_args(argv)


def main(argv=None, session=None):
    """Apply (or, with --dry-run, only read and print) the schedule configuration. Returns a
    summary dict: group, schedule, state, retired {name: action}, enabled_schedules."""
    args = parse_args(argv)
    sess = _aws.get_session(session)
    account = _aws.account_id(sess)
    sch = _aws.client(sess, "scheduler")

    instance_id = find_instance_id(_aws.client(sess, "ec2"))
    if not scheduler_role_exists(_aws.client(sess, "iam")):
        msg = (f"Role {config.SCHEDULER_ROLE} not found: run `python -m infra.scheduler_role` "
               "first.")
        if not args.dry_run:
            raise SystemExit(msg + " Nothing was changed.")
        _aws.report(f"[WARN] {msg}", account=account)

    group = ensure_group(sch, args.dry_run, account)
    action, state = ensure_schedule(sch, account, instance_id, args)
    retired = {name: retire(sch, name, args, account) for name in config.RETIRED_SCHEDULES}

    # Retired schedules end up disabled or deleted, except one already disabled (also off).
    enabled = [config.SCHEDULE_NAME] if state == "ENABLED" else []
    summary = {"group": group, "schedule": action, "state": state, "retired": retired,
               "enabled_schedules": enabled, "dry_run": args.dry_run}
    print("\nSummary" + (" (dry-run, nothing changed)" if args.dry_run else "") + ":")
    print(f"  group {config.SCHEDULE_GROUP}: {group}")
    print(f"  schedule {config.SCHEDULE_NAME}: {action}, {state}, target {INSTANCE_PLACEHOLDER} "
          f"(Name={config.INSTANCE_NAME})")
    for name, act in retired.items():
        print(f"  retired {config.RETIRED_SCHEDULE_GROUP}/{name}: {act}")
    if not enabled:
        print("  WARNING: no Price Intelligence schedule is enabled; nothing will start the "
              "instance. Use --enable when the new run model is deployed.")
    return summary


def _cli():
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        # Only the error code and operation: AWS messages can carry ARNs with the account ID.
        code = _aws.error_code(exc)
        if code is None:
            raise
        op = getattr(exc, "operation_name", "AWS call")
        print(f"AWS error {code} in {op}. Nothing after this step was changed.", file=sys.stderr)
        sys.exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
