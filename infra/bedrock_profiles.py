"""Bedrock application inference profiles for the relevance AI. Safe to run more than once.

What it leaves configured (eu-north-1, tag `project=price-intelligence` on both):
- `price-intelligence-deepseek-v3-2`: copy of the foundation model `deepseek.v3.2` (primary).
- `price-intelligence-haiku-4-5`: copy of the system profile
  `eu.anthropic.claude-haiku-4-5-20251001-v1:0` (fallback; same EU regions as that profile).
They call the same models as the plain IDs; the only difference is that every invocation is
attributed to the project tag in Cost Explorer and budgets.

Idempotent: a profile that already exists with the same name is kept (only the project tag is
added if missing). A profile cannot change its model, so the name carries the model version; if
an existing profile points to another model the script reports FAIL and exits 1 (rename it in
`infra/config.py` instead of editing it in AWS).

At the end it prints the values for the instance environment file
(`PI_RELEVANCE_PRIMARY_MODEL`, `PI_RELEVANCE_FALLBACK_MODEL`; the account is shown as
`<account>`; `resolve_model_ids(session)` returns the real values to other scripts). If AWS does
not allow an application profile copied from `deepseek.v3.2` (ValidationException), the script
says so and the primary falls back to the plain model ID `deepseek.v3.2` (documented fallback:
the cost is then split by model ID in Cost Explorer). Any other error exits 1.

Cost: the profiles cost nothing by themselves; invocations are billed at the model's price.

Usage (from the repo root, profile `wayakit-vdc`):
    python -m infra.bedrock_profiles --dry-run   # read calls only, prints the plan
    python -m infra.bedrock_profiles
"""
import re

from botocore.exceptions import BotoCoreError, ClientError

from infra import _aws, config

# Error returned by CreateInferenceProfile when the source cannot be copied into an application
# profile. Only this code triggers the documented fallback; anything else is a real failure.
UNSUPPORTED_SOURCE_CODES = frozenset({"ValidationException"})

_DESCRIPTION_RE = re.compile(r"([0-9a-zA-Z:.][ _-]?)+")  # CreateInferenceProfile `description`


def profile_description(source_id):
    """Description sent to AWS. The API only accepts letters, digits, ':' and '.', with single
    ' ', '_' or '-' separators (no '(' or ',')."""
    text = f"Price Intelligence tagged copy of {source_id}"
    if len(text) > 200 or not _DESCRIPTION_RE.fullmatch(text):
        raise ValueError("Inference profile description not accepted by the Bedrock API.")
    return text


def desired_profiles(account):
    """The two application profiles, in order (primary first). Pure: each item has
    `name`, `model_id` (foundation model it must point to), `source_id`, `source_arn`
    (`modelSource.copyFrom`), `env_var` (instance variable that receives its ARN),
    `default_id` (value the instance uses when the profile does not exist, the same default as
    scraper/services/ai_service.py) and `fallback_allowed` (True only for DeepSeek: if AWS
    rejects the copy, use `default_id` instead of failing)."""
    sources = config.bedrock_profile_sources(account)
    return [
        {
            "name": config.DEEPSEEK_PROFILE,
            "model_id": config.DEEPSEEK_MODEL_ID,
            "source_id": config.DEEPSEEK_MODEL_ID,
            "source_arn": sources[config.DEEPSEEK_PROFILE],
            "env_var": config.ENV_PRIMARY_MODEL,
            "default_id": config.DEEPSEEK_MODEL_ID,
            "fallback_allowed": True,
        },
        {
            "name": config.HAIKU_PROFILE,
            "model_id": config.HAIKU_MODEL_ID,
            "source_id": config.HAIKU_SYSTEM_PROFILE_ID,
            "source_arn": sources[config.HAIKU_PROFILE],
            "env_var": config.ENV_FALLBACK_MODEL,
            "default_id": config.HAIKU_SYSTEM_PROFILE_ID,
            "fallback_allowed": False,
        },
    ]


def model_arns(models):
    """`models` of a profile summary (`[{"modelArn": ...}]`) -> list of model ARNs."""
    return [m["modelArn"] for m in models or [] if m.get("modelArn")]


def source_matches(model_id, models):
    """True if the profile routes only to `model_id` (in any region). An empty list is False:
    a profile without models is not the one we want."""
    arns = model_arns(models)
    return bool(arns) and all(a.endswith(f":foundation-model/{model_id}") for a in arns)


def regions_of(models):
    """Sorted regions a profile routes to (4th field of each model ARN). Printed so the instance
    role can be checked against the real EU regions of the `eu.` Haiku profile."""
    return sorted({a.split(":")[3] for a in model_arns(models) if a.count(":") >= 5})


def env_assignments(existing):
    """`{profile name: ARN}` of existing application profiles -> `{env var: model ID or ARN}`
    for the instance. A missing profile falls back to the default the code already uses
    (scraper/services/ai_service.py), so the result is always a complete, usable pair."""
    return {
        config.ENV_PRIMARY_MODEL: existing.get(config.DEEPSEEK_PROFILE, config.DEEPSEEK_MODEL_ID),
        config.ENV_FALLBACK_MODEL: existing.get(config.HAIKU_PROFILE, config.HAIKU_SYSTEM_PROFILE_ID),
    }


