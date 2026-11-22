"""Shared configuration for the `infra/` scripts: profile, region, tag, resource names and cron.

Every name, prefix and limit used by more than one script lives here and only here. Nothing in
this module calls AWS or stores an account ID, email, IP or instance ID: the account comes from
STS at run time (`infra._aws.account_id`), the alert email is a CLI argument and the instance
is found by its Name tag. The ARN builders below are pure functions that take the account ID.
"""
import os
import re

from pi_secrets import SECRET_NAME  # noqa: F401  (re-exported; single source of the secret name)

# --- Account access ----------------------------------------------------------------------------
DEFAULT_PROFILE = "wayakit-vdc"
REGION = "eu-north-1"


def profile():
    """AWS CLI profile to use: `AWS_PROFILE` if set, else `wayakit-vdc`. Read at call time so a
    shell override (or a test) applies without re-importing. `infra._aws.get_session` refuses
    the profile `default` (CLAUDE.md: never `default`)."""
    return os.environ.get("AWS_PROFILE") or DEFAULT_PROFILE


# --- Tag and naming ----------------------------------------------------------------------------
PROJECT = "price-intelligence"  # prefix of every new resource name
PROJECT_TAG_KEY = "project"
PROJECT_TAG_VALUE = PROJECT
PROJECT_TAG = {"Key": PROJECT_TAG_KEY, "Value": PROJECT_TAG_VALUE}

# --- S3 bucket (infra/bucket.py) ---------------------------------------------------------------
DATA_PREFIX = "data/"
MODELS_PREFIX = "models/"
STATE_PREFIX = "state/"
LOGS_PREFIX = "logs/"
RELEASES_PREFIX = "releases/"
BUCKET_PREFIXES = (DATA_PREFIX, MODELS_PREFIX, STATE_PREFIX, LOGS_PREFIX, RELEASES_PREFIX)
NONCURRENT_EXPIRATION_DAYS = 30
LOGS_EXPIRATION_DAYS = 180
ABORT_MULTIPART_DAYS = 7

# --- Releases (F4: infra/release.py publishes, infra/instance/pi_deploy.py installs on boot) -----
# Keys under RELEASES_PREFIX are written only by the deploy role; the instance only reads them and
# writes its result under DEPLOYS_STATE_PREFIX. pi_deploy.py cannot import this module (stdlib
# only, outside the repo on the instance): it repeats these values and a unit test keeps them equal.
RELEASES_CURRENT_KEY = f"{RELEASES_PREFIX}current"  # "<sha>\n": the release every boot installs
RELEASES_PREVIOUS_KEY = f"{RELEASES_PREFIX}previous"  # "<sha>\n": the release before the last deploy
DEPLOY_REQUEST_KEY = f"{RELEASES_PREFIX}deploy-request.json"  # "deploy only" signal for the next boot
DEPLOYS_STATE_PREFIX = f"{STATE_PREFIX}deploys/"  # <request_id>.json: result written by the instance
DEPLOY_REQUEST_TTL_SECONDS = 1800  # a request is valid for 30 minutes after it is written
# Files every release archive must contain (checked by release.py before upload, by pi_deploy.py
# before the switch).
RELEASE_REQUIRED_FILES = (
    "run_pipeline.sh",
    "environment.yml",
    "infra/instance/pi_boot_guard.py",
    "infra/instance/pi-pipeline.service",
    "infra/instance/pi-deploy.service",
    "infra/instance/pi_deploy.py",
)
# Release folders kept on the instance (the current and previous targets always among them). The
# S3 archives are not pruned: a few hundred KB each, and old versions expire with the lifecycle.
RELEASES_TO_KEEP = 5

