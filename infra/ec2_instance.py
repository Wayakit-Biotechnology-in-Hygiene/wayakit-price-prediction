"""Adopts the Price Intelligence EC2 instance (`wayakit-app-server`) or creates it. Idempotent.

What it leaves configured (default "adopt" mode, on the existing instance found by its Name tag):
- Tags `project=price-intelligence` and `Name=wayakit-app-server` (other tags are kept).
- IMDSv2 required: `HttpTokens=required`, hop limit 2, metadata endpoint enabled.
- The instance profile `price-intelligence-instance` (created by `infra/instance_role.py`)
  associated with the instance. If another profile is associated it is REPLACED, and the previous
  profile name is printed: it is the rollback (`--rollback-profile <that name>`). The old role
  `Wayakit-EC2-Secrets-Role` is never deleted or edited.
The profile is checked before anything is changed: if it is missing (or has no role) the script
exits 1 with the instance untouched.
It never stops, starts, reboots or terminates the instance and never changes its type. Changing
the metadata options or the profile works on a stopped or a running instance; on a running one the
new role's credentials reach the processes within minutes. It also warns if the instance's
shutdown behavior is not `stop` (`run_pipeline.sh` ends with `shutdown -h`, which would TERMINATE
an instance set to `terminate`).

`--rollback-profile NAME`: only re-associates the instance profile NAME (tags and IMDS untouched).

`--create` (not used yet; documented and unit-tested only): launches a NEW t3.medium with the
latest Amazon Linux 2023 AMI (`resolve:ssm:` public parameter, no SSM call), IMDSv2 required, the
PI instance profile, an encrypted gp3 root volume, shutdown behavior `stop`, tags on the instance
and its volume, no key pair (access through SSM), and a userData that clones the repo
(`--repo-url`, branch `--branch`, default `main`) into /home/ec2-user/wayakit-price-prediction
and runs `infra/instance/bootstrap.sh`. It refuses if an instance with that Name tag already
exists (never a duplicate). The instance needs outbound internet (default VPC subnet with a public
IP, or `--subnet-id`). Cloning needs the repo to be readable without credentials: how a private
repo authenticates is still open (F4 replaces the clone with the S3 release).

`--dry-run`: only read calls (sts:GetCallerIdentity, ec2:DescribeInstances,
ec2:DescribeInstanceAttribute, ec2:DescribeIamInstanceProfileAssociations,
iam:GetInstanceProfile); prints the plan.

Instance IDs and the account ID are never printed (the instance is named by its Name tag).

Cost: none by itself in adopt mode. `--create` adds a t3.medium (on demand, eu-north-1, only while
running: one monthly run plus maintenance) and its EBS volume (charged even while stopped).

Usage (from the repo root, profile wayakit-vdc):
    python -m infra.ec2_instance --dry-run
    python -m infra.ec2_instance
    python -m infra.ec2_instance --rollback-profile <previous profile name> [--dry-run]
    python -m infra.ec2_instance --create --alerts-topic --primary-model <profile ARN> \
        --fallback-model <profile ARN> [--subnet-id ID] [--security-group-id ID] --dry-run
"""
import shlex

from infra import _aws, config

STATES_ALIVE = ("pending", "running", "stopping", "stopped")  # not shutting-down/terminated
SHUTDOWN_BEHAVIOR = "stop"
ROOT_DEVICE = "/dev/xvda"  # AL2023 root device name
ROOT_VOLUME_GB = 30  # conda env + Chrome + data; the AL2023 default (8 GB) is too small
AL2023_AMI = "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
REPO_URL = f"https://github.com/{config.GITHUB_ORG}/{config.GITHUB_REPO}.git"
INSTANCE_HOME = "/home/ec2-user"
REPO_DIR = f"{INSTANCE_HOME}/{config.GITHUB_REPO}"
DEFAULT_LOG_DIR = f"{INSTANCE_HOME}/logs"


# --- Pure builders ----------------------------------------------------------------------------
def desired_tags():
    """Tags the instance must have (as a dict)."""
    return _aws.tags_to_dict(_aws.project_tags({"Name": config.INSTANCE_NAME}))


def desired_metadata_options():
    """IMDSv2 required with hop limit 2 (AWS default for AL2023; leaves room for a container)."""
    return {"HttpTokens": "required", "HttpPutResponseHopLimit": config.IMDS_HOP_LIMIT,
            "HttpEndpoint": "enabled"}


def metadata_changes(current):
    """Keyword arguments for `modify_instance_metadata_options` (without InstanceId) if `current`
    (the instance's `MetadataOptions`) differs from the desired options, else {}."""
    desired = desired_metadata_options()
    current = current or {}
    if all(current.get(k) == v for k, v in desired.items()):
        return {}
    return dict(desired)


