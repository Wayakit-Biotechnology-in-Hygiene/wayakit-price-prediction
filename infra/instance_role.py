"""Create or update the IAM role and instance profile of the PI instance. Safe to run more than once.

What it leaves configured:
- Role `price-intelligence-instance` (trusted only by `ec2.amazonaws.com`, tag
  `project=price-intelligence`) with one inline policy, `price-intelligence-instance-access`:
  - `secretsmanager:GetSecretValue` only on `prod/wayakit-app` (ARN pattern
    `prod/wayakit-app-??????`: the 6 characters Secrets Manager appends, nothing more);
  - S3: `ListBucket` on the PI bucket, `Get/Put/DeleteObject` on the data prefixes only
    (`data/`, `models/`, `state/`, `logs/`) and only `GetObject` on `releases/` (releases and the
    `releases/current` pointer are written by the deploy role alone, so a compromised or buggy
    run cannot replace the code the next boot installs);
  - `cloudwatch:PutMetricData` only in the `PriceIntelligence` namespace (the action has no
    resource-level permissions, so its Resource must be `*`; the namespace condition limits it);
  - `sns:Publish` only on the `price-intelligence-alerts` topic (its key is the AWS-managed
    `alias/aws/sns`, whose key policy already lets SNS use it for the account: no KMS statement);
  - `bedrock:InvokeModel` (the Converse API needs nothing else) on the DeepSeek V3.2 foundation
    model in eu-north-1 (plain model ID, also the fallback if no application profile exists), on
    the project's application inference profiles that exist, on the Haiku 4.5 `eu.` system profile,
    and on the Haiku 4.5 foundation model in the EU regions that profile routes to. Those regions
    are read from AWS (`GetInferenceProfile`), not guessed, and that statement only applies when
    the call goes through one of the Haiku profiles (`bedrock:InferenceProfileArn` condition).
- The AWS-managed policy `AmazonSSMManagedInstanceCore` attached (SSM access, no SSH).
- Instance profile `price-intelligence-instance` (tagged) holding the role. This script does not
  touch the instance: `ec2_instance.py` associates the profile (rollback: re-associate the old one).

The old role `Wayakit-EC2-Secrets-Role` is never edited or deleted. `--audit-old-role` is a
read-only report of who used it in the last 90 days (CloudTrail `LookupEvents`: which principals
assumed it, from which instances, which API calls and which secrets), so Mafer can confirm that
only PI uses it before the switch. It covers management events in eu-north-1 only.

Run after `bucket.py`, `alerts.py` and `bedrock_profiles.py` (a missing application profile is
reported as a warning and left out of the policy; run this script again once it exists).
Extra inline or managed policies found on the role are reported, never removed.

Cost: IAM and CloudTrail event history are free.

Usage (from the repo root):
    python -m infra.instance_role --dry-run
    python -m infra.instance_role
    python -m infra.instance_role --audit-old-role
"""
import datetime as dt
import json
import re
import urllib.parse
from collections import Counter

from infra import _aws, config
# Lives in config (pure, shared by every policy builder); re-exported because scheduler_role.py and
# deploy_role.py import it from here.
from infra.config import check_account  # noqa: F401

POLICY_NAME = f"{config.PROJECT}-instance-access"
DESCRIPTION = "Price Intelligence EC2 instance: PI secret, bucket, SSM, metrics, alerts and Bedrock"
MAX_SESSION_SECONDS = 3600
EC2_SERVICE = "ec2.amazonaws.com"
MANAGED_POLICIES = (config.SSM_MANAGED_POLICY_ARN,)
APPLICATION_PROFILES = (config.DEEPSEEK_PROFILE, config.HAIKU_PROFILE)
# States in which an instance still exists (terminated ones keep their Name tag for a while).
LIVE_STATES = ("pending", "running", "stopping", "stopped")
_INSTANCE_ID = re.compile(r"i-[0-9a-f]{8,17}")
# Prefixes the instance reads and writes (in config order); `releases/` is read-only for it.
DATA_PREFIXES = tuple(p for p in config.BUCKET_PREFIXES if p != config.RELEASES_PREFIX)


# --- Pure builders -----------------------------------------------------------------------------
def trust_policy():
    """Only EC2 can assume the role (through the instance profile)."""
    return {
        "Version": config.POLICY_VERSION,
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": EC2_SERVICE},
            "Action": "sts:AssumeRole",
        }],
    }


