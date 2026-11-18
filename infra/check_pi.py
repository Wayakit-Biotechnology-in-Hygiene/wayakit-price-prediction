"""Read-only check of the Price Intelligence AWS resources against `infra/config.py`.

It changes nothing: every AWS call is a `Describe*`, `Get*` or `List*` (STS
`GetCallerIdentity` included). It prints one line per check, `[OK]`, `[WARN]` or `[FAIL]`, and
exits 1 if there is any FAIL (WARN lines do not change the exit code). Use it after applying
the `infra/` scripts, and to see a change made from code in AWS (for example a new cron in
`config.SCHEDULE_CRON`: apply `python -m infra.schedule`, then this check shows the cron, the
time zone and the state AWS has).

What it checks:
- Bucket `price-intelligence-<account>`: region, SSE-S3 (AES256) with bucket key, the four
  Block Public Access flags, versioning, `BucketOwnerEnforced`, TLS-only bucket policy and no
  public Allow (Principal `"*"`, `{"AWS": "*"}` or `{"AWS": ["*"]}`, or any NotPrincipal),
  lifecycle (noncurrent versions 30 days, `logs/` 180 days, abort multipart 7 days, nothing
  expires current objects outside `logs/`), project tag. Every bucket read passes
  `ExpectedBucketOwner` (the account from STS): a bucket with PI's name in another account is
  never read; S3 answers AccessDenied and the section FAILs.
- Instance `wayakit-app-server` (by Name tag; exactly one): IMDSv2 required, hop limit 2,
  project tag, instance profile `price-intelligence-instance` associated.
- Bedrock: the Haiku system profile's routing regions, and the two application inference
  profiles (ACTIVE, copied from the expected model, tagged). Their ARNs are printed: they are
  the values of `PI_RELEVANCE_PRIMARY_MODEL` / `PI_RELEVANCE_FALLBACK_MODEL` on the instance. A
  missing DeepSeek profile is a WARN (documented fallback: the plain model ID).
- IAM: the instance role, its instance profile, the scheduler role and the deploy role: trust
  policy, tags, attached AWS-managed policies and permissions. Permissions are checked by
  meaning, not text: each required grant must be allowed, a set of look-alike resources (other
  buckets, including names with PI's bucket name as prefix or suffix, other secrets, including
  one whose name starts with PI's, other topic, other model, other instance) must not be, no
  wildcard actions, `Resource: "*"` only for `cloudwatch:PutMetricData` (it has no
  resource-level permissions; it must carry the `cloudwatch:namespace` condition),
  `ec2:CreateTags` limited to the `pi-mode` tag key. Any action outside the spec is a FAIL. The
  old roles must still exist (rollback).
- GitHub OIDC provider and the deploy role's trust (only `refs/heads/main`, StringEquals on
  `sub` and `aud`).
- Schedule group and `price-intelligence-monthly`: cron, time zone, flexible window OFF,
  universal target, scheduler role, input with the instance, no retries, state. The retired
  `default` schedules must be DISABLED or deleted. A DISABLED monthly schedule is a WARN (it is
  created disabled until F2 is deployed).
- SNS topic `price-intelligence-alerts`: SSE with `alias/aws/sns`, tag, and at least one
  confirmed email subscription (emails are printed masked).

The output never shows the account ID (`<account>`), the instance ID (`<instance-id>`) or a full
email address, so it can be pasted into chat or docs.

Cost: none in practice. Configuration reads in IAM, EC2, Scheduler, SNS, Bedrock and STS are
free; the S3 bucket-configuration reads are a handful of requests (fractions of a cent).

Usage (from the repo root):
    python -m infra.check_pi
    python -m infra.check_pi --only schedule iam
`--dry-run` is accepted for symmetry with the other scripts and changes nothing (the check is
read-only either way).
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from dataclasses import dataclass, field

from infra import _aws, config

OK, WARN, FAIL = "OK", "WARN", "FAIL"
SECTIONS = ("bucket", "instance", "bedrock", "iam", "oidc", "schedule", "alerts")

# The only action allowed on `Resource: "*"`: PutMetricData has no resource-level permissions
# (it is scoped with the cloudwatch:namespace condition instead).
STAR_RESOURCE_ACTIONS = frozenset({"cloudwatch:putmetricdata"})
AWS_MANAGED_PREFIX = "arn:aws:iam::aws:policy/"
ACTIVE_INSTANCE_STATES = ("pending", "running", "stopping", "stopped")
# Look-alike resources used to prove a policy is not broader than it should be.
OTHER_BUCKET = "other-bucket"
OTHER_INSTANCE_ID = "i-00000000000000000"
OTHER_MODEL_ID = "amazon.nova-pro-v1:0"
SECRET_SUFFIX = "AbCdEf"  # config.SECRET_ARN_SUFFIX_LENGTH characters, like the ones Secrets Manager appends


# --- Results -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    status: str
    label: str
    detail: str = ""

    def line(self):
        return f"[{self.status}] {self.label}" + (f": {self.detail}" if self.detail else "")


@dataclass(frozen=True)
class Missing:
    """A resource or configuration AWS reports as absent (the error code is kept)."""
    code: str


def _ok(label, detail=""):
    return Finding(OK, label, detail)


def _warn(label, detail=""):
    return Finding(WARN, label, detail)


def _fail(label, detail=""):
    return Finding(FAIL, label, detail)


def _expect(cond, label, good, bad, *, warn=False):
    return Finding(OK if cond else (WARN if warn else FAIL), label, good if cond else bad)


def has_failures(findings):
    return any(f.status == FAIL for f in findings)


def summary(findings):
    counts = {s: sum(1 for f in findings if f.status == s) for s in (OK, WARN, FAIL)}
    return f"{counts[OK]} OK, {counts[WARN]} WARN, {counts[FAIL]} FAIL"


def redact(text, account=None, instance_id=None):
    """Remove the account ID and the instance ID from a line before it is printed."""
    text = _aws.redact_account(text, account)
    return text.replace(instance_id, "<instance-id>") if instance_id else text


# --- Policy helpers (pure) ---------------------------------------------------------------------
def as_document(doc):
    """Policy document as a dict. IAM may hand it back URL-encoded or as JSON text."""
    if doc is None or isinstance(doc, dict):
        return doc
    text = str(doc)
    if not text.lstrip().startswith("{"):
        text = urllib.parse.unquote(text)
    return json.loads(text)


def _as_list(value):
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def statements(doc):
    return [s for s in _as_list((as_document(doc) or {}).get("Statement")) if isinstance(s, dict)]


def wildcard_match(pattern, value, *, ignore_case=False):
    """IAM wildcard match: `*` is any run of characters (`/` and `:` included), `?` is one."""
    regex = "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern)
    flags = re.DOTALL | (re.IGNORECASE if ignore_case else 0)
    return re.fullmatch(regex, value, flags) is not None


def _matches_action(stmt, action):
    if "NotAction" in stmt:
        return not any(wildcard_match(p, action, ignore_case=True) for p in _as_list(stmt["NotAction"]))
    return any(wildcard_match(p, action, ignore_case=True) for p in _as_list(stmt.get("Action")))


def _matches_resource(stmt, resource):
    if "NotResource" in stmt:
        return not any(wildcard_match(p, resource) for p in _as_list(stmt["NotResource"]))
    return any(wildcard_match(p, resource) for p in _as_list(stmt.get("Resource")))


def allows(stmts, action, resource):
    """Simplified IAM evaluation of identity-policy statements. Conditions are ignored on Allow
    (a conditional grant counts as a grant; the conditions that matter are checked apart) and an
    unconditional matching Deny wins."""
    for s in stmts:
        if s.get("Effect") == "Deny" and not s.get("Condition") \
                and _matches_action(s, action) and _matches_resource(s, resource):
            return False
    return any(s.get("Effect") == "Allow" and _matches_action(s, action) and _matches_resource(s, resource)
               for s in stmts)


def granted_actions(stmts):
    return {a.lower() for s in stmts if s.get("Effect") == "Allow" for a in _as_list(s.get("Action"))}


def broad_grants(stmts, star_resource_actions=STAR_RESOURCE_ACTIONS):
    """Problems that make a policy broader than least privilege: NotAction/NotResource in an
    Allow, wildcard actions, and `Resource: "*"` for actions that support resource ARNs."""
    problems = []
    for i, s in enumerate(stmts):
        if s.get("Effect") != "Allow":
            continue
        sid = s.get("Sid") or f"#{i}"
        if "NotAction" in s or "NotResource" in s:
            problems.append(f"statement {sid} uses NotAction/NotResource")
        actions = _as_list(s.get("Action"))
        problems += [f"statement {sid} has wildcard action {a}" for a in actions if "*" in a or "?" in a]
        if "*" in _as_list(s.get("Resource")):
            extra = sorted({a.lower() for a in actions} - star_resource_actions)
            if extra:
                problems.append(f"statement {sid} grants {', '.join(extra)} on Resource *")
    return problems


def condition_values(stmt, key):
    """{operator: [values]} for one condition key in a statement (key compared without case)."""
    out = {}
    for operator, block in (stmt.get("Condition") or {}).items():
        for k, v in (block or {}).items():
            if k.lower() == key.lower():
                out[operator] = [str(x).lower() if isinstance(x, bool) else str(x) for x in _as_list(v)]
    return out


@dataclass(frozen=True)
class Probe:
    action: str
    resource: str


@dataclass(frozen=True)
class ConditionRule:
    """Every Allow statement that grants `action` must restrict `key` with `operator` to exactly
    `values`."""
    action: str
    key: str
    operator: str
    values: frozenset


@dataclass(frozen=True)
class RoleExpectation:
    must_allow: tuple = ()
    must_deny: tuple = ()
    expected_actions: frozenset = frozenset()
    conditions: tuple = ()
    aws_managed: frozenset = frozenset()  # AWS-managed policies that must be attached (and no others)
    skipped: tuple = ()  # probes that could not be built (e.g. instance not found)


def evaluate_permissions(label, stmts, exp):
    """Findings for the permission statements of one role against its expectation."""
    out = []
    missing = [f"{p.action} on {p.resource}" for p in exp.must_allow if not allows(stmts, p.action, p.resource)]
    out.append(_expect(not missing, f"{label} required permissions",
                       f"{len(exp.must_allow)} grant(s) present", "missing " + "; ".join(missing)))
    broad = [f"{p.action} on {p.resource}" for p in exp.must_deny if allows(stmts, p.action, p.resource)]
    out.append(_expect(not broad, f"{label} least privilege",
                       f"{len(exp.must_deny)} look-alike resources denied", "also allows " + "; ".join(broad)))
    problems = broad_grants(stmts)
    out.append(_expect(not problems, f"{label} wildcards", "no wildcard actions or broad resources",
                       "; ".join(problems)))
    extra = sorted(granted_actions(stmts) - {a.lower() for a in exp.expected_actions})
    # An extra action is a FAIL even on a specific resource: the spec is the whole grant.
    out.append(_expect(not extra, f"{label} actions", "only the expected actions",
                       "not in the spec: " + ", ".join(extra)))
    for rule in exp.conditions:
        granting = [s for s in stmts if s.get("Effect") == "Allow" and _matches_action(s, rule.action)]
        good = bool(granting) and all(
            set(condition_values(s, rule.key).get(rule.operator, ())) == set(rule.values) for s in granting)
        want = f"{rule.operator} {rule.key} = {sorted(rule.values)}"
        out.append(_expect(good, f"{label} condition on {rule.action}", want,
                           f"expected {want} on every statement granting it"))
    if exp.skipped:
        out.append(_warn(f"{label} probes skipped", "; ".join(exp.skipped)))
    return out


# --- Expected permissions (pure) ---------------------------------------------------------------
def _object_arn(account, key, bucket=None):
    return f"arn:aws:s3:::{bucket or config.bucket_name(account)}/{key}"


def lookalike_buckets(account):
    """Buckets a least-privilege policy must not reach. Besides an unrelated name, two that share
    PI's bucket name as prefix or suffix: a wildcard on the name (`price-intelligence-*`,
    `*-<account>`) would also match buckets PI does not own (bucket names are global)."""
    name = config.bucket_name(account)
    return (OTHER_BUCKET, f"{name}-other", f"other-{name}")


def secret_arn(account, secret_name=config.SECRET_NAME, suffix=SECRET_SUFFIX):
    """ARN a secret named `secret_name` would have (Secrets Manager appends `-<6 characters>`)."""
    return config.arn("secretsmanager", account, f"secret:{secret_name}-{suffix}")


def lookalike_secrets(account):
    """Secrets a least-privilege policy must not reach: an unrelated one, and one whose name
    starts with PI's (`prod/wayakit-app-*` would match it; `prod/wayakit-app-??????` does not)."""
    return (secret_arn(account, "prod/other"), secret_arn(account, f"{config.SECRET_NAME}-other"))


