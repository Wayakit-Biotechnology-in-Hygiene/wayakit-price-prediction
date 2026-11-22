"""Publish a release of the PI code to S3 and have the instance install it on a deploy-only boot (F4).
Called by `.github/workflows/deploy.yml` after CI passes on `main`, and by its manual rollback.

What it leaves configured (bucket `price-intelligence-<account>`, eu-north-1):
- `releases/<sha>.tar.gz`: the archive built by `git archive <sha> | gzip -n` (checked locally
  first: it must contain `config.RELEASE_REQUIRED_FILES`), and `releases/<sha>.tar.gz.sha256`
  (`"<hex>  <sha>.tar.gz\\n"`, the `sha256sum` format) that the instance verifies.
- `releases/previous`: the old value of `releases/current` (only if there was one and it is
  another commit), so a rollback target is always at hand.
- `releases/current`: `"<sha>\\n"`, the release every boot of the instance installs. If the
  deploy fails or times out it is put back to its old value (if there was none, the new value
  stays and a warning says so: the deploy role cannot delete objects).
- `releases/deploy-request.json`: `{"sha", "request_id", "requested_at", "expires_at"}` (epoch
  seconds, valid `config.DEPLOY_REQUEST_TTL_SECONDS`). It tells `pi-deploy.service` that the next
  boot is deploy-only: install, write the result, power off. When the deploy ends (ok, failed,
  timeout or error) it is closed: rewritten with `expires_at == requested_at` and
  `"closed": true`, so a later manual start within the TTL is not powered off.

Steps of `deploy` (every S3 call passes `ExpectedBucketOwner` = the account from STS):
 1. Local checks, no AWS: `--sha` is 40 lowercase hex, `--request-id` is safe for an S3 key,
    the archive is a gzip tar with the required files; neither now nor now + TTL falls on the last
    Thursday of the month in Mexico City time (the whole day is reserved for the monthly run: a
    deploy boot left on would turn the scheduled start into a no-op), and no minute between now
    and now + TTL falls in the monthly window (`pi_boot_guard.is_scheduled_window`: last Thursday
    21:40-23:00 Mexico City).
 2. STS `GetCallerIdentity`; the instance by Name tag (`instance_role.find_instance_id`), then
    `DescribeInstances` on its ID: refused unless it is `stopped` (a run or a maintenance session
    may be in progress). Nothing has been written yet.
 3. Read `releases/current` (`ListObjectsV2` with that exact prefix, then `GetObject`).
    `--dry-run` stops here and prints the plan.
 4. Put the archive, its `.sha256`, `previous`, `current` and the request; `StartInstances`.
 5. Poll `state/deploys/<request_id>.json` (written by the instance) every 15 s for up to
    `--timeout-minutes`: `ok` -> exit 0; `failed`, a result for another sha or request, or no
    result in time -> `current` moved back, exit 1. An AWS error after `current` moved also moves
    it back before it propagates.

Exit codes: 0 deployed (or dry-run plan with nothing to refuse); 1 refused, failed or timed out
(message on stderr starting with `REFUSED:` or `FAILED:`); 2 bad arguments (argparse). AWS errors
become one sanitized line and exit 1 (`_aws.cli_main`). Output never shows the account, bucket
name or instance ID.

Credentials: `--env-credentials` uses boto3's default chain (the short-lived OIDC credentials of
the GitHub Actions job, role `price-intelligence-deploy`; see `infra/deploy_role.py` for exactly
what it may do); without it, the `wayakit-vdc` profile. Never the profile `default`.

Cost: S3 requests and a few hundred KB per archive (archives are not pruned; old versions of
overwritten keys expire with the bucket lifecycle), plus the minutes the instance runs to install.

Usage (from the repo root; in practice only the workflow runs it):
    git archive --format=tar <sha> | gzip -n > release.tar.gz
    python -m infra.release deploy --sha <sha> --archive release.tar.gz --request-id manual-1 --dry-run
    python -m infra.release deploy --env-credentials --sha <sha> --archive release.tar.gz \\
        --request-id <run_id>-<attempt>
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import re
import sys
import tarfile
import time
from zoneinfo import ZoneInfo

from infra import _aws, config
from infra.instance.pi_boot_guard import is_last_thursday, is_scheduled_window
from infra.instance_role import _info, _redact, find_instance_id

SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
POLL_SECONDS = 15
DEFAULT_TIMEOUT_MINUTES = 15
MAX_TIMEOUT_MINUTES = config.DEPLOY_REQUEST_TTL_SECONDS // 60  # waiting past the request is pointless
WINDOW_CHECK_STEP = dt.timedelta(minutes=1)
REASON_MAX_CHARS = 200
EXIT_OK, EXIT_FAILED = 0, 1


# --- Pure helpers ------------------------------------------------------------------------------
def archive_key(sha):
    return f"{config.RELEASES_PREFIX}{sha}.tar.gz"


def checksum_key(sha):
    return f"{archive_key(sha)}.sha256"


def result_key(request_id):
    return f"{config.DEPLOYS_STATE_PREFIX}{request_id}.json"


def checksum_body(digest, sha):
    """`sha256sum` line for the archive: two spaces, the file name, a newline."""
    return f"{digest}  {sha}.tar.gz\n"


def pointer_body(sha):
    return f"{sha}\n"


def parse_pointer(body):
    """Commit SHA held by a pointer object (`bytes`). Raises ValueError unless it is 40 lowercase
    hex characters (surrounding whitespace allowed)."""
    text = body.decode("utf-8", errors="replace").strip()
    if not SHA_PATTERN.fullmatch(text):
        raise ValueError("pointer is not a 40-character lowercase hex SHA")
    return text


def window_conflict(now, ttl_seconds=config.DEPLOY_REQUEST_TTL_SECONDS, tz=config.SCHEDULE_TIMEZONE):
    """First minute in [now, now + ttl] inside the monthly boot window, or None. `now` must be
    timezone-aware. Minute steps are enough: the window is 80 minutes long."""
    steps = int(ttl_seconds // WINDOW_CHECK_STEP.total_seconds())
    for i in range(steps + 1):
        moment = now + i * WINDOW_CHECK_STEP
        if is_scheduled_window(moment, tz):
            return moment
    return None


def reserved_day(now, ttl_seconds=config.DEPLOY_REQUEST_TTL_SECONDS, tz=config.SCHEDULE_TIMEZONE):
    """Local date (in `tz`) of the last Thursday of the month touched by [now, now + ttl], or None.
    The whole day is reserved: a deploy boot that hangs early on that day (e.g. S3 unreachable)
    leaves the instance on, the 21:50 StartInstances is then a no-op and the monthly run would be
    skipped without any alert. The interval is shorter than a day, so its two ends cover it."""
    for moment in (now, now + dt.timedelta(seconds=ttl_seconds)):
        day = moment.astimezone(ZoneInfo(tz)).date()
        if is_last_thursday(day):
            return day
    return None


def build_request(sha, request_id, now, ttl_seconds=config.DEPLOY_REQUEST_TTL_SECONDS):
    requested_at = int(now.timestamp())
    return {"sha": sha, "request_id": request_id, "requested_at": requested_at,
            "expires_at": requested_at + int(ttl_seconds)}


def closed_request(request):
    """The same request, already expired: no boot treats it as deploy-only any more."""
    return {**request, "expires_at": request["requested_at"], "closed": True}


def request_body(request):
    return (json.dumps(request, sort_keys=True) + "\n").encode()


def _clean(text):
    """Printable ASCII only, cut to REASON_MAX_CHARS: the reason comes from an S3 object."""
    return "".join(ch for ch in str(text) if " " <= ch <= "~")[:REASON_MAX_CHARS]


def parse_result(body, sha, request_id):
    """(`ok` | `failed`, reason) from the result object the instance wrote. Anything that is not
    a JSON object for this request and this sha with `result` ok/failed counts as failed."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "failed", "result object is not valid JSON"
    if not isinstance(data, dict):
        return "failed", "result object is not a JSON object"
    if data.get("request_id") != request_id or data.get("sha") != sha:
        return "failed", "result object is for another request or sha"
    outcome = data.get("result")
    if outcome not in ("ok", "failed"):
        return "failed", "result object has no ok/failed result"
    return outcome, _clean(data.get("reason") or "")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def missing_release_files(path):
    """Required files (`config.RELEASE_REQUIRED_FILES`) that are not regular files in the gzip tar
    at `path`. Raises ValueError if it is not a readable gzip tar."""
    try:
        with tarfile.open(path, mode="r:gz") as tar:
            present = {m.name.removeprefix("./") for m in tar.getmembers() if m.isfile()}
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise ValueError(f"archive is not a readable gzip tar ({type(exc).__name__})") from None
    return [name for name in config.RELEASE_REQUIRED_FILES if name not in present]