def existing_profiles(bedrock):
    """`{name: summary}` of the application inference profiles in the region (read only). Each
    summary has `inferenceProfileArn` and `models` (`[{"modelArn": ...}]`)."""
    out = {}
    for page in bedrock.get_paginator("list_inference_profiles").paginate(typeEquals="APPLICATION"):
        for p in page.get("inferenceProfileSummaries", []):
            out[p["inferenceProfileName"]] = p
    return out


def arns_by_name(summaries):
    """`{name: summary}` -> `{name: ARN}` (input of `env_assignments`)."""
    return {name: p["inferenceProfileArn"] for name, p in summaries.items()}


def resolve_model_ids(session=None):
    """Values for `PI_RELEVANCE_PRIMARY_MODEL` / `PI_RELEVANCE_FALLBACK_MODEL` from what exists
    in AWS (read only; real ARNs, never print them unredacted)."""
    sess = _aws.get_session(session)
    return env_assignments(arns_by_name(existing_profiles(_aws.client(sess, "bedrock"))))


def _ensure_profile(bedrock, spec, existing, *, dry_run, account):
    """Ensure one profile. Returns (status, value) with status "ok", "fallback" or "fail" and
    value the ARN or model ID for the instance (None in dry-run when it would be created)."""
    name = spec["name"]
    say = lambda msg, dry=False: _aws.report(msg, dry_run=dry, account=account)  # noqa: E731

    if name in existing:
        arn = existing[name]["inferenceProfileArn"]
        models = existing[name].get("models", [])
        if not source_matches(spec["model_id"], models):
            say(f"[FAIL] {name}: exists but does not point to {spec['model_id']} "
                f"(models: {', '.join(model_arns(models)) or 'none'}). A profile cannot change "
                "model: give the new one another name in infra/config.py.")
            return "fail", None
        say(f"[OK] {name}: exists ({arn}); regions: {', '.join(regions_of(models))}")
        current = bedrock.list_tags_for_resource(resourceARN=arn).get("tags", [])
        missing = _aws.tags_to_add(current)
        if missing:
            if dry_run:
                say(f"tag {name} with {missing}", dry=True)
            else:
                bedrock.tag_resource(resourceARN=arn, tags=_aws.to_tag_list(missing, lower=True))
                say(f"     {name}: tagged {missing}")
        return "ok", arn

    if dry_run:
        say(f"create {name} copied from {spec['source_arn']}", dry=True)
        return "ok", None
    try:
        arn = bedrock.create_inference_profile(
            inferenceProfileName=name,
            description=profile_description(spec["source_id"]),
            modelSource={"copyFrom": spec["source_arn"]},
            tags=_aws.project_tags(lower=True),
        )["inferenceProfileArn"]
    except ClientError as exc:
        code = _aws.error_code(exc)
        if spec["fallback_allowed"] and code in UNSUPPORTED_SOURCE_CODES:
            say(f"[FALLBACK] {name}: AWS does not allow an application inference profile copied "
                f"from {spec['source_id']} ({code}). The instance uses the plain model ID "
                f"{spec['default_id']} for {spec['env_var']}; its cost is split by model ID in "
                "Cost Explorer instead of by tag.")
            return "fallback", spec["default_id"]
        raise
    say(f"[OK] {name}: created ({arn})")
    return "ok", arn


def _placeholder(spec):
    """Dry-run value for a profile that would be created (its ARN is generated by AWS)."""
    text = f"<ARN of {spec['name']} once created"
    if spec["fallback_allowed"]:
        text += f", or {spec['default_id']} if AWS rejects the copy"
    return text + ">"


def main(argv=None, session=None):
    """Ensure both profiles and print the instance values. Returns the exit code (0 or 1)."""
    args = _aws.base_parser(__doc__).parse_args(argv)
    account = None
    try:
        sess = _aws.get_session(session)
        account = _aws.account_id(sess)
        bedrock = _aws.client(sess, "bedrock")
        existing = existing_profiles(bedrock)
        values, failed = {}, False
        for spec in desired_profiles(account):
            status, value = _ensure_profile(bedrock, spec, existing, dry_run=args.dry_run,
                                            account=account)
            failed |= status == "fail"
            if status == "fail":
                value = f"<unusable: fix {spec['name']} first>"
            values[spec["env_var"]] = value or _placeholder(spec)
    except ClientError as exc:
        _aws.report(f"ERROR: {exc.operation_name} failed ({_aws.error_code(exc)}); nothing else "
                    "was changed after it.", account=account)
        return 1
    except BotoCoreError as exc:
        _aws.report(f"ERROR: AWS client error ({type(exc).__name__}).", account=account)
        return 1

    _aws.report(f"Instance environment ({config.INSTANCE_ENV_FILE}):", account=account)
    for var, value in values.items():
        _aws.report(f"  {var}={value}", account=account)
    if args.dry_run:
        _aws.report("--dry-run: nothing was changed.")
    return 1 if failed else 0


if __name__ == "__main__":
    _aws.cli_main(main)