# Application profile IDs are 12 lowercase alphanumerics; a fullmatch (not a prefix check) keeps
# a `/*` or empty ID from turning into a wildcard Resource of bedrock:InvokeModel.
_APP_PROFILE_ID = r"[a-z0-9]{12}"


def _check_profile_arns(account, profile_arns):
    pattern = re.compile(re.escape(f"arn:aws:bedrock:{config.REGION}:{account}:application-inference-profile/")
                         + _APP_PROFILE_ID)
    for name, value in profile_arns.items():
        if name not in APPLICATION_PROFILES:
            raise ValueError(f"Unknown application inference profile '{name}'.")
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(f"Profile '{name}' is not an application profile of this account in {config.REGION}.")


def _check_haiku_models(haiku_model_arns):
    if not haiku_model_arns:
        raise ValueError("The Haiku system profile routes to no model.")
    pattern = re.compile(r"arn:aws:bedrock:eu-[a-z]+-\d::foundation-model/" + re.escape(config.HAIKU_MODEL_ID))
    for value in haiku_model_arns:
        if not pattern.fullmatch(str(value)):
            raise ValueError(f"Unexpected Haiku model ARN (must be the Haiku 4.5 model in an EU region): {value}")


def instance_policy(account, profile_arns, haiku_model_arns):
    """Inline policy of the instance role.

    `profile_arns`: {application profile name: ARN} for the profiles that exist (may be empty).
    `haiku_model_arns`: foundation-model ARNs the Haiku profiles route to (EU regions only).
    Raises ValueError on an account that is not 12 digits, a foreign or malformed profile ARN, or a
    non-EU / non-Haiku model ARN.
    """
    check_account(account)
    _check_profile_arns(account, profile_arns)
    _check_haiku_models(haiku_model_arns)
    bucket = config.bucket_arn(account)
    haiku_system = config.inference_profile_arn(account, config.HAIKU_SYSTEM_PROFILE_ID)
    haiku_profiles = {haiku_system}
    if config.HAIKU_PROFILE in profile_arns:
        haiku_profiles.add(profile_arns[config.HAIKU_PROFILE])
    return {
        "Version": config.POLICY_VERSION,
        "Statement": [
            {
                "Sid": "ReadPiSecret",
                "Effect": "Allow",
                "Action": "secretsmanager:GetSecretValue",
                "Resource": config.secret_arn_pattern(account),
            },
            {
                "Sid": "ListPiBucket",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": bucket,
            },
            {
                "Sid": "ReadWritePiObjects",
                "Effect": "Allow",
                "Action": ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"],
                "Resource": [f"{bucket}/{prefix}*" for prefix in DATA_PREFIXES],
            },
            {
                "Sid": "ReadPiReleases",
                "Effect": "Allow",
                "Action": "s3:GetObject",
                "Resource": f"{bucket}/{config.RELEASES_PREFIX}*",
            },
            {
                "Sid": "PutPiMetrics",
                "Effect": "Allow",
                "Action": "cloudwatch:PutMetricData",
                "Resource": "*",  # PutMetricData has no resource ARN; limited by namespace below
                "Condition": {"StringEquals": {"cloudwatch:namespace": config.METRICS_NAMESPACE}},
            },
            {
                "Sid": "PublishPiAlerts",
                "Effect": "Allow",
                "Action": "sns:Publish",
                "Resource": config.topic_arn(account),
            },
            {
                "Sid": "InvokeDeepSeek",
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": config.foundation_model_arn(config.DEEPSEEK_MODEL_ID),
            },
            {
                "Sid": "InvokePiInferenceProfiles",
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": sorted({haiku_system, *profile_arns.values()}),
            },
            {
                "Sid": "InvokeHaikuThroughProfiles",
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": sorted(set(haiku_model_arns)),
                "Condition": {"StringEquals": {"bedrock:InferenceProfileArn": sorted(haiku_profiles)}},
            },
        ],
    }


# --- Shared IAM helpers (also used by scheduler_role.py and deploy_role.py) ----------------------
_INSTANCE_ID_IN_TEXT = re.compile(r"\bi-[0-9a-f]{8,17}\b")


def redact_instance_ids(text):
    """Instance IDs never reach the output (it may be pasted into chat or docs)."""
    return _INSTANCE_ID_IN_TEXT.sub("i-<instance>", str(text))


def _redact(text, account):
    """Known account and instance IDs first, then the catch-all for any other 12-digit number
    or instance ID (e.g. another account in a CloudTrail record of the audit)."""
    return _aws.mask_ids(_aws.redact_account(redact_instance_ids(text), account))