# --- S3 access ---------------------------------------------------------------------------------
class ReleaseStore:
    """The few S3 operations the deploy role may do, always with `ExpectedBucketOwner`."""

    def __init__(self, s3, account):
        self.s3, self.bucket, self.owner = s3, config.bucket_name(account), account

    def exists(self, key):
        # ListObjectsV2 instead of a failed GetObject: without ListBucket on the key, S3 answers
        # AccessDenied for a missing key, and "missing" must not look like a permission problem.
        page = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=key, ExpectedBucketOwner=self.owner)
        return any(obj.get("Key") == key for obj in page.get("Contents", []))

    def read(self, key):
        """Body bytes, or None if the key does not exist."""
        if not self.exists(key):
            return None
        return self.s3.get_object(Bucket=self.bucket, Key=key, ExpectedBucketOwner=self.owner)["Body"].read()

    def put(self, key, body, content_type):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type,
                           ExpectedBucketOwner=self.owner)


# --- Deploy ------------------------------------------------------------------------------------
def _stop(kind, message, account=None):
    """Print `REFUSED:`/`FAILED:` plus the redacted message on stderr; return EXIT_FAILED."""
    print(_redact(f"{kind}: {message}", account), file=sys.stderr)
    return EXIT_FAILED


def instance_state(ec2, instance_id):
    page = ec2.describe_instances(InstanceIds=[instance_id])
    states = [i["State"]["Name"] for r in page.get("Reservations", []) for i in r.get("Instances", [])]
    if len(states) != 1:
        raise SystemExit("DescribeInstances did not return exactly the PI instance.")
    return states[0]


