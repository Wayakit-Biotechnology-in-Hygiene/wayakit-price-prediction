"""SNS topic for pipeline failure alerts, plus an optional email subscription. Safe to run more
than once.

What it leaves configured (eu-north-1):
- SNS topic `price-intelligence-alerts`, tagged `project=price-intelligence`, with server-side
  encryption under the AWS-managed key `alias/aws/sns`. The EXIT trap of `run_pipeline.sh`
  publishes to it when a run fails (`PI_ALERTS_TOPIC_ARN` in the instance environment file).
- With `--email <address>`: an email subscription to that topic. The address is passed at run
  time and never stored in the repo or printed in full (it is shown masked, `m***@domain`).
  AWS sends a confirmation email; nothing is delivered until the link in it is followed. An
  address that is already subscribed (confirmed or pending) is not subscribed again.

Idempotent: an existing topic is kept; only a different encryption key or a missing project tag
is corrected. Without `--email` no subscription is touched. Nothing is ever deleted. At the end it
prints the subscription counts and warns if no subscription is confirmed (alerts would reach
nobody). Any AWS error exits 1.

Cost: the topic is free; the first 1,000 email notifications a month are free (then USD 2 per
100,000). The AWS-managed key has no monthly fee; each encrypted publish is one KMS request
(20,000 a month free). In practice: USD 0.

Usage (from the repo root, profile `wayakit-vdc`):
    python -m infra.alerts --dry-run [--email <address>]   # read calls only, prints the plan
    python -m infra.alerts --email <address>
"""
import re
import sys

from botocore.exceptions import BotoCoreError, ClientError

from infra import _aws, config

# GetTopicAttributes on a missing topic (query protocol code; JSON protocol name kept for safety).
_NOT_FOUND_CODES = frozenset({"NotFound", "NotFoundException"})
# Deliberately loose: one "@", no spaces, a dot in the domain. SNS validates the rest.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PENDING = "PendingConfirmation"


def desired_attributes():
    """Topic attributes the script enforces (SSE with the AWS-managed SNS key)."""
    return {"KmsMasterKeyId": config.SNS_KMS_KEY}


def attribute_changes(current):
    """`GetTopicAttributes["Attributes"]` -> `{name: desired value}` of the enforced attributes
    that are missing or different. Empty dict = nothing to change."""
    current = current or {}
    return {k: v for k, v in desired_attributes().items() if current.get(k) != v}


def normalize_email(address):
    """Trimmed address, or ValueError (whose message never contains the address)."""
    address = (address or "").strip()
    if not _EMAIL_RE.fullmatch(address):
        raise ValueError("--email is not a valid email address.")
    return address


def redact_cli_message(message):
    """argparse error text with every token that contains "@" replaced by `<redacted>`.

    argparse echoes the offending argv ("unrecognized arguments: --emial <address>",
    "ambiguous option: --e=<address> ..."), so a typo on the command line would print the
    address in full. Any "@" token is dropped, not masked: it may not even be a valid address."""
    return re.sub(r"\S*@\S*", "<redacted>", str(message))


def _no_echo_error(parser, message):
    """Replacement for `ArgumentParser.error`: same usage line and exit code 2, redacted text."""
    parser.print_usage(sys.stderr)
    parser.exit(2, f"{parser.prog}: error: {redact_cli_message(message)}\n")


def find_email_subscription(subscriptions, address):
    """The email subscription of `address` among `ListSubscriptionsByTopic["Subscriptions"]`,
    compared case-insensitively, or None. Other protocols never match."""
    wanted = address.strip().lower()
    for sub in subscriptions or []:
        if sub.get("Protocol") == "email" and str(sub.get("Endpoint", "")).strip().lower() == wanted:
            return sub
    return None


def subscription_state(sub):
    """"pending" (the address has not confirmed yet) or "confirmed"."""
    return "pending" if sub.get("SubscriptionArn") == PENDING else "confirmed"


def subscription_counts(attributes):
    """(confirmed, pending) from `GetTopicAttributes["Attributes"]` (strings in the API)."""
    attributes = attributes or {}
    return (int(attributes.get("SubscriptionsConfirmed", 0) or 0),
            int(attributes.get("SubscriptionsPending", 0) or 0))


def get_topic_attributes(sns, arn):
    """Attributes of the topic, or None if it does not exist (read only)."""
    try:
        return sns.get_topic_attributes(TopicArn=arn).get("Attributes", {})
    except ClientError as exc:
        if _aws.error_code(exc) in _NOT_FOUND_CODES:
            return None
        raise