def _act(action, *, dry_run, account):
    """Announce an action: `[dry-run] would <action>`, or `-> <action>` just before doing it.
    The account ID and instance IDs are redacted."""
    action = _redact(action, account)
    return _aws.report(action if dry_run else f"-> {action}", dry_run=dry_run, account=account)


def _info(message, *, account):
    """Print one line (also the audit's output helper), redacted like `_act`."""
    return _aws.report(_redact(message, account), account=account)


def _missing(exc):
    """True for `NoSuchEntity`; any other error is re-raised by the caller."""
    return _aws.error_code(exc) == "NoSuchEntity"


def as_policy(value):
    """Policy document from AWS as a dict. boto3 already decodes IAM documents; a raw
    URL-encoded string is decoded here too, just in case."""
    if isinstance(value, dict):
        return value
    text = urllib.parse.unquote(value) if str(value).lstrip().startswith("%") else value
    return json.loads(text)


def ensure_role(iam, *, name, trust, description, policy_name, policy, account, dry_run,
                max_session=MAX_SESSION_SECONDS):
    """Create the role or bring it to the desired trust, description, tags and inline policy.
    Only `get_role`, `get_role_policy` and `list_role_policies` are called in dry-run. Returns
    True if the role existed before."""
    try:
        role = iam.get_role(RoleName=name)["Role"]
    except Exception as exc:  # noqa: BLE001  (re-raised unless the role is missing)
        if not _missing(exc):
            raise
        role = None

    if role is None:
        _act(f"create role {name} (tag {config.PROJECT_TAG_KEY}={config.PROJECT_TAG_VALUE})",
             dry_run=dry_run, account=account)
        if dry_run:
            _info(f"  trust policy:\n{json.dumps(trust, indent=2, sort_keys=True)}", account=account)
        else:
            iam.create_role(RoleName=name, AssumeRolePolicyDocument=_aws.policy_json(trust),
                            Description=description, MaxSessionDuration=max_session,
                            Tags=_aws.project_tags())
        current_policy = None
    else:
        _info(f"Role {name}: exists", account=account)
        current_trust = as_policy(role["AssumeRolePolicyDocument"])
        if not _aws.policies_equal(current_trust, trust):
            _info(_aws.json_diff(current_trust, trust, "trust policy"), account=account)
            _act(f"update the trust policy of {name}", dry_run=dry_run, account=account)
            if not dry_run:
                iam.update_assume_role_policy(RoleName=name, PolicyDocument=_aws.policy_json(trust))
        if role.get("Description") != description or role.get("MaxSessionDuration") != max_session:
            _act(f"update the description and session duration of {name}", dry_run=dry_run, account=account)
            if not dry_run:
                iam.update_role(RoleName=name, Description=description, MaxSessionDuration=max_session)
        add = _aws.tags_to_add(role.get("Tags"))
        if add:
            _act(f"tag role {name} with {add}", dry_run=dry_run, account=account)
            if not dry_run:
                iam.tag_role(RoleName=name, Tags=_aws.to_tag_list(add))
        try:
            current_policy = as_policy(iam.get_role_policy(RoleName=name, PolicyName=policy_name)["PolicyDocument"])
        except Exception as exc:  # noqa: BLE001
            if not _missing(exc):
                raise
            current_policy = None
        extra = sorted(set(iam.list_role_policies(RoleName=name)["PolicyNames"]) - {policy_name})
        for other in extra:
            _info(f"[WARN] role {name} has another inline policy '{other}' (left as is; review it)",
                  account=account)

    if current_policy is None or not _aws.policies_equal(current_policy, policy):
        _info(_aws.json_diff(current_policy or {}, policy, policy_name), account=account)
        _act(f"put inline policy {policy_name} on {name}", dry_run=dry_run, account=account)
        if not dry_run:
            iam.put_role_policy(RoleName=name, PolicyName=policy_name, PolicyDocument=_aws.policy_json(policy))
    else:
        _info(f"Inline policy {policy_name}: up to date", account=account)
    return role is not None


def ensure_managed_policies(iam, *, name, policy_arns, role_exists, account, dry_run):
    """Attach the managed policies that are missing; extra ones are reported, not detached."""
    attached = set()
    if role_exists:
        attached = {p["PolicyArn"] for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]}
    for arn in sorted(set(policy_arns) - attached):
        _act(f"attach managed policy {arn} to {name}", dry_run=dry_run, account=account)
        if not dry_run:
            iam.attach_role_policy(RoleName=name, PolicyArn=arn)
    for arn in sorted(attached - set(policy_arns)):
        _info(f"[WARN] role {name} also has managed policy {arn} (left as is; review it)", account=account)