def _restore_pointer(store, old, sha, account):
    """Put `releases/current` back to `old`. Never raises: a failure is printed as MANUAL ACTION."""
    if old is None:
        print(f"WARNING: there was no {config.RELEASES_CURRENT_KEY} before; it stays at {sha} (the deploy role "
              "cannot delete it). The next boot will try to install it again.", file=sys.stderr)
        return
    if old == sha:
        return
    try:
        store.put(config.RELEASES_CURRENT_KEY, pointer_body(old).encode(), "text/plain")
        _info(f"{config.RELEASES_CURRENT_KEY} moved back to {old}", account=account)
    except Exception as exc:  # noqa: BLE001 - reported loudly; the original failure still exits 1
        print(_redact(f"MANUAL ACTION: could not move {config.RELEASES_CURRENT_KEY} back to {old} "
                      f"({type(exc).__name__} {_aws.error_code(exc) or ''}). Put it back by hand "
                      "(docs/runbook.md, Deploy and rollback).", account), file=sys.stderr)


def _close_request(store, request, account):
    """Expire the deploy request. Never raises: it expires on its own after the TTL anyway."""
    try:
        store.put(config.DEPLOY_REQUEST_KEY, request_body(closed_request(request)), "application/json")
    except Exception as exc:  # noqa: BLE001 - see docstring; reported, not fatal
        print(_redact(f"WARNING: could not close {config.DEPLOY_REQUEST_KEY} ({type(exc).__name__} "
                      f"{_aws.error_code(exc) or ''}); it expires on its own at {request['expires_at']}.",
                      account), file=sys.stderr)