def instances_from_reservations(reservations):
    """Flat list of instances from `describe_instances` reservations."""
    return [i for r in reservations or [] for i in r.get("Instances", [])]


def select_instance(instances, name=config.INSTANCE_NAME):
    """The one instance named `name`. SystemExit if there are none or more than one (the
    schedule and the scripts resolve the instance by this tag, so it must be unique)."""
    if not instances:
        raise SystemExit(f"No instance with Name tag '{name}' (pending/running/stopping/stopped).")
    if len(instances) > 1:
        raise SystemExit(f"{len(instances)} instances have the Name tag '{name}'; it must be unique. "
                         "Fix it by hand before running this script.")
    return instances[0]


def profile_name_from_arn(arn):
    """`arn:aws:iam::<account>:instance-profile/<path/>name` -> `name`."""
    return str(arn).rsplit("/", 1)[-1]


def association_plan(associations, desired_arn):
    """What to do with the instance profile association.

    `associations` are the `IamInstanceProfileAssociations` of the instance in state associating
    or associated. Returns a dict: action ("none" | "associate" | "replace"), association_id
    (for replace, else None) and previous_profile (name of the profile associated now, or None).
    Compares by profile name (the last ARN segment): AWS may report the same profile with a path.
    """
    current = [a for a in associations or [] if a.get("State") in ("associating", "associated")]
    if not current:
        return {"action": "associate", "association_id": None, "previous_profile": None}
    assoc = current[0]
    previous = profile_name_from_arn(assoc["IamInstanceProfile"]["Arn"])
    if previous == profile_name_from_arn(desired_arn):
        return {"action": "none", "association_id": assoc["AssociationId"], "previous_profile": previous}
    return {"action": "replace", "association_id": assoc["AssociationId"], "previous_profile": previous}


def bootstrap_args(log_dir=DEFAULT_LOG_DIR, topic_arn=None, primary_model=None, fallback_model=None):
    """Arguments for `infra/instance/bootstrap.sh` (only the values given)."""
    args = ["--log-dir", log_dir]
    for flag, value in (("--alerts-topic-arn", topic_arn), ("--primary-model", primary_model),
                        ("--fallback-model", fallback_model)):
        if value:
            args += [flag, value]
    return args


def user_data(repo_url=REPO_URL, branch=config.DEPLOY_BRANCH, bootstrap=None):
    """First-boot script for `--create`: clone the repo as ec2-user (skipped if it is already
    there) and run bootstrap.sh as root. Every value is shell-quoted."""
    bootstrap = bootstrap_args() if bootstrap is None else list(bootstrap)
    q = shlex.quote
    return "\n".join([
        "#!/bin/bash",
        "# Price Intelligence first boot (infra/ec2_instance.py --create).",
        "set -euo pipefail",
        "dnf install -y git",
        f"if [ ! -d {q(REPO_DIR)}/.git ]; then",
        f"  runuser -u ec2-user -- git clone --branch {q(branch)} --depth 1 {q(repo_url)} {q(REPO_DIR)}",
        "fi",
        f"bash {q(REPO_DIR + '/infra/instance/bootstrap.sh')} {' '.join(q(a) for a in bootstrap)}",
        "",
    ])


def run_instances_params(user_data_text, subnet_id=None, security_group_ids=(),
                         volume_gb=ROOT_VOLUME_GB, image_id=AL2023_AMI):
    """Keyword arguments for `ec2.run_instances` (`--create`)."""
    tags = _aws.project_tags({"Name": config.INSTANCE_NAME})
    params = {
        "ImageId": image_id,
        "InstanceType": config.INSTANCE_TYPE,
        "MinCount": 1,
        "MaxCount": 1,
        "IamInstanceProfile": {"Name": config.INSTANCE_PROFILE},
        "MetadataOptions": desired_metadata_options(),
        "InstanceInitiatedShutdownBehavior": SHUTDOWN_BEHAVIOR,
        "BlockDeviceMappings": [{"DeviceName": ROOT_DEVICE, "Ebs": {
            "VolumeSize": int(volume_gb), "VolumeType": "gp3", "Encrypted": True,
            "DeleteOnTermination": True}}],
        "TagSpecifications": [{"ResourceType": "instance", "Tags": tags},
                              {"ResourceType": "volume", "Tags": tags}],
        "UserData": user_data_text,
    }
    if subnet_id:
        params["SubnetId"] = subnet_id
    if security_group_ids:
        params["SecurityGroupIds"] = list(security_group_ids)
    return params