def ensure_instance_profile(iam, *, profile_name, role_name, account, dry_run):
    """Create the instance profile (tagged) and put the role in it. An instance profile holds one
    role at most: if it holds another one, stop and let a person decide."""
    try:
        profile = iam.get_instance_profile(InstanceProfileName=profile_name)["InstanceProfile"]
    except Exception as exc:  # noqa: BLE001
        if not _missing(exc):
            raise
        profile = None

    if profile is None:
        _act(f"create instance profile {profile_name} (tagged)", dry_run=dry_run, account=account)
        if not dry_run:
            iam.create_instance_profile(InstanceProfileName=profile_name, Tags=_aws.project_tags())
        roles = []
    else:
        _info(f"Instance profile {profile_name}: exists", account=account)
        roles = [r["RoleName"] for r in profile.get("Roles", [])]
        add = _aws.tags_to_add(profile.get("Tags"))
        if add:
            _act(f"tag instance profile {profile_name} with {add}", dry_run=dry_run, account=account)
            if not dry_run:
                iam.tag_instance_profile(InstanceProfileName=profile_name, Tags=_aws.to_tag_list(add))

    if role_name in roles:
        return
    if roles:
        raise SystemExit(f"Instance profile {profile_name} holds role(s) {roles}, not {role_name}. "
                         "Not changed: fix it by hand or ask before replacing it.")
    _act(f"add role {role_name} to instance profile {profile_name}", dry_run=dry_run, account=account)
    if not dry_run:
        iam.add_role_to_instance_profile(InstanceProfileName=profile_name, RoleName=role_name)


def find_instance_ids(ec2, name=config.INSTANCE_NAME):
    """IDs of the non-terminated instances with this Name tag (read-only)."""
    ids, kwargs = [], {"Filters": [{"Name": "tag:Name", "Values": [name]},
                                   {"Name": "instance-state-name", "Values": list(LIVE_STATES)}]}
    while True:
        page = ec2.describe_instances(**kwargs)
        ids += [i["InstanceId"] for r in page.get("Reservations", []) for i in r.get("Instances", [])]
        if not page.get("NextToken"):
            return sorted(set(ids))
        kwargs["NextToken"] = page["NextToken"]


def find_instance_id(ec2, name=config.INSTANCE_NAME):
    """The one instance with this Name tag. Zero or several is an error (never guess)."""
    ids = find_instance_ids(ec2, name)
    if len(ids) != 1:
        raise SystemExit(f"Expected exactly one live instance named '{name}', found {len(ids)}.")
    if not _INSTANCE_ID.fullmatch(ids[0]):
        raise SystemExit("EC2 returned an unexpected instance ID format.")
    return ids[0]


# --- Bedrock reads -----------------------------------------------------------------------------
def read_profile_arns(bedrock):
    """{name: ARN} of the project's application inference profiles that exist (read-only)."""
    found, kwargs = {}, {"typeEquals": "APPLICATION"}
    while True:
        page = bedrock.list_inference_profiles(**kwargs)
        for summary in page.get("inferenceProfileSummaries", []):
            if summary.get("inferenceProfileName") in APPLICATION_PROFILES:
                found[summary["inferenceProfileName"]] = summary["inferenceProfileArn"]
        if not page.get("nextToken"):
            return found
        kwargs["nextToken"] = page["nextToken"]


def read_haiku_model_arns(bedrock, profile_arns):
    """Foundation-model ARNs the Haiku profiles route to, as AWS reports them (read-only)."""
    sources = [config.HAIKU_SYSTEM_PROFILE_ID]
    if config.HAIKU_PROFILE in profile_arns:
        sources.append(profile_arns[config.HAIKU_PROFILE])
    arns = set()
    for source in sources:
        profile = bedrock.get_inference_profile(inferenceProfileIdentifier=source)
        arns |= {m["modelArn"] for m in profile.get("models", [])}
    return sorted(arns)


# --- Audit of the old role (read-only) ---------------------------------------------------------
def _lookup(cloudtrail, key, value, start, end):
    events, kwargs = [], {"LookupAttributes": [{"AttributeKey": key, "AttributeValue": value}],
                          "StartTime": start, "EndTime": end, "MaxResults": 50}
    while True:
        page = cloudtrail.lookup_events(**kwargs)
        events += page.get("Events", [])
        if not page.get("NextToken"):
            return events
        kwargs["NextToken"] = page["NextToken"]


