"""Create or update the GitHub OIDC provider and the deploy role GitHub Actions will use (F4).
Safe to run more than once.

What it leaves configured:
- IAM OIDC provider `token.actions.githubusercontent.com` with the audience `sts.amazonaws.com`
  (created if missing, tagged `project=price-intelligence`). There is one provider per URL in the
  account: if it already exists it is kept (no thumbprint change), the audience is added if
  missing, and it is only tagged if no other project tag is there (another project may own it).
  It is never deleted. No thumbprint is sent: IAM trusts GitHub's certificate authority itself.
- Role `price-intelligence-deploy` (tagged, 1-hour sessions) assumable only with a GitHub OIDC
  token whose `aud` is `sts.amazonaws.com` and whose `sub` is exactly
  `repo:Wayakit-Biotechnology-in-Hygiene/wayakit-price-prediction:ref:refs/heads/main`
  (`StringEquals`: no other branch, tag, pull request or environment).
- One inline policy, `price-intelligence-deploy-access`, and nothing else:
  - `s3:PutObject` on `releases/*` of the PI bucket (publish a release);
  - `s3:GetObject` and `s3:PutObject` on the `releases/current` pointer (read and move it);
  - `ec2:StartInstances` on the `wayakit-app-server` instance (resolved by Name tag at run time);
  - `ec2:CreateTags` on that instance, only for the `pi-mode` tag key (the F4 "deploy only" signal).

Cost: IAM is free.

Usage (from the repo root):
    python -m infra.deploy_role --dry-run
    python -m infra.deploy_role
"""

from infra import _aws, config
from infra.config import check_account
from infra.instance_role import _act, _info, _missing, ensure_role, find_instance_id
from infra.scheduler_role import _check_instance_id

POLICY_NAME = f"{config.PROJECT}-deploy-access"
DESCRIPTION = "Price Intelligence deploy from GitHub Actions (main only): releases, pointer, start instance"
MAX_SESSION_SECONDS = 3600


def trust_policy(account):
    """Web identity from GitHub's OIDC provider, only for the `main` branch of the PI repo."""
    check_account(account)
    return {
        "Version": config.POLICY_VERSION,
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Federated": config.oidc_provider_arn(account)},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {"StringEquals": {
                f"{config.GITHUB_OIDC_HOST}:aud": config.GITHUB_OIDC_AUDIENCE,
                f"{config.GITHUB_OIDC_HOST}:sub": config.DEPLOY_OIDC_SUBJECT,
            }},
        }],
    }


def deploy_policy(account, instance_id):
    """Publish releases, read and move the `current` pointer, start the instance and set its
    `pi-mode` tag. Nothing else."""
    check_account(account)
    _check_instance_id(instance_id)
    bucket = config.bucket_arn(account)
    instance = config.instance_arn(account, instance_id)
    return {
        "Version": config.POLICY_VERSION,
        "Statement": [
            {
                "Sid": "PublishReleases",
                "Effect": "Allow",
                "Action": "s3:PutObject",
                "Resource": f"{bucket}/{config.RELEASES_PREFIX}*",
            },
            {
                "Sid": "ReadAndMoveCurrentPointer",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": f"{bucket}/{config.RELEASES_CURRENT_KEY}",
            },
            {
                "Sid": "StartPiInstance",
                "Effect": "Allow",
                "Action": "ec2:StartInstances",
                "Resource": instance,
            },
            {
                "Sid": "SetPiModeTag",
                "Effect": "Allow",
                "Action": "ec2:CreateTags",
                "Resource": instance,
                "Condition": {"ForAllValues:StringEquals": {"aws:TagKeys": [config.PI_MODE_TAG_KEY]}},
            },
        ],
    }


def ensure_oidc_provider(iam, *, account, dry_run):
    """Create the GitHub OIDC provider if missing, or add the audience and tag to the existing one.
    Read calls only in dry-run. Returns the provider ARN."""
    arn = config.oidc_provider_arn(account)
    try:
        provider = iam.get_open_id_connect_provider(OpenIDConnectProviderArn=arn)
    except Exception as exc:  # noqa: BLE001  (re-raised unless the provider is missing)
        if not _missing(exc):
            raise
        provider = None

    if provider is None:
        _act(f"create OIDC provider {config.GITHUB_OIDC_URL} (audience {config.GITHUB_OIDC_AUDIENCE}, tagged)",
             dry_run=dry_run, account=account)
        if not dry_run:
            iam.create_open_id_connect_provider(Url=config.GITHUB_OIDC_URL,
                                                ClientIDList=[config.GITHUB_OIDC_AUDIENCE],
                                                Tags=_aws.project_tags())
        return arn

    _info(f"OIDC provider {config.GITHUB_OIDC_HOST}: exists", account=account)
    if config.GITHUB_OIDC_AUDIENCE not in provider.get("ClientIDList", []):
        _act(f"add audience {config.GITHUB_OIDC_AUDIENCE} to the OIDC provider", dry_run=dry_run, account=account)
        if not dry_run:
            iam.add_client_id_to_open_id_connect_provider(OpenIDConnectProviderArn=arn,
                                                          ClientID=config.GITHUB_OIDC_AUDIENCE)
    tags = _aws.tags_to_dict(provider.get("Tags"))
    owner = tags.get(config.PROJECT_TAG_KEY)
    if owner is None:
        _act(f"tag the OIDC provider with {config.PROJECT_TAG_KEY}={config.PROJECT_TAG_VALUE}",
             dry_run=dry_run, account=account)
        if not dry_run:
            iam.tag_open_id_connect_provider(OpenIDConnectProviderArn=arn, Tags=_aws.project_tags())
    elif owner != config.PROJECT_TAG_VALUE:
        _info(f"[WARN] the OIDC provider is tagged {config.PROJECT_TAG_KEY}={owner}: shared with another "
              "project, tags left as they are", account=account)
    return arn


def main(argv=None, session=None):
    args = _aws.base_parser(__doc__).parse_args(argv)
    sess = _aws.get_session(session)
    account = _aws.account_id(sess)
    instance_id = find_instance_id(_aws.client(sess, "ec2"))
    iam = _aws.client(sess, "iam")
    ensure_oidc_provider(iam, account=account, dry_run=args.dry_run)
    ensure_role(iam, name=config.DEPLOY_ROLE, trust=trust_policy(account), description=DESCRIPTION,
                policy_name=POLICY_NAME, policy=deploy_policy(account, instance_id), account=account,
                dry_run=args.dry_run, max_session=MAX_SESSION_SECONDS)
    _info(f"Role: {config.role_arn(account, config.DEPLOY_ROLE)} (for F4: aws-actions/configure-aws-credentials "
          "with role-to-assume)", account=account)
    if args.dry_run:
        _info("--dry-run: nothing was changed.", account=account)
    return 0


if __name__ == "__main__":
    _aws.cli_main(main)