# --- AWS reads --------------------------------------------------------------------------------
def find_instances(ec2, name=config.INSTANCE_NAME):
    """Instances with the Name tag `name` that are not terminated (follows NextToken)."""
    kwargs = {"Filters": [{"Name": "tag:Name", "Values": [name]},
                          {"Name": "instance-state-name", "Values": list(STATES_ALIVE)}]}
    found = []
    while True:
        resp = ec2.describe_instances(**kwargs)
        found += instances_from_reservations(resp.get("Reservations"))
        if not resp.get("NextToken"):
            return found
        kwargs["NextToken"] = resp["NextToken"]


def get_profile_arn(iam, name):
    """ARN of the instance profile `name`, or None if it does not exist. SystemExit if it exists
    without a role (associating it would leave the instance with no credentials)."""
    try:
        profile = iam.get_instance_profile(InstanceProfileName=name)["InstanceProfile"]
    except Exception as exc:  # botocore ClientError; only NoSuchEntity means "missing"
        if _aws.error_code(exc) == "NoSuchEntity":
            return None
        raise
    if not profile.get("Roles"):
        raise SystemExit(f"Instance profile '{name}' has no role; run infra/instance_role.py first.")
    return profile["Arn"]


def current_associations(ec2, instance_id):
    resp = ec2.describe_iam_instance_profile_associations(
        Filters=[{"Name": "instance-id", "Values": [instance_id]},
                 {"Name": "state", "Values": ["associating", "associated"]}])
    return resp.get("IamInstanceProfileAssociations", [])


# --- Steps ------------------------------------------------------------------------------------
def ensure_tags(ec2, instance, dry_run, account):
    missing = _aws.tags_to_add(instance.get("Tags"), desired_tags())
    if not missing:
        _aws.report("tags: OK", account=account)
        return False
    _aws.report(f"add tags {sorted(missing)} to the instance", dry_run=dry_run, account=account)
    if not dry_run:
        ec2.create_tags(Resources=[instance["InstanceId"]], Tags=_aws.to_tag_list(missing))
    return True


def ensure_imdsv2(ec2, instance, dry_run, account):
    changes = metadata_changes(instance.get("MetadataOptions"))
    if not changes:
        _aws.report("IMDSv2: OK (required, hop limit 2)", account=account)
        return False
    current = {k: (instance.get("MetadataOptions") or {}).get(k) for k in changes}
    _aws.report(f"set metadata options {current} -> {changes}", dry_run=dry_run, account=account)
    if not dry_run:
        ec2.modify_instance_metadata_options(InstanceId=instance["InstanceId"], **changes)
    return True


def check_shutdown_behavior(ec2, instance, account):
    value = ec2.describe_instance_attribute(
        InstanceId=instance["InstanceId"], Attribute="instanceInitiatedShutdownBehavior",
    )["InstanceInitiatedShutdownBehavior"]["Value"]
    if value != SHUTDOWN_BEHAVIOR:
        _aws.report(f"WARNING: shutdown behavior is '{value}', not '{SHUTDOWN_BEHAVIOR}': the "
                    "pipeline's 'shutdown -h' would terminate the instance. Not changed here.",
                    account=account)
    else:
        _aws.report("shutdown behavior: stop", account=account)
    return value


def resolve_profile(iam, profile_name, dry_run, account):
    """ARN of `profile_name`. Called BEFORE any change, so an apply with a missing (or role-less)
    profile exits 1 having changed nothing. In --dry-run a missing profile only warns (None)."""
    arn = get_profile_arn(iam, profile_name)
    if arn is None:
        message = (f"instance profile '{profile_name}' does not exist"
                   + (" (run infra/instance_role.py first)" if profile_name == config.INSTANCE_PROFILE else ""))
        if dry_run:
            _aws.report(f"WARNING: {message}; it could not be associated now", account=account)
            return None
        raise SystemExit(message[0].upper() + message[1:] + ".")
    return arn


def ensure_profile(ec2, instance, profile_name, arn, dry_run, account):
    """Associates `profile_name` (ARN from resolve_profile); returns the plan (see association_plan)."""
    if arn is None:  # dry-run with a missing profile: resolve_profile already warned
        return {"action": "missing", "association_id": None, "previous_profile": None}
    plan = association_plan(current_associations(ec2, instance["InstanceId"]), arn)
    if plan["action"] == "none":
        _aws.report(f"instance profile: OK ({profile_name})", account=account)
        return plan
    if plan["previous_profile"]:
        _aws.report(f"previous instance profile: {plan['previous_profile']} (rollback: "
                    f"python -m infra.ec2_instance --rollback-profile {plan['previous_profile']})",
                    account=account)
    _aws.report(f"{plan['action']} instance profile {profile_name}", dry_run=dry_run, account=account)
    if not dry_run:
        if plan["action"] == "replace":
            ec2.replace_iam_instance_profile_association(
                IamInstanceProfile={"Arn": arn}, AssociationId=plan["association_id"])
        else:
            ec2.associate_iam_instance_profile(
                IamInstanceProfile={"Arn": arn}, InstanceId=instance["InstanceId"])
    return plan