# --- IAM ---------------------------------------------------------------------------------------
INSTANCE_ROLE = f"{PROJECT}-instance"
INSTANCE_PROFILE = INSTANCE_ROLE  # same name as the role, like the console does
SCHEDULER_ROLE = f"{PROJECT}-scheduler"
DEPLOY_ROLE = f"{PROJECT}-deploy"
SSM_MANAGED_POLICY_ARN = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
POLICY_VERSION = "2012-10-17"
# Created by hand before F3. F3 never deletes or edits them (rollback = re-associate the old one).
OLD_INSTANCE_ROLE = "Wayakit-EC2-Secrets-Role"
OLD_SCHEDULER_ROLE = "Wayakit-EventBridge-Role"
CLOUDTRAIL_LOOKBACK_DAYS = 90  # LookupEvents only keeps 90 days of management events

# --- GitHub OIDC (infra/deploy_role.py, used in F4) --------------------------------------------
GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
GITHUB_OIDC_URL = f"https://{GITHUB_OIDC_HOST}"
GITHUB_OIDC_AUDIENCE = "sts.amazonaws.com"
GITHUB_ORG = "Wayakit-Biotechnology-in-Hygiene"
GITHUB_REPO = "wayakit-price-prediction"
DEPLOY_BRANCH = "main"  # Mafer, 2026-11-18: production branch at the end of the project
DEPLOY_OIDC_SUBJECT = f"repo:{GITHUB_ORG}/{GITHUB_REPO}:ref:refs/heads/{DEPLOY_BRANCH}"

# --- EC2 instance (infra/ec2_instance.py, infra/instance/) --------------------------------------
INSTANCE_NAME = "wayakit-app-server"  # Name tag; the instance ID is resolved at run time
INSTANCE_TYPE = "t3.medium"  # only for --create; adopt mode never changes the type
IMDS_HOP_LIMIT = 2
CONDA_ENV = "wayakit_env"  # hardcoded in run_pipeline.sh
INSTANCE_ENV_FILE = "/etc/price-intelligence.env"
PIPELINE_SERVICE = "pi-pipeline.service"
DEPLOY_SERVICE = "pi-deploy.service"  # installs releases/current on every boot (F4)
# F4 layout: INSTANCE_APP_DIR (the path pi-pipeline.service and the docs use) is a symlink to
# INSTANCE_RELEASES_DIR/<sha>; the deployer lives outside the releases so a bad release cannot
# break it.
INSTANCE_APP_DIR = "/home/ec2-user/wayakit-price-prediction"
INSTANCE_RELEASES_DIR = "/home/ec2-user/pi-releases"
INSTANCE_DEPLOYER_DIR = "/opt/price-intelligence"
# Variables written to INSTANCE_ENV_FILE (names only; no secrets go in that file).
ENV_LOG_DIR = "PI_LOG_DIR"
ENV_ALERTS_TOPIC_ARN = "PI_ALERTS_TOPIC_ARN"
ENV_PRIMARY_MODEL = "PI_RELEVANCE_PRIMARY_MODEL"  # same names scraper/services/ai_service.py reads
ENV_FALLBACK_MODEL = "PI_RELEVANCE_FALLBACK_MODEL"

# --- EventBridge Scheduler (infra/schedule.py) --------------------------------------------------
SCHEDULE_GROUP = PROJECT
SCHEDULE_NAME = f"{PROJECT}-monthly"
SCHEDULE_CRON = "cron(50 21 ? * 5L *)"  # last Thursday of the month, 21:50 local time
SCHEDULE_TIMEZONE = "America/Mexico_City"
SCHEDULE_TARGET_ARN = "arn:aws:scheduler:::aws-sdk:ec2:startInstances"  # universal target
RETIRED_SCHEDULE_GROUP = "default"
RETIRED_SCHEDULES = ("Wayakit-Start-Thursday-Night", "Wayakit-Start-Friday-Night")

# --- Alerts and metrics ------------------------------------------------------------------------
ALERTS_TOPIC = f"{PROJECT}-alerts"
SNS_KMS_KEY = "alias/aws/sns"  # AWS-managed key: no key cost
METRICS_NAMESPACE = "PriceIntelligence"