def list_subscriptions(sns, arn):
    """All subscriptions of the topic, every page (read only)."""
    subs = []
    for page in sns.get_paginator("list_subscriptions_by_topic").paginate(TopicArn=arn):
        subs.extend(page.get("Subscriptions", []))
    return subs


def _ensure_topic(sns, arn, attributes, *, dry_run, say):
    """Create the topic or correct its encryption and tag. Returns the topic ARN."""
    if attributes is None:
        if dry_run:
            say(f"create SNS topic {config.ALERTS_TOPIC} (SSE {config.SNS_KMS_KEY}, "
                f"tag {config.PROJECT_TAG_KEY}={config.PROJECT_TAG_VALUE})", True)
            return arn
        arn = sns.create_topic(Name=config.ALERTS_TOPIC, Attributes=desired_attributes(),
                               Tags=_aws.project_tags())["TopicArn"]
        say(f"[OK] topic {config.ALERTS_TOPIC}: created ({arn})")
        return arn

    say(f"[OK] topic {config.ALERTS_TOPIC}: exists ({arn})")
    for name, value in attribute_changes(attributes).items():
        if dry_run:
            say(f"set {name}={value} on {config.ALERTS_TOPIC} (now: "
                f"{attributes.get(name) or 'not set'})", True)
        else:
            sns.set_topic_attributes(TopicArn=arn, AttributeName=name, AttributeValue=value)
            say(f"     {config.ALERTS_TOPIC}: {name} set to {value}")
    missing = _aws.tags_to_add(sns.list_tags_for_resource(ResourceArn=arn).get("Tags", []))
    if missing:
        if dry_run:
            say(f"tag {config.ALERTS_TOPIC} with {missing}", True)
        else:
            sns.tag_resource(ResourceArn=arn, Tags=_aws.to_tag_list(missing))
            say(f"     {config.ALERTS_TOPIC}: tagged {missing}")
    return arn


def _ensure_subscription(sns, arn, address, topic_exists, *, dry_run, say):
    """Subscribe `address` unless it already is. Returns True if a subscription was requested
    (or would be, in dry-run)."""
    masked = _aws.mask_email(address)
    if topic_exists:
        sub = find_email_subscription(list_subscriptions(sns, arn), address)
        if sub is not None:
            say(f"[OK] {masked}: already subscribed ({subscription_state(sub)})")
            return False
    if dry_run:
        say(f"subscribe {masked} by email (AWS sends a confirmation email)", True)
        return True
    sns.subscribe(TopicArn=arn, Protocol="email", Endpoint=address)
    say(f"[OK] {masked}: subscription requested. Follow the link in the confirmation email; "
        "nothing is delivered until then.")
    return True


def main(argv=None, session=None):
    """Ensure the topic (and the subscription with --email). Returns the exit code (0 or 1)."""
    parser = _aws.base_parser(__doc__)
    # Shared base parser (in `_aws`), so the safe error is bound to this instance: every argparse
    # error path (unrecognized, ambiguous, missing value) goes through `parser.error`.
    parser.error = lambda message: _no_echo_error(parser, message)
    parser.add_argument("--email", help="address to subscribe (never stored; printed masked)")
    args = parser.parse_args(argv)
    address = None
    if args.email is not None:
        try:
            address = normalize_email(args.email)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1

    account = None
    say = lambda msg, dry=False: _aws.report(msg, dry_run=dry and args.dry_run,  # noqa: E731
                                             account=account)
    try:
        sess = _aws.get_session(session)
        account = _aws.account_id(sess)
        sns = _aws.client(sess, "sns")
        arn = config.topic_arn(account)
        attributes = get_topic_attributes(sns, arn)
        arn = _ensure_topic(sns, arn, attributes, dry_run=args.dry_run, say=say)
        requested = False
        if address is not None:
            requested = _ensure_subscription(sns, arn, address, attributes is not None,
                                             dry_run=args.dry_run, say=say)
    except ClientError as exc:
        say(f"ERROR: {exc.operation_name} failed ({_aws.error_code(exc)}); nothing else was "
            "changed after it.")
        return 1
    except BotoCoreError as exc:
        say(f"ERROR: AWS client error ({type(exc).__name__}).")
        return 1

    confirmed, pending = subscription_counts(attributes)
    pending += int(requested)
    say(f"Subscriptions: {confirmed} confirmed, {pending} pending confirmation.")
    if confirmed == 0:
        say("[WARN] no confirmed subscription: failure alerts reach nobody until one is confirmed.")
    say(f"Instance environment ({config.INSTANCE_ENV_FILE}):")
    say(f"  {config.ENV_ALERTS_TOPIC_ARN}={arn}")
    if args.dry_run:
        say("--dry-run: nothing was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