def _parse(events):
    """Unique CloudTrail records (the JSON inside `CloudTrailEvent`), by event ID."""
    out = {}
    for event in events:
        record = json.loads(event.get("CloudTrailEvent") or "{}")
        out[event.get("EventId") or record.get("eventID")] = record
    return list(out.values())


def _mask_session(session, pi_instance_ids):
    if session in pi_instance_ids:
        return f"{config.INSTANCE_NAME} (PI instance)"
    if "@" in session:
        return _aws.mask_email(session)
    if session.startswith("i-"):
        return f"i-***{session[-4:]} (another instance)"  # last 4 characters only, to find it in the console
    return f"{session[:3]}***"


def summarize_audit(role_arn, assume_records, session_records, pi_instance_ids, associated_instance_ids):
    """Pure summary of the old role's use. Inputs are parsed CloudTrail records:
    `assume_records` are AssumeRole* events on the role; `session_records` are the events made
    with any session name seen there (filtered here to sessions issued by this role).
    Returns a dict; `only_pi` is True only when there is evidence (at least one session) and
    every signal points to the PI instance alone. No session at all is inconclusive, never "only PI"."""
    pi = set(pi_instance_ids)
    assumed_by, sessions = Counter(), Counter()
    for record in assume_records:
        if not str(record.get("eventName", "")).startswith("AssumeRole"):
            continue
        who = record.get("userIdentity", {})
        assumed_by[who.get("invokedBy") or who.get("type") or "unknown"] += 1
        session = (record.get("requestParameters") or {}).get("roleSessionName")
        if session:
            sessions[session] += 1

    calls, secrets = Counter(), Counter()
    for record in session_records:
        issuer = record.get("userIdentity", {}).get("sessionContext", {}).get("sessionIssuer", {})
        if issuer.get("arn") != role_arn:
            continue
        calls[f"{record.get('eventSource', '?').split('.')[0]}:{record.get('eventName', '?')}"] += 1
        if str(record.get("eventSource", "")).startswith("secretsmanager."):
            secret = (record.get("requestParameters") or {}).get("secretId")
            if secret:
                secrets[secret] += 1

    other_sessions = sorted(s for s in sessions if s not in pi)
    other_instances = sorted(set(associated_instance_ids) - pi)
    other_principals = sorted(p for p in assumed_by if p != EC2_SERVICE)
    pi_secret = re.compile(r"(.*:secret:)?" + re.escape(config.SECRET_NAME) + r"(-[A-Za-z0-9]{6})?")
    other_secrets = sorted(s for s in secrets if not pi_secret.fullmatch(s))
    return {
        "assume_events": sum(assumed_by.values()),
        "assumed_by": dict(assumed_by),
        "sessions": dict(sessions),
        "calls": dict(calls),
        "secrets_read": dict(secrets),
        "other_sessions": other_sessions,
        "other_instances": other_instances,
        "other_principals": other_principals,
        "other_secrets": other_secrets,
        "evidence": bool(sessions),
        "only_pi": bool(pi) and bool(sessions) and not (
            other_sessions or other_instances or other_principals or other_secrets),
    }