# --- Bedrock (infra/bedrock_profiles.py) --------------------------------------------------------
# Same defaults as scraper/services/ai_service.py (a unit test keeps them in sync).
DEEPSEEK_MODEL_ID = "deepseek.v3.2"
HAIKU_SYSTEM_PROFILE_ID = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
HAIKU_MODEL_ID = "anthropic.claude-haiku-4-5-20251001-v1:0"
# A profile cannot change model, so the name carries the version.
DEEPSEEK_PROFILE = f"{PROJECT}-deepseek-v3-2"
HAIKU_PROFILE = f"{PROJECT}-haiku-4-5"


# --- Names and ARNs (pure) ---------------------------------------------------------------------
# ASCII digits only: `\d` would also accept other Unicode digits, which no AWS account has.
_ACCOUNT = re.compile(r"[0-9]{12}")


def check_account(account):
    """Every policy builder takes the account from STS; anything else (e.g. `*` or ``) would yield
    wildcard or malformed ARNs in a policy, so it stops here. Raises ValueError; returns None."""
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        raise ValueError("account must be a 12-digit AWS account ID.")


def bucket_name(account):
    # Bucket names are global; the account ID makes it unique.
    return f"{PROJECT}-{account}"


def bucket_arn(account):
    return f"arn:aws:s3:::{bucket_name(account)}"


def arn(service, account, resource, region=REGION):
    """Generic ARN `arn:aws:<service>:<region>:<account>:<resource>`. Use `region=""` for global
    services (IAM)."""
    return f"arn:aws:{service}:{region}:{account}:{resource}"


def role_arn(account, role_name):
    return arn("iam", account, f"role/{role_name}", region="")


def instance_profile_arn(account, profile_name=INSTANCE_PROFILE):
    return arn("iam", account, f"instance-profile/{profile_name}", region="")


def oidc_provider_arn(account):
    return arn("iam", account, f"oidc-provider/{GITHUB_OIDC_HOST}", region="")


# Secrets Manager always appends "-" plus exactly 6 random characters to the name in the ARN.
SECRET_ARN_SUFFIX_LENGTH = 6


def secret_arn_pattern(account, secret_name=SECRET_NAME):
    """IAM resource pattern for the secret: `secret:<name>-??????`. Each `?` is one character, so
    it matches only this secret's ARN, never another secret whose name starts with `<name>-`
    (a trailing `*` would)."""
    return arn("secretsmanager", account, f"secret:{secret_name}-" + "?" * SECRET_ARN_SUFFIX_LENGTH)


def topic_arn(account, topic=ALERTS_TOPIC):
    return arn("sns", account, topic)


def instance_arn(account, instance_id):
    return arn("ec2", account, f"instance/{instance_id}")


def schedule_arn(account, name=SCHEDULE_NAME, group=SCHEDULE_GROUP):
    return arn("scheduler", account, f"schedule/{group}/{name}")


def schedule_group_arn(account, group=SCHEDULE_GROUP):
    return arn("scheduler", account, f"schedule-group/{group}")


def foundation_model_arn(model_id, region=REGION):
    """Foundation-model ARNs have no account. `region` may be a pattern such as `eu-*` if the
    caller has confirmed IAM accepts it there."""
    return f"arn:aws:bedrock:{region}::foundation-model/{model_id}"


def inference_profile_arn(account, profile_id, region=REGION):
    """ARN of a system-defined (cross-region) inference profile, e.g. HAIKU_SYSTEM_PROFILE_ID.
    Application profiles get a generated ID: read their ARN from AWS, do not build it."""
    return arn("bedrock", account, f"inference-profile/{profile_id}", region=region)


def bedrock_profile_sources(account):
    """Application profile name -> ARN it is copied from (`modelSource.copyFrom`)."""
    return {
        DEEPSEEK_PROFILE: foundation_model_arn(DEEPSEEK_MODEL_ID),
        HAIKU_PROFILE: inference_profile_arn(account, HAIKU_SYSTEM_PROFILE_ID),
    }
