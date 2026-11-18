"""Create or update the IAM role EventBridge Scheduler uses to start the PI instance. Safe to run
more than once.

What it leaves configured:
- Role `price-intelligence-scheduler` (tag `project=price-intelligence`), trusted only by
  `scheduler.amazonaws.com` and only for schedules in this account (`aws:SourceAccount`).
- One inline policy, `price-intelligence-scheduler-access`: `ec2:StartInstances` on the
  `wayakit-app-server` instance only. The instance ID is resolved at run time by its Name tag
  (exactly one live instance, otherwise it stops); if the instance is ever replaced, run this
  script again.

The old role `Wayakit-EventBridge-Role` (AmazonEC2FullAccess) is never edited or deleted; it
stops being used when `schedule.py` points the new schedule at this role and disables the old
schedules.

Cost: IAM is free.

Usage (from the repo root):
    python -m infra.scheduler_role --dry-run
    python -m infra.scheduler_role
"""
import re

from infra import _aws, config
from infra.config import check_account
from infra.instance_role import _info, ensure_role, find_instance_id

POLICY_NAME = f"{config.PROJECT}-scheduler-access"
DESCRIPTION = "Price Intelligence EventBridge Scheduler: start the PI instance only"
SCHEDULER_SERVICE = "scheduler.amazonaws.com"
_INSTANCE_ID = re.compile(r"i-[0-9a-f]{8,17}")


def _check_instance_id(instance_id):
    if not _INSTANCE_ID.fullmatch(str(instance_id)):
        raise ValueError("instance_id must be a single EC2 instance ID (i-...).")


def trust_policy(account):
    """Only EventBridge Scheduler, and only on behalf of this account (confused-deputy guard)."""
    check_account(account)
    return {
        "Version": config.POLICY_VERSION,
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": SCHEDULER_SERVICE},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": account}},
        }],
    }


def scheduler_policy(account, instance_id):
    """`ec2:StartInstances` on one instance; nothing else (no Stop, no Describe)."""
    check_account(account)
    _check_instance_id(instance_id)
    return {
        "Version": config.POLICY_VERSION,
        "Statement": [{
            "Sid": "StartPiInstance",
            "Effect": "Allow",
            "Action": "ec2:StartInstances",
            "Resource": config.instance_arn(account, instance_id),
        }],
    }


def main(argv=None, session=None):
    args = _aws.base_parser(__doc__).parse_args(argv)
    sess = _aws.get_session(session)
    account = _aws.account_id(sess)
    instance_id = find_instance_id(_aws.client(sess, "ec2"))
    ensure_role(_aws.client(sess, "iam"), name=config.SCHEDULER_ROLE, trust=trust_policy(account),
                description=DESCRIPTION, policy_name=POLICY_NAME,
                policy=scheduler_policy(account, instance_id), account=account, dry_run=args.dry_run)
    _info(f"Role: {config.role_arn(account, config.SCHEDULER_ROLE)} (used by `python -m infra.schedule`)",
          account=account)
    if args.dry_run:
        _info("--dry-run: nothing was changed.", account=account)
    return 0


if __name__ == "__main__":
    _aws.cli_main(main)