def wait_for_result(store, sha, request_id, timeout_minutes, sleep):
    """Poll the result object. Returns (`ok` | `failed` | `timeout`, reason). Exactly
    `ceil(timeout_minutes * 60 / POLL_SECONDS)` polls, each one after `sleep(POLL_SECONDS)`."""
    key = result_key(request_id)
    polls = math.ceil(timeout_minutes * 60 / POLL_SECONDS)
    for _ in range(polls):
        sleep(POLL_SECONDS)
        body = store.read(key)
        if body is not None:
            return parse_result(body, sha, request_id)
    return "timeout", f"no result at {key} after {timeout_minutes} minutes"


def deploy(sess, args, *, now, sleep):
    sha, request_id = args.sha, args.request_id
    if not SHA_PATTERN.fullmatch(sha or ""):
        return _stop("REFUSED", "--sha must be 40 lowercase hex characters")
    if not REQUEST_ID_PATTERN.fullmatch(request_id or ""):
        return _stop("REFUSED", "--request-id must be 1-64 letters, digits or '-' (starting with a letter or digit)")
    if not 1 <= args.timeout_minutes <= MAX_TIMEOUT_MINUTES:
        return _stop("REFUSED", f"--timeout-minutes must be between 1 and {MAX_TIMEOUT_MINUTES}")
    try:
        missing = missing_release_files(args.archive)
        with open(args.archive, "rb") as handle:
            data = handle.read()
    except (OSError, ValueError) as exc:
        return _stop("REFUSED", f"cannot use the archive: {exc}")
    if missing:
        return _stop("REFUSED", f"the archive lacks required files: {', '.join(missing)}")
    day = reserved_day(now)
    if day is not None:
        return _stop("REFUSED", f"the last Thursday of the month is reserved for the monthly run ({day:%Y-%m-%d}, "
                                f"{config.SCHEDULE_TIMEZONE}); deploy another day")
    conflict = window_conflict(now)  # inside the reserved day; kept as a second, independent guard
    if conflict is not None:
        return _stop("REFUSED", f"the monthly run window starts within {config.DEPLOY_REQUEST_TTL_SECONDS // 60} "
                                f"minutes or is open ({conflict.astimezone(dt.timezone.utc):%Y-%m-%d %H:%M} UTC is inside it); "
                                "deploy later")

    account = _aws.account_id(sess)
    ec2 = _aws.client(sess, "ec2")
    instance_id = find_instance_id(ec2)
    state = instance_state(ec2, instance_id)
    if state != "stopped":
        return _stop("REFUSED", f"instance {config.INSTANCE_NAME} is {state}, not stopped (a run or a maintenance "
                                "session may be in progress); nothing was changed", account)

    store = ReleaseStore(_aws.client(sess, "s3"), account)
    old_body = store.read(config.RELEASES_CURRENT_KEY)
    try:
        old = None if old_body is None else parse_pointer(old_body)
    except ValueError:
        return _stop("REFUSED", f"{config.RELEASES_CURRENT_KEY} is malformed; fix it by hand first", account)

    digest = sha256_bytes(data)
    request = build_request(sha, request_id, now)
    plan = [f"upload {archive_key(sha)} ({len(data)} bytes, sha256 {digest}) and {checksum_key(sha)}",
            f"set {config.RELEASES_PREVIOUS_KEY} to {old}" if old and old != sha
            else f"leave {config.RELEASES_PREVIOUS_KEY} as it is",
            f"set {config.RELEASES_CURRENT_KEY} to {sha} (was {old or 'missing'})",
            f"write {config.DEPLOY_REQUEST_KEY} (request {request_id}, valid until {request['expires_at']})",
            f"start instance {config.INSTANCE_NAME} (stopped) and wait up to {args.timeout_minutes} minutes "
            f"for {result_key(request_id)}"]
    if args.dry_run:
        for line in plan:
            _aws.report(_redact(line, account), dry_run=True, account=account)
        _info("--dry-run: nothing was changed.", account=account)
        return EXIT_OK

    store.put(archive_key(sha), data, "application/gzip")
    store.put(checksum_key(sha), checksum_body(digest, sha).encode(), "text/plain")
    _info(f"-> uploaded {archive_key(sha)} and {checksum_key(sha)}", account=account)
    if old and old != sha:
        store.put(config.RELEASES_PREVIOUS_KEY, pointer_body(old).encode(), "text/plain")
    try:
        # From here on `current` points at the new release: any failure moves it back.
        store.put(config.RELEASES_CURRENT_KEY, pointer_body(sha).encode(), "text/plain")
        _info(f"-> {config.RELEASES_CURRENT_KEY} = {sha} (was {old or 'missing'})", account=account)
        store.put(config.DEPLOY_REQUEST_KEY, request_body(request), "application/json")
        ec2.start_instances(InstanceIds=[instance_id])
        _info(f"-> started {config.INSTANCE_NAME}; waiting for {result_key(request_id)}", account=account)
        outcome, reason = wait_for_result(store, sha, request_id, args.timeout_minutes, sleep)
    except BaseException:
        _restore_pointer(store, old, sha, account)
        _close_request(store, request, account)
        raise
    if outcome == "ok":
        _close_request(store, request, account)
        _info(f"OK: release {sha} deployed (request {request_id})" + (f": {reason}" if reason else ""),
              account=account)
        return EXIT_OK
    _restore_pointer(store, old, sha, account)
    _close_request(store, request, account)
    if outcome == "timeout":
        # The deploy role has no StopInstances (by design), so a person has to look and stop it.
        return _stop("FAILED", f"{reason}; the instance may still be running and this job cannot stop it: "
                               f"check {config.INSTANCE_NAME} in the EC2 console or over SSM "
                               f"(journalctl -u {config.DEPLOY_SERVICE} -b; deploy.log in the log folder) "
                               "and stop it by hand", account)
    return _stop("FAILED", f"the instance reported a failed deploy: {reason or 'no reason given'}", account)