def instance_role_expectation(account, app_profile_arns=(), haiku_model_arns=()):
    """What the instance role must (and must not) be allowed to do. `app_profile_arns` are the
    application inference profiles read from Bedrock; `haiku_model_arns` are the foundation
    models the `eu.` Haiku system profile routes to (also read from Bedrock). Without them only
    the Haiku model in REGION is probed."""
    secret = secret_arn(account)
    haiku_models = tuple(haiku_model_arns) or (config.foundation_model_arn(config.HAIKU_MODEL_ID),)
    must_allow = [
        Probe("secretsmanager:GetSecretValue", secret),
        Probe("s3:ListBucket", config.bucket_arn(account)),
        *[Probe(a, _object_arn(account, f"{prefix}probe"))
          for prefix in config.BUCKET_PREFIXES for a in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject")],
        Probe("cloudwatch:PutMetricData", "*"),
        Probe("sns:Publish", config.topic_arn(account)),
        Probe("bedrock:InvokeModel", config.foundation_model_arn(config.DEEPSEEK_MODEL_ID)),
        Probe("bedrock:InvokeModel", config.inference_profile_arn(account, config.HAIKU_SYSTEM_PROFILE_ID)),
        *[Probe("bedrock:InvokeModel", a) for a in haiku_models],
        *[Probe("bedrock:InvokeModel", a) for a in app_profile_arns],
    ]
    must_deny = [
        *[Probe("secretsmanager:GetSecretValue", other) for other in lookalike_secrets(account)],
        *[p for other in lookalike_buckets(account)
          for p in (Probe("s3:ListBucket", f"arn:aws:s3:::{other}"),
                    *[Probe(a, _object_arn(account, f"{prefix}probe", other))
                      for prefix in config.BUCKET_PREFIXES for a in ("s3:GetObject", "s3:PutObject")])],
        Probe("sns:Publish", config.topic_arn(account, "other-topic")),
        Probe("bedrock:InvokeModel", config.foundation_model_arn(OTHER_MODEL_ID)),
        Probe("bedrock:InvokeModel", config.inference_profile_arn(account, f"eu.{OTHER_MODEL_ID}")),
        Probe("iam:PassRole", config.role_arn(account, config.INSTANCE_ROLE)),
    ]
    return RoleExpectation(
        must_allow=tuple(must_allow), must_deny=tuple(must_deny),
        expected_actions=frozenset({"secretsmanager:GetSecretValue", "s3:ListBucket", "s3:GetObject",
                                    "s3:PutObject", "s3:DeleteObject", "cloudwatch:PutMetricData",
                                    "sns:Publish", "bedrock:InvokeModel"}),
        conditions=(ConditionRule("cloudwatch:PutMetricData", "cloudwatch:namespace", "StringEquals",
                                  frozenset({config.METRICS_NAMESPACE})),),
        aws_managed=frozenset({config.SSM_MANAGED_POLICY_ARN}),
        skipped=() if app_profile_arns else ("application inference profiles not found",),
    )


def scheduler_role_expectation(account, instance_id=None):
    must_allow, skipped = [], []
    if instance_id:
        must_allow.append(Probe("ec2:StartInstances", config.instance_arn(account, instance_id)))
    else:
        skipped.append("instance not found")
    must_deny = [Probe("ec2:StartInstances", config.instance_arn(account, OTHER_INSTANCE_ID)),
                 Probe("ec2:StopInstances", config.instance_arn(account, instance_id or OTHER_INSTANCE_ID))]
    return RoleExpectation(tuple(must_allow), tuple(must_deny), frozenset({"ec2:StartInstances"}),
                           skipped=tuple(skipped))


def deploy_role_expectation(account, instance_id=None):
    current = _object_arn(account, config.RELEASES_CURRENT_KEY)
    must_allow = [Probe("s3:PutObject", _object_arn(account, f"{config.RELEASES_PREFIX}probe.tar.gz")),
                  Probe("s3:GetObject", current), Probe("s3:PutObject", current)]
    skipped = []
    if instance_id:
        target = config.instance_arn(account, instance_id)
        must_allow += [Probe("ec2:StartInstances", target), Probe("ec2:CreateTags", target)]
    else:
        skipped.append("instance not found")
    other = config.instance_arn(account, OTHER_INSTANCE_ID)
    must_deny = [
        Probe("s3:PutObject", _object_arn(account, f"{config.DATA_PREFIX}probe")),
        Probe("s3:GetObject", _object_arn(account, f"{config.DATA_PREFIX}probe")),
        Probe("s3:DeleteObject", current),
        *[Probe(a, _object_arn(account, key, other)) for other in lookalike_buckets(account)
          for a, key in (("s3:PutObject", f"{config.RELEASES_PREFIX}probe"),
                         ("s3:GetObject", config.RELEASES_CURRENT_KEY))],
        Probe("ec2:StartInstances", other),
        Probe("ec2:CreateTags", other),
        Probe("ec2:StopInstances", config.instance_arn(account, instance_id or OTHER_INSTANCE_ID)),
    ]
    return RoleExpectation(
        tuple(must_allow), tuple(must_deny),
        frozenset({"s3:PutObject", "s3:GetObject", "ec2:StartInstances", "ec2:CreateTags"}),
        conditions=(ConditionRule("ec2:CreateTags", "aws:TagKeys", "ForAllValues:StringEquals",
                                  frozenset({config.PI_MODE_TAG_KEY})),),
        skipped=tuple(skipped))


# --- Trust policies (pure) ---------------------------------------------------------------------
def _allow_statements(doc):
    return [s for s in statements(doc) if s.get("Effect") == "Allow"]


def evaluate_service_trust(label, doc, service, account=None):
    """Trust that only `service` may assume the role (sts:AssumeRole). With `account`, every
    statement must also carry `StringEquals aws:SourceAccount = account` (confused deputy)."""
    allow = _allow_statements(doc)
    problems = []
    if not allow:
        problems.append("no Allow statement")
    for s in allow:
        principal = s.get("Principal")
        if not isinstance(principal, dict) or set(principal) != {"Service"} \
                or set(_as_list(principal["Service"])) != {service}:
            problems.append(f"principal {json.dumps(principal, sort_keys=True)}")
        if {a.lower() for a in _as_list(s.get("Action"))} != {"sts:assumerole"}:
            problems.append(f"actions {_as_list(s.get('Action'))}")
        if account and condition_values(s, "aws:SourceAccount").get("StringEquals") != [account]:
            problems.append("missing StringEquals aws:SourceAccount")
    return [_expect(not problems, f"{label} trust", f"only {service}", "; ".join(problems))]


def evaluate_deploy_trust(doc, account):
    """Trust of the deploy role: only GitHub OIDC tokens for the production branch."""
    allow = _allow_statements(doc)
    provider = config.oidc_provider_arn(account)
    sub_key, aud_key = f"{config.GITHUB_OIDC_HOST}:sub", f"{config.GITHUB_OIDC_HOST}:aud"
    problems = [] if allow else ["no Allow statement"]
    for s in allow:
        principal = s.get("Principal")
        if not isinstance(principal, dict) or set(principal) != {"Federated"} \
                or _as_list(principal["Federated"]) != [provider]:
            problems.append("principal is not the GitHub OIDC provider")
        if {a.lower() for a in _as_list(s.get("Action"))} != {"sts:assumerolewithwebidentity"}:
            problems.append(f"actions {_as_list(s.get('Action'))}")
        sub, aud = condition_values(s, sub_key), condition_values(s, aud_key)
        if sub != {"StringEquals": [config.DEPLOY_OIDC_SUBJECT]}:
            problems.append(f"sub condition {sub or 'missing'} (expected StringEquals {config.DEPLOY_OIDC_SUBJECT})")
        if aud != {"StringEquals": [config.GITHUB_OIDC_AUDIENCE]}:
            problems.append(f"aud condition {aud or 'missing'}")
    return [_expect(not problems, f"role {config.DEPLOY_ROLE} trust",
                    f"only {config.DEPLOY_OIDC_SUBJECT}", "; ".join(problems))]


# --- Roles (pure part) -------------------------------------------------------------------------
@dataclass
class RoleState:
    name: str
    trust: dict
    tags: list
    attached: list  # policy ARNs
    documents: dict = field(default_factory=dict)  # inline name or managed ARN -> document


def evaluate_tags(label, tags):
    have = _aws.tags_to_dict(tags)
    good = have.get(config.PROJECT_TAG_KEY) == config.PROJECT_TAG_VALUE
    return _expect(good, f"{label} tag", f"{config.PROJECT_TAG_KEY}={config.PROJECT_TAG_VALUE}",
                   f"{config.PROJECT_TAG_KEY}={have.get(config.PROJECT_TAG_KEY, '<missing>')}")


def evaluate_role(state, exp, trust_findings, name=None):
    """Findings for one role. `name` labels a missing role (`state` None), so the output says
    which one is absent."""
    name = state.name if state else name
    label = f"role {name}" if name else "role"
    if state is None:
        return [_fail(label, "not found")]
    out = [_ok(label, "exists"), evaluate_tags(label, state.tags), *trust_findings]
    managed = {a for a in state.attached if a.startswith(AWS_MANAGED_PREFIX)}
    missing, extra = sorted(exp.aws_managed - managed), sorted(managed - exp.aws_managed)
    out.append(_expect(not missing and not extra, f"{label} AWS-managed policies",
                       ", ".join(sorted(exp.aws_managed)) or "none",
                       "; ".join(filter(None, [missing and f"missing {', '.join(missing)}",
                                               extra and f"unexpected {', '.join(extra)}"]))))
    stmts = [s for doc in state.documents.values() for s in statements(doc)]
    return out + evaluate_permissions(label, stmts, exp)


def evaluate_instance_profile(profile):
    label = f"instance profile {config.INSTANCE_PROFILE}"
    if profile is None:
        return [_fail(label, "not found")]
    roles = [r.get("RoleName") for r in profile.get("Roles", [])]
    return [_expect(roles == [config.INSTANCE_ROLE], label, f"holds {config.INSTANCE_ROLE}",
                    f"holds {roles or 'no role'}"),
            evaluate_tags(label, profile.get("Tags"))]


def evaluate_old_role(name, exists):
    return _expect(exists, f"old role {name}", "still exists (rollback target; F3 never deletes it)",
                   "not found: rollback to the old role is no longer possible", warn=True)


# --- Bucket (pure) -----------------------------------------------------------------------------
def _rule_prefix(rule):
    filt = rule.get("Filter") or {}
    if "Prefix" in filt:
        return filt["Prefix"] or ""
    if "And" in filt:
        return filt["And"].get("Prefix") or ""
    return rule.get("Prefix") or ""


def _covers(rule_prefix, prefix):
    """True if a rule on `rule_prefix` can act on objects under `prefix`."""
    return prefix.startswith(rule_prefix) or rule_prefix.startswith(prefix)


def evaluate_lifecycle(rules):
    label = "bucket lifecycle"
    if isinstance(rules, Missing):
        return [_fail(label, f"no lifecycle configuration ({rules.code})")]
    enabled = [r for r in rules or [] if r.get("Status") == "Enabled"]
    noncurrent = [r.get("NoncurrentVersionExpiration", {}).get("NoncurrentDays")
                  for r in enabled if _rule_prefix(r) == "" and "NoncurrentVersionExpiration" in r]
    logs = [r.get("Expiration", {}).get("Days") for r in enabled
            if _rule_prefix(r) == config.LOGS_PREFIX and "Expiration" in r]
    abort = [r.get("AbortIncompleteMultipartUpload", {}).get("DaysAfterInitiation")
             for r in enabled if _rule_prefix(r) == "" and "AbortIncompleteMultipartUpload" in r]
    kept = [p for p in config.BUCKET_PREFIXES if p != config.LOGS_PREFIX]
    expiring = sorted({p for r in enabled
                       if {"Days", "Date"} & set(r.get("Expiration") or {})
                       for p in kept if _covers(_rule_prefix(r), p)})
    return [
        _expect(set(noncurrent) == {config.NONCURRENT_EXPIRATION_DAYS}, f"{label} noncurrent versions",
                f"expire after {config.NONCURRENT_EXPIRATION_DAYS} days",
                f"bucket-wide NoncurrentDays {noncurrent or 'missing'}"),
        _expect(set(logs) == {config.LOGS_EXPIRATION_DAYS}, f"{label} {config.LOGS_PREFIX}",
                f"expire after {config.LOGS_EXPIRATION_DAYS} days", f"Expiration days {logs or 'missing'}"),
        _expect(set(abort) == {config.ABORT_MULTIPART_DAYS}, f"{label} incomplete multipart",
                f"aborted after {config.ABORT_MULTIPART_DAYS} days", f"DaysAfterInitiation {abort or 'missing'}"),
        _expect(not expiring, f"{label} kept prefixes", "nothing expires current objects outside logs/",
                f"a rule expires current objects under {', '.join(expiring)}"),
    ]


def is_everyone(principal):
    """True if a resource-policy Principal covers every AWS principal: `"*"` or `"*"` among the
    `AWS` values, in string or list form (`{"AWS": "*"}` and `{"AWS": ["*"]}` mean the same)."""
    if principal == "*":
        return True
    return isinstance(principal, dict) and "*" in _as_list(principal.get("AWS"))


def _is_public_allow(stmt):
    # An Allow with NotPrincipal grants everyone except the listed principals: public as well.
    return stmt.get("Effect") == "Allow" and ("NotPrincipal" in stmt or is_everyone(stmt.get("Principal")))


def _tls_only(stmts, bucket_arn):
    for s in stmts:
        if s.get("Effect") != "Deny" or not is_everyone(s.get("Principal")):
            continue
        if not ({"s3:*", "*"} & {a.lower() for a in _as_list(s.get("Action"))}):
            continue
        if not {bucket_arn, f"{bucket_arn}/*"} <= set(_as_list(s.get("Resource"))):
            continue
        if condition_values(s, "aws:SecureTransport").get("Bool") == ["false"]:
            return True
    return False


def evaluate_bucket_policy(policy, account):
    label = "bucket policy"
    if isinstance(policy, Missing):
        return [_fail(label, f"no bucket policy ({policy.code})")]
    stmts = statements(policy)
    public = [s.get("Sid") or f"#{i}" for i, s in enumerate(stmts) if _is_public_allow(s)]
    return [_expect(_tls_only(stmts, config.bucket_arn(account)), f"{label} TLS only",
                    "denies aws:SecureTransport=false", "no Deny on aws:SecureTransport=false for the bucket"),
            _expect(not public, f"{label} public access", "no public Allow",
                    f"public Allow in {', '.join(public)}")]


def evaluate_bucket(state, account):
    """`state`: get_bucket_* responses by short name (`location`, `encryption`, `public_access`,
    `versioning`, `ownership`, `policy`, `lifecycle`, `tags`); a `Missing` where AWS said absent."""
    name = config.bucket_name(account)
    label = f"bucket {name}"
    location = state.get("location")
    if isinstance(location, Missing):
        return [_fail(label, f"not found ({location.code})")]
    region = (location or {}).get("LocationConstraint") or "us-east-1"
    out = [_expect(region == config.REGION, label, f"exists in {config.REGION}", f"region {region}")]

    enc = state.get("encryption")
    enc = {} if isinstance(enc, Missing) else (enc or {})
    rules = enc.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
    algos = [r.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") for r in rules]
    out.append(_expect(algos == ["AES256"], f"{label} encryption", "SSE-S3 (AES256)", f"SSE {algos or 'missing'}"))
    out.append(_expect(bool(rules) and all(r.get("BucketKeyEnabled") for r in rules), f"{label} bucket key",
                       "enabled", "not enabled", warn=True))

    pab = state.get("public_access")
    flags = {} if isinstance(pab, Missing) else (pab or {}).get("PublicAccessBlockConfiguration", {})
    off = [k for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
           if not flags.get(k)]
    out.append(_expect(not off, f"{label} Block Public Access", "all four on", f"off: {', '.join(off)}"))

    versioning = state.get("versioning")
    status = None if isinstance(versioning, Missing) else (versioning or {}).get("Status")
    out.append(_expect(status == "Enabled", f"{label} versioning", "Enabled", f"{status or 'never enabled'}"))

    own = state.get("ownership")
    owners = [] if isinstance(own, Missing) else [
        r.get("ObjectOwnership") for r in (own or {}).get("OwnershipControls", {}).get("Rules", [])]
    out.append(_expect(owners == ["BucketOwnerEnforced"], f"{label} object ownership", "BucketOwnerEnforced",
                       f"{owners or 'missing'}"))

    policy = state.get("policy")
    out += evaluate_bucket_policy(policy if isinstance(policy, Missing) else (policy or {}).get("Policy"), account)
    lifecycle = state.get("lifecycle")
    out += evaluate_lifecycle(lifecycle if isinstance(lifecycle, Missing) else (lifecycle or {}).get("Rules"))
    tags = state.get("tags")
    out.append(evaluate_tags(label, [] if isinstance(tags, Missing) else (tags or {}).get("TagSet")))
    return out


# --- Instance (pure) ---------------------------------------------------------------------------
def evaluate_instance(instances, associations):
    """`instances`: the Instances of describe_instances filtered by Name tag (not terminated);
    `associations`: describe_iam_instance_profile_associations for that instance."""
    label = f"instance {config.INSTANCE_NAME}"
    if len(instances) != 1:
        return [_fail(label, f"{len(instances)} instances with this Name tag (expected exactly 1)")]
    inst = instances[0]
    md = inst.get("MetadataOptions") or {}
    out = [_ok(label, f"found ({inst.get('State', {}).get('Name', 'unknown')}, {inst.get('InstanceType')})"),
           _expect(md.get("HttpTokens") == "required", f"{label} IMDSv2", "required",
                   f"HttpTokens={md.get('HttpTokens')}"),
           _expect(md.get("HttpPutResponseHopLimit") == config.IMDS_HOP_LIMIT, f"{label} IMDS hop limit",
                   str(config.IMDS_HOP_LIMIT), str(md.get("HttpPutResponseHopLimit"))),
           _expect(md.get("HttpEndpoint", "enabled") == "enabled", f"{label} IMDS endpoint", "enabled",
                   "disabled (the instance role would get no credentials)"),
           evaluate_tags(label, inst.get("Tags"))]
    current = [a for a in associations if a.get("State") in ("associated", "associating")]
    names = [a.get("IamInstanceProfile", {}).get("Arn", "").rsplit("/", 1)[-1] for a in current]
    if names == [config.INSTANCE_PROFILE]:
        out.append(_expect(current[0]["State"] == "associated", f"{label} instance profile",
                           config.INSTANCE_PROFILE, f"{config.INSTANCE_PROFILE} still {current[0]['State']}",
                           warn=True))
    else:
        out.append(_fail(f"{label} instance profile",
                         f"associated: {', '.join(names) or 'none'} (expected {config.INSTANCE_PROFILE}; "
                         "apply python -m infra.ec2_instance)"))
    return out


# --- Bedrock (pure) ----------------------------------------------------------------------------
def evaluate_bedrock_profile(name, summary, tags, model_id, *, required=True):
    label = f"Bedrock profile {name}"
    if summary is None:
        if required:
            return [_fail(label, "not found (apply python -m infra.bedrock_profiles)")]
        return [_warn(label, f"not found: the instance must use the plain model ID {model_id} "
                             "(documented fallback) or apply python -m infra.bedrock_profiles")]
    models = [m.get("modelArn", "") for m in summary.get("models", [])]
    wrong = [m for m in models if not m.endswith(f"foundation-model/{model_id}")]
    return [
        _expect(summary.get("status") == "ACTIVE", label, f"ACTIVE, {summary.get('inferenceProfileArn')}",
                f"status {summary.get('status')}"),
        _expect(bool(models) and not wrong, f"{label} model", f"{model_id} in {len(models)} region(s)",
                f"routes to {wrong or 'no model'}"),
        evaluate_tags(label, tags),
    ]


def evaluate_haiku_system_profile(summary):
    label = f"Bedrock system profile {config.HAIKU_SYSTEM_PROFILE_ID}"
    if summary is None:
        return [_fail(label, "not found in this region")]
    regions = sorted({m.get("modelArn", "").split(":")[3] for m in summary.get("models", [])})
    return [_expect(bool(regions), label, f"routes to {', '.join(regions)}", "no models listed")]


# --- OIDC (pure) -------------------------------------------------------------------------------
def evaluate_oidc(provider):
    label = f"OIDC provider {config.GITHUB_OIDC_HOST}"
    if provider is None:
        return [_fail(label, "not found (apply python -m infra.deploy_role)")]
    url = str(provider.get("Url", "")).removeprefix("https://")
    clients = provider.get("ClientIDList", [])
    return [_expect(url == config.GITHUB_OIDC_HOST, label, "exists", f"URL {url}"),
            _expect(config.GITHUB_OIDC_AUDIENCE in clients, f"{label} audience", config.GITHUB_OIDC_AUDIENCE,
                    f"client IDs {clients}"),
            evaluate_tags(label, provider.get("Tags"))]


# --- Schedule (pure) ---------------------------------------------------------------------------
def evaluate_schedule_group(group, tags):
    label = f"schedule group {config.SCHEDULE_GROUP}"
    if group is None:
        return [_fail(label, "not found (apply python -m infra.schedule)")]
    return [_expect(group.get("State") == "ACTIVE", label, "ACTIVE", f"state {group.get('State')}"),
            evaluate_tags(label, tags)]


def evaluate_schedule(schedule, account, instance_id=None):
    label = f"schedule {config.SCHEDULE_NAME}"
    if schedule is None:
        return [_fail(label, "not found (apply python -m infra.schedule)")]
    target = schedule.get("Target") or {}
    expr, tz, state = (schedule.get("ScheduleExpression"), schedule.get("ScheduleExpressionTimezone"),
                       schedule.get("State"))
    out = [
        _expect(expr == config.SCHEDULE_CRON and tz == config.SCHEDULE_TIMEZONE, label,
                f"{expr} {tz}, {state}",
                f"AWS has {expr} {tz}; config has {config.SCHEDULE_CRON} {config.SCHEDULE_TIMEZONE}"),
        _expect((schedule.get("FlexibleTimeWindow") or {}).get("Mode") == "OFF", f"{label} flexible window",
                "OFF", f"{schedule.get('FlexibleTimeWindow')}"),
        _expect(target.get("Arn") == config.SCHEDULE_TARGET_ARN, f"{label} target", "ec2:StartInstances",
                f"target {target.get('Arn')}"),
        _expect(target.get("RoleArn") == config.role_arn(account, config.SCHEDULER_ROLE), f"{label} role",
                config.SCHEDULER_ROLE, f"role {target.get('RoleArn')}"),
        _expect((target.get("RetryPolicy") or {}).get("MaximumRetryAttempts") == 0, f"{label} retries",
                "0 (a missed start is reported, not retried)", f"{target.get('RetryPolicy')}"),
    ]
    if instance_id:
        try:
            ids = json.loads(target.get("Input") or "{}").get("InstanceIds")
        except (ValueError, AttributeError):
            ids = None
        out.append(_expect(ids == [instance_id], f"{label} input", f"starts {config.INSTANCE_NAME}",
                           "InstanceIds is not exactly the instance"))
    else:
        out.append(_warn(f"{label} input", "instance not found; target input not checked"))
    out.append(_expect(state == "ENABLED", f"{label} state", "ENABLED",
                       "DISABLED: nothing runs monthly yet (python -m infra.schedule --enable after the F2 deploy)",
                       warn=True))
    return out


def evaluate_retired(name, schedule):
    label = f"retired schedule {config.RETIRED_SCHEDULE_GROUP}/{name}"
    if schedule is None:
        return _ok(label, "deleted")
    state = schedule.get("State")
    return _expect(state == "DISABLED", label, "DISABLED",
                   f"{state}: still starts the instance (apply python -m infra.schedule)")


def evaluate_start_sources(schedule, retired):
    """Both the monthly schedule and an old weekly one enabled = the instance starts twice."""
    new_on = bool(schedule) and schedule.get("State") == "ENABLED"
    old_on = [n for n, s in retired.items() if s and s.get("State") == "ENABLED"]
    if new_on and old_on:
        return [_fail("instance starts", f"monthly schedule and {', '.join(old_on)} are all ENABLED")]
    return []


# --- Alerts (pure) -----------------------------------------------------------------------------
def evaluate_topic(attributes, tags, subscriptions):
    label = f"SNS topic {config.ALERTS_TOPIC}"
    if attributes is None:
        return [_fail(label, "not found (apply python -m infra.alerts)")]
    key = attributes.get("KmsMasterKeyId")
    emails = [s for s in subscriptions if s.get("Protocol") == "email"]
    confirmed = [s for s in emails if str(s.get("SubscriptionArn", "")).startswith("arn:")]
    pending = [s for s in emails if s.get("SubscriptionArn") == "PendingConfirmation"]
    masked = ", ".join(_aws.mask_email(s.get("Endpoint", "")) for s in confirmed + pending)
    if confirmed:
        sub = _ok(f"{label} email subscriptions", f"{len(confirmed)} confirmed, {len(pending)} pending ({masked})")
    elif pending:
        sub = _warn(f"{label} email subscriptions",
                    f"{len(pending)} pending confirmation ({masked}): open the AWS email and confirm")
    else:
        sub = _fail(f"{label} email subscriptions",
                    "none: failure alerts reach nobody (python -m infra.alerts --email <address>)")
    return [_ok(label, "exists"),
            _expect(key == config.SNS_KMS_KEY, f"{label} encryption", config.SNS_KMS_KEY, f"KMS key {key or 'none'}"),
            evaluate_tags(label, tags), sub]


# --- Reads (only Describe*/Get*/List*) ---------------------------------------------------------
def _read(call, missing_codes, **kwargs):
    """Call a read API; return `Missing(code)` if AWS answers with one of `missing_codes`."""
    from botocore.exceptions import ClientError

    try:
        return call(**kwargs)
    except ClientError as exc:
        code = _aws.error_code(exc)
        if code in missing_codes:
            return Missing(code)
        raise


def _iam_pages(call, key, **kwargs):
    items, marker = [], None
    while True:
        resp = call(**kwargs, **({"Marker": marker} if marker else {}))
        items += resp.get(key, [])
        if not resp.get("IsTruncated"):
            return items
        marker = resp["Marker"]


def _token_pages(call, key, token_in, token_out, **kwargs):
    items, token = [], None
    while True:
        resp = call(**kwargs, **({token_in: token} if token else {}))
        items += resp.get(key, [])
        token = resp.get(token_out)
        if not token:
            return items


def read_role(iam, name):
    """RoleState of a role (trust, tags, attached ARNs, inline and customer-managed documents),
    or None if it does not exist."""
    resp = _read(iam.get_role, ("NoSuchEntity",), RoleName=name)
    if isinstance(resp, Missing):
        return None
    role = resp["Role"]
    docs = {}
    for policy in _iam_pages(iam.list_role_policies, "PolicyNames", RoleName=name):
        docs[policy] = as_document(iam.get_role_policy(RoleName=name, PolicyName=policy)["PolicyDocument"])
    attached = [p["PolicyArn"] for p in _iam_pages(iam.list_attached_role_policies, "AttachedPolicies",
                                                    RoleName=name)]
    for policy_arn in attached:
        if policy_arn.startswith(AWS_MANAGED_PREFIX):
            continue  # AWS-managed policies are checked by ARN only
        version = iam.get_policy(PolicyArn=policy_arn)["Policy"]["DefaultVersionId"]
        docs[policy_arn] = as_document(
            iam.get_policy_version(PolicyArn=policy_arn, VersionId=version)["PolicyVersion"]["Document"])
    return RoleState(name, as_document(role.get("AssumeRolePolicyDocument")), role.get("Tags", []), attached, docs)


class Ctx:
    """Clients and the facts several sections need (instance, Bedrock profiles), read once."""

    def __init__(self, session, account):
        self.session, self.account = session, account
        self._clients, self._cache = {}, {}

    def client(self, service):
        if service not in self._clients:
            self._clients[service] = _aws.client(self.session, service)
        return self._clients[service]

    def _once(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def instances(self):
        def read():
            resp = _token_pages(self.client("ec2").describe_instances, "Reservations", "NextToken", "NextToken",
                                Filters=[{"Name": "tag:Name", "Values": [config.INSTANCE_NAME]},
                                         {"Name": "instance-state-name", "Values": list(ACTIVE_INSTANCE_STATES)}])
            return [i for r in resp for i in r.get("Instances", [])]
        return self._once("instances", read)

    def instance_id(self):
        found = self.instances()
        return found[0]["InstanceId"] if len(found) == 1 else None

    def known_instance_id(self):
        """The instance ID if it was already read (for redaction); never calls AWS."""
        found = self._cache.get("instances") or []
        return found[0].get("InstanceId") if len(found) == 1 else None

    def app_profiles(self):
        """Application inference profiles by name (only PI's two)."""
        def read():
            items = _token_pages(self.client("bedrock").list_inference_profiles, "inferenceProfileSummaries",
                                 "nextToken", "nextToken", typeEquals="APPLICATION")
            wanted = {config.DEEPSEEK_PROFILE, config.HAIKU_PROFILE}
            return {p["inferenceProfileName"]: p for p in items if p.get("inferenceProfileName") in wanted}
        return self._once("app_profiles", read)

    def haiku_system_profile(self):
        def read():
            resp = _read(self.client("bedrock").get_inference_profile, ("ResourceNotFoundException",),
                         inferenceProfileIdentifier=config.HAIKU_SYSTEM_PROFILE_ID)
            return None if isinstance(resp, Missing) else resp
        return self._once("haiku_system", read)


def section_bucket(ctx):
    s3, bucket = ctx.client("s3"), config.bucket_name(ctx.account)
    # Bucket names are global: without the owner check a bucket with PI's name in another account
    # would be read and judged as if it were PI's.
    kwargs = {"Bucket": bucket, "ExpectedBucketOwner": ctx.account}
    state = {"location": _read(s3.get_bucket_location, ("NoSuchBucket",), **kwargs)}
    if not isinstance(state["location"], Missing):
        reads = {
            "encryption": (s3.get_bucket_encryption, ("ServerSideEncryptionConfigurationNotFoundError",)),
            "public_access": (s3.get_public_access_block, ("NoSuchPublicAccessBlockConfiguration",)),
            "versioning": (s3.get_bucket_versioning, ()),
            "ownership": (s3.get_bucket_ownership_controls, ("OwnershipControlsNotFoundError",)),
            "policy": (s3.get_bucket_policy, ("NoSuchBucketPolicy",)),
            "lifecycle": (s3.get_bucket_lifecycle_configuration, ("NoSuchLifecycleConfiguration",)),
            "tags": (s3.get_bucket_tagging, ("NoSuchTagSet",)),
        }
        for key, (call, missing) in reads.items():
            state[key] = _read(call, missing, **kwargs)
    return evaluate_bucket(state, ctx.account)


def section_instance(ctx):
    instances = ctx.instances()
    associations = []
    if len(instances) == 1:
        associations = _token_pages(
            ctx.client("ec2").describe_iam_instance_profile_associations, "IamInstanceProfileAssociations",
            "NextToken", "NextToken", Filters=[{"Name": "instance-id", "Values": [instances[0]["InstanceId"]]}])
    return evaluate_instance(instances, associations)


def section_bedrock(ctx):
    bedrock, profiles = ctx.client("bedrock"), ctx.app_profiles()
    out = evaluate_haiku_system_profile(ctx.haiku_system_profile())
    for name, model_id, required in ((config.DEEPSEEK_PROFILE, config.DEEPSEEK_MODEL_ID, False),
                                     (config.HAIKU_PROFILE, config.HAIKU_MODEL_ID, True)):
        summary = profiles.get(name)
        tags = bedrock.list_tags_for_resource(resourceARN=summary["inferenceProfileArn"]).get("tags", []) \
            if summary else []
        out += evaluate_bedrock_profile(name, summary, tags, model_id, required=required)
    return out


def section_iam(ctx):
    iam, account, instance_id = ctx.client("iam"), ctx.account, ctx.instance_id()
    profile_arns = tuple(p["inferenceProfileArn"] for p in ctx.app_profiles().values())
    # The Haiku models in every EU region the profiles route to, as Bedrock reports them.
    haiku_sources = [ctx.haiku_system_profile() or {}, ctx.app_profiles().get(config.HAIKU_PROFILE) or {}]
    haiku_models = tuple(sorted({m["modelArn"] for src in haiku_sources for m in src.get("models", [])
                                 if m.get("modelArn")}))

    out = []
    role = read_role(iam, config.INSTANCE_ROLE)
    out += evaluate_role(role, instance_role_expectation(account, profile_arns, haiku_models),
                         evaluate_service_trust(f"role {config.INSTANCE_ROLE}", role.trust, "ec2.amazonaws.com")
                         if role else [], config.INSTANCE_ROLE)
    profile = _read(iam.get_instance_profile, ("NoSuchEntity",), InstanceProfileName=config.INSTANCE_PROFILE)
    out += evaluate_instance_profile(None if isinstance(profile, Missing) else profile["InstanceProfile"])

    role = read_role(iam, config.SCHEDULER_ROLE)
    out += evaluate_role(role, scheduler_role_expectation(account, instance_id),
                         evaluate_service_trust(f"role {config.SCHEDULER_ROLE}", role.trust,
                                                "scheduler.amazonaws.com", account) if role else [],
                         config.SCHEDULER_ROLE)
    role = read_role(iam, config.DEPLOY_ROLE)
    out += evaluate_role(role, deploy_role_expectation(account, instance_id),
                         evaluate_deploy_trust(role.trust, account) if role else [], config.DEPLOY_ROLE)
    for name in (config.OLD_INSTANCE_ROLE, config.OLD_SCHEDULER_ROLE):
        out.append(evaluate_old_role(name, not isinstance(
            _read(iam.get_role, ("NoSuchEntity",), RoleName=name), Missing)))
    return out


def section_oidc(ctx):
    resp = _read(ctx.client("iam").get_open_id_connect_provider, ("NoSuchEntity",),
                 OpenIDConnectProviderArn=config.oidc_provider_arn(ctx.account))
    return evaluate_oidc(None if isinstance(resp, Missing) else resp)


def section_schedule(ctx):
    scheduler = ctx.client("scheduler")
    group = _read(scheduler.get_schedule_group, ("ResourceNotFoundException",), Name=config.SCHEDULE_GROUP)
    group = None if isinstance(group, Missing) else group
    tags = scheduler.list_tags_for_resource(ResourceArn=group["Arn"]).get("Tags", []) if group else []
    out = evaluate_schedule_group(group, tags)
    schedule = None
    if group:
        schedule = _read(scheduler.get_schedule, ("ResourceNotFoundException",),
                         Name=config.SCHEDULE_NAME, GroupName=config.SCHEDULE_GROUP)
        schedule = None if isinstance(schedule, Missing) else schedule
    out += evaluate_schedule(schedule, ctx.account, ctx.instance_id())
    retired = {}
    for name in config.RETIRED_SCHEDULES:
        resp = _read(scheduler.get_schedule, ("ResourceNotFoundException",),
                     Name=name, GroupName=config.RETIRED_SCHEDULE_GROUP)
        retired[name] = None if isinstance(resp, Missing) else resp
        out.append(evaluate_retired(name, retired[name]))
    return out + evaluate_start_sources(schedule, retired)


def section_alerts(ctx):
    sns, arn = ctx.client("sns"), config.topic_arn(ctx.account)
    attrs = _read(sns.get_topic_attributes, ("NotFound",), TopicArn=arn)
    if isinstance(attrs, Missing):
        return evaluate_topic(None, [], [])
    tags = sns.list_tags_for_resource(ResourceArn=arn).get("Tags", [])
    subs = _token_pages(sns.list_subscriptions_by_topic, "Subscriptions", "NextToken", "NextToken", TopicArn=arn)
    return evaluate_topic(attrs.get("Attributes", {}), tags, subs)


SECTION_FUNCS = {"bucket": section_bucket, "instance": section_instance, "bedrock": section_bedrock,
                 "iam": section_iam, "oidc": section_oidc, "schedule": section_schedule,
                 "alerts": section_alerts}


def run_section(name, ctx):
    """Run one section. An AWS or unexpected error becomes a FAIL line (so it sets exit 1) and the
    other sections still run."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        return SECTION_FUNCS[name](ctx)
    except ClientError as exc:
        return [_fail(name, f"AWS error {_aws.error_code(exc)}")]
    except BotoCoreError as exc:
        return [_fail(name, f"AWS client error {type(exc).__name__}")]
    except Exception as exc:  # noqa: BLE001 - reported as FAIL and turned into exit 1
        return [_fail(name, f"unexpected {type(exc).__name__}: {exc}")]


def main(argv=None, session=None):
    parser = _aws.base_parser(__doc__)
    parser.add_argument("--only", nargs="+", choices=SECTIONS, metavar="SECTION",
                        help=f"check only these sections ({', '.join(SECTIONS)})")
    args = parser.parse_args(argv)
    sess = _aws.get_session(session)
    account = _aws.account_id(sess)
    ctx = Ctx(sess, account)
    findings = []
    for name in args.only or SECTIONS:
        for finding in run_section(name, ctx):
            findings.append(finding)
            print(redact(finding.line(), account, ctx.known_instance_id()))
    print(summary(findings))
    return 1 if has_failures(findings) else 0


if __name__ == "__main__":
    sys.exit(main())