def create(ec2, iam, args, account):
    if find_instances(ec2):
        raise SystemExit(f"An instance named '{config.INSTANCE_NAME}' already exists: use adopt mode "
                         "(without --create). --create never makes a second one.")
    if get_profile_arn(iam, config.INSTANCE_PROFILE) is None:
        if not args.dry_run:
            raise SystemExit(f"Instance profile '{config.INSTANCE_PROFILE}' does not exist; "
                             "run infra/instance_role.py first.")
        _aws.report(f"WARNING: instance profile '{config.INSTANCE_PROFILE}' does not exist yet",
                    account=account)
    topic = config.topic_arn(account) if args.alerts_topic else None
    data = user_data(args.repo_url, args.branch, bootstrap_args(
        args.log_dir, topic, args.primary_model, args.fallback_model))
    params = run_instances_params(data, args.subnet_id, args.security_group_id or (), args.volume_gb)
    _aws.report(f"launch a {config.INSTANCE_TYPE} '{config.INSTANCE_NAME}' (AL2023, IMDSv2, profile "
                f"{config.INSTANCE_PROFILE}, {args.volume_gb} GB gp3, branch {args.branch})",
                dry_run=args.dry_run, account=account)
    if not args.dry_run:
        ec2.run_instances(**params)
        _aws.report("launched; bootstrap.sh runs on the first boot (see /var/log/cloud-init-output.log)",
                    account=account)
    return params


# --- CLI --------------------------------------------------------------------------------------
def parse_args(argv=None):
    parser = _aws.base_parser(__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rollback-profile", metavar="NAME",
                      help="re-associate this instance profile (e.g. the one printed as previous)")
    mode.add_argument("--create", action="store_true", help="launch a new instance (see above)")
    parser.add_argument("--subnet-id", help="--create: subnet (default: the default VPC's)")
    parser.add_argument("--security-group-id", action="append",
                        help="--create: security group (repeatable; default: the VPC default)")
    parser.add_argument("--volume-gb", type=int, default=ROOT_VOLUME_GB, help="--create: root volume size")
    parser.add_argument("--repo-url", default=REPO_URL, help="--create: repository to clone")
    parser.add_argument("--branch", default=config.DEPLOY_BRANCH, help="--create: branch to clone")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help="--create: PI_LOG_DIR on the instance")
    parser.add_argument("--alerts-topic", action="store_true",
                        help=f"--create: set PI_ALERTS_TOPIC_ARN to the {config.ALERTS_TOPIC} topic")
    parser.add_argument("--primary-model", help="--create: PI_RELEVANCE_PRIMARY_MODEL (profile ARN or ID)")
    parser.add_argument("--fallback-model", help="--create: PI_RELEVANCE_FALLBACK_MODEL (profile ARN or ID)")
    return parser.parse_args(argv)


def main(argv=None, session=None):
    args = parse_args(argv)
    sess = _aws.get_session(session)
    account = _aws.account_id(sess)
    ec2 = _aws.client(sess, "ec2")
    iam = _aws.client(sess, "iam")
    if args.create:
        create(ec2, iam, args, account)
        return 0
    instance = select_instance(find_instances(ec2))
    _aws.report(f"instance '{config.INSTANCE_NAME}': {instance['State']['Name']}", account=account)
    if args.rollback_profile:
        arn = resolve_profile(iam, args.rollback_profile, args.dry_run, account)
        ensure_profile(ec2, instance, args.rollback_profile, arn, args.dry_run, account)
        return 0
    # Reads and the profile check first: a missing profile must stop the run before the tags or
    # the metadata options are changed (no half-applied adopt).
    check_shutdown_behavior(ec2, instance, account)
    arn = resolve_profile(iam, config.INSTANCE_PROFILE, args.dry_run, account)
    ensure_tags(ec2, instance, args.dry_run, account)
    ensure_imdsv2(ec2, instance, args.dry_run, account)
    ensure_profile(ec2, instance, config.INSTANCE_PROFILE, arn, args.dry_run, account)
    return 0


if __name__ == "__main__":
    _aws.cli_main(main)