def audit_old_role(session, account, *, now=None, role_name=config.OLD_INSTANCE_ROLE):
    """Read-only report of who used `role_name` in the last CLOUDTRAIL_LOOKBACK_DAYS days.
    Calls only Get/List/Describe/Lookup operations. Returns the summary dict (None if the role
    does not exist)."""
    iam = _aws.client(session, "iam")
    ec2 = _aws.client(session, "ec2")
    cloudtrail = _aws.client(session, "cloudtrail")
    end = now or dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=config.CLOUDTRAIL_LOOKBACK_DAYS)
    say = lambda message: _info(message, account=account)  # noqa: E731

    try:
        role = iam.get_role(RoleName=role_name)["Role"]
    except Exception as exc:  # noqa: BLE001
        if not _missing(exc):
            raise
        say(f"Role {role_name} does not exist: nothing to audit.")
        return None
    role_arn = role["Arn"]  # from IAM, not rebuilt: a role with a path has it in its ARN
    last = role.get("RoleLastUsed") or {}
    say(f"Role {role_name}: last used {last.get('LastUsedDate', 'never (or before tracking)')} "
        f"in {last.get('Region', '-')}")

    profile_arns = {p["Arn"] for p in iam.list_instance_profiles_for_role(RoleName=role_name)["InstanceProfiles"]}
    associated, kwargs = set(), {}
    while True:
        page = ec2.describe_iam_instance_profile_associations(**kwargs)
        for assoc in page.get("IamInstanceProfileAssociations", []):
            if assoc.get("IamInstanceProfile", {}).get("Arn") in profile_arns and \
                    assoc.get("State") in ("associating", "associated"):
                associated.add(assoc["InstanceId"])
        if not page.get("NextToken"):
            break
        kwargs["NextToken"] = page["NextToken"]
    pi_ids = find_instance_ids(ec2)

    assume = _parse(_lookup(cloudtrail, "ResourceName", role_arn, start, end)
                    + _lookup(cloudtrail, "ResourceName", role_name, start, end))
    names = sorted({(r.get("requestParameters") or {}).get("roleSessionName") for r in assume} - {None})
    by_session = []
    for name in names:
        by_session += _lookup(cloudtrail, "Username", name, start, end)
    summary = summarize_audit(role_arn, assume, _parse(by_session), pi_ids, associated)

    say(f"Audit of {role_name}, last {config.CLOUDTRAIL_LOOKBACK_DAYS} days, CloudTrail management "
        f"events in {config.REGION} (S3 object reads and other data events are not covered):")
    say(f"  instance profiles with this role: {len(profile_arns)}; instances using them: "
        f"{len(associated)} ({len(summary['other_instances'])} not the PI instance)")
    say(f"  AssumeRole events: {summary['assume_events']}, by {summary['assumed_by'] or 'nobody'}")
    for session_name, count in sorted(summary["sessions"].items()):
        say(f"    session {_mask_session(session_name, pi_ids)}: {count}")
    say("  API calls made with the role:")
    for call, count in sorted(summary["calls"].items(), key=lambda kv: (-kv[1], kv[0])):
        say(f"    {call}: {count}")
    say("  secrets read: " + (", ".join(f"{s} ({n})" for s, n in sorted(summary["secrets_read"].items()))
                              or "none"))
    if summary["only_pi"]:
        verdict = "only the PI instance"
    elif not summary["evidence"]:
        verdict = "INCONCLUSIVE: no session of this role found; review RoleLastUsed and CloudTrail by hand"
    else:
        verdict = "NOT only the PI instance: review before switching"
    say(f"  verdict: {verdict}")
    return summary


# --- Entry point -------------------------------------------------------------------------------
def build_parser():
    parser = _aws.base_parser(__doc__)
    parser.add_argument("--audit-old-role", action="store_true",
                        help=f"read-only report of who used {config.OLD_INSTANCE_ROLE} (CloudTrail, "
                             f"{config.CLOUDTRAIL_LOOKBACK_DAYS} days); changes nothing")
    return parser


def main(argv=None, session=None):
    args = build_parser().parse_args(argv)
    sess = _aws.get_session(session)
    account = _aws.account_id(sess)
    if args.audit_old_role:
        audit_old_role(sess, account)
        return 0

    iam = _aws.client(sess, "iam")
    bedrock = _aws.client(sess, "bedrock")
    profile_arns = read_profile_arns(bedrock)
    for name in APPLICATION_PROFILES:
        if name not in profile_arns:
            _info(f"[WARN] application inference profile {name} not found: left out of the policy "
                  "(run bedrock_profiles.py, then this script again)", account=account)
    try:
        policy = instance_policy(account, profile_arns, read_haiku_model_arns(bedrock, profile_arns))
    except ValueError as exc:
        raise SystemExit(f"Cannot build the instance policy: {_redact(str(exc), account)}") from None

    exists = ensure_role(iam, name=config.INSTANCE_ROLE, trust=trust_policy(), description=DESCRIPTION,
                         policy_name=POLICY_NAME, policy=policy, account=account, dry_run=args.dry_run)
    ensure_managed_policies(iam, name=config.INSTANCE_ROLE, policy_arns=MANAGED_POLICIES,
                            role_exists=exists, account=account, dry_run=args.dry_run)
    ensure_instance_profile(iam, profile_name=config.INSTANCE_PROFILE, role_name=config.INSTANCE_ROLE,
                            account=account, dry_run=args.dry_run)
    _info(f"Role: {config.role_arn(account, config.INSTANCE_ROLE)}", account=account)
    _info(f"Instance profile: {config.instance_profile_arn(account)} "
          "(associate it with `python -m infra.ec2_instance`)", account=account)
    if args.dry_run:
        _info("--dry-run: nothing was changed.", account=account)
    return 0


if __name__ == "__main__":
    _aws.cli_main(main)