# --- Entry point -------------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("deploy", help="publish a release and install it on a deploy-only boot")
    cmd.add_argument("--sha", required=True, help="commit to deploy (40 lowercase hex characters)")
    cmd.add_argument("--archive", required=True, help="gzip tar built with `git archive <sha> | gzip -n`")
    cmd.add_argument("--request-id", required=True, help="unique ID of this deploy (Actions: <run_id>-<attempt>)")
    cmd.add_argument("--timeout-minutes", type=int, default=DEFAULT_TIMEOUT_MINUTES,
                     help=f"how long to wait for the instance's result (default {DEFAULT_TIMEOUT_MINUTES})")
    cmd.add_argument(_aws.ENV_CREDENTIALS_FLAG, dest="env_credentials", action="store_true",
                     help="use the environment credentials (GitHub Actions OIDC) instead of the profile")
    cmd.add_argument("--dry-run", action="store_true", help="only read calls; print the plan")
    return parser


def main(argv=None, session=None, *, now=None, sleep=time.sleep):
    """`now`: timezone-aware datetime used for the window check and the request timestamps
    (default: the current UTC time). `sleep`: called with POLL_SECONDS before each poll (tests
    pass a fake). Returns the exit code."""
    args = build_parser().parse_args(argv)
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    sess = _aws.get_session(session, env_credentials=args.env_credentials)
    return deploy(sess, args, now=now, sleep=sleep)


if __name__ == "__main__":
    _aws.cli_main(main)
