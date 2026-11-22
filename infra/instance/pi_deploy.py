#!/usr/bin/env python3
"""Release installer of the Price Intelligence instance (F4, pull-on-boot).

What it leaves configured on `wayakit-app-server` (run as root by `pi-deploy.service` on every
boot, before `pi-pipeline.service`):
- `/home/ec2-user/wayakit-price-prediction` (APP_LINK, the fixed path of `pi-pipeline.service`)
  is a symlink to `/home/ec2-user/pi-releases/<sha>`, the release named by the S3 pointer
  `releases/current` (40 lowercase hex). The archive `releases/<sha>.tar.gz` is checked against
  `releases/<sha>.tar.gz.sha256`, unpacked safely (regular files, directories and symlinks
  without `..` or absolute targets only), must hold RELEASE_REQUIRED_FILES, pass
  `bash -n run_pipeline.sh` and its own pi_deploy.py must start (`--help`) under the system
  python3; then it gets a `.release` file with the sha and is owned by ec2-user. The switch is one `rename(2)` of a new
  symlink onto APP_LINK: on any failure before it, APP_LINK is never touched.
- Legacy migration (first F4 install): if APP_LINK is a real directory (the old git checkout), it
  is renamed to `/home/ec2-user/wayakit-price-prediction.pre-f4` right before the switch (it
  refuses if that backup already exists) and put back if the switch fails. It is never pruned.
- The unit files (`pi-pipeline.service`, `pi-deploy.service`) in /etc/systemd/system and this
  deployer (`pi_deploy.py`, `pi_boot_guard.py`) in /opt/price-intelligence are synced from the
  active release when they differ (Python files are compiled first; `daemon-reload` if a unit
  changed). The deployer lives outside the releases so a bad release cannot break it.
- At most RELEASES_TO_KEEP release folders: always the active one and the one before it, then
  the newest. Other folders in /home/ec2-user/pi-releases are left alone.
- `deploy.log` (append) in PI_LOG_DIR (from /etc/price-intelligence.env; default
  /home/ec2-user/logs). Nothing is written inside the release folders except `.release`.

Deploy mode: `infra/release.py` (GitHub Actions) writes `releases/deploy-request.json`
({"sha", "request_id", "requested_at", "expires_at"}, epoch seconds) and starts the stopped
instance. A boot is in deploy mode only if that request is valid, not closed and
requested_at <= now < expires_at (with CLOCK_SKEW_SECONDS of slack before requested_at), and no
result exists yet for its request_id. The STS call and the reads that decide this (request and
result) are retried on failure (READ_ATTEMPTS, about 2 minutes; never on "not found"): if they
still fail, the boot cannot know it was a deploy, so it fails as a normal boot and stays on
(`release.py` times out; stop the instance by hand). In deploy mode it also:
- writes `state/deploys/<request_id>.json` {"request_id", "sha", "result": "ok"|"failed",
  "reason", "finished_at" (epoch s), "previous"} (`reason` is one of the fixed REASON_* strings,
  never AWS text);
- powers the instance off at the end (`systemctl poweroff --no-block`, also after a failure)
  unless the boot guard says this is the monthly window (then the pipeline runs next) or
  --no-poweroff. A deploy boot therefore never runs the pipeline: `pi-pipeline.service` is
  ordered after this unit and its own guard skips it outside the window.
Any other boot is a normal boot: it installs the pointer's release if it is not active yet (the
pointer is the source of truth), writes no result and never powers off. A missing pointer on a
normal boot is not an error ("no release pointer yet").

environment.yml: if the new release changes it, the install stops before the switch ("run with
--allow-env-update over SSM"); with --allow-env-update it first runs
`conda env update -n wayakit_env -f <release>/environment.yml` as ec2-user.

On failure with PI_ALERTS_TOPIC_ARN set: `aws sns publish` (best effort; a failed publish is
logged). AWS is reached only through the `aws` CLI v2 with the instance role; every `s3api` call
passes `--region eu-north-1 --expected-bucket-owner <account>` (account from STS). It never
prints AWS error text (it can carry ARNs): only "aws <op> failed (exit N, <ErrorCode>)". The
account and the bucket name are redacted from every line it prints.

Standalone on purpose (stdlib only, Python >= 3.9; the only import is `pi_boot_guard` from the
same folder): it runs with the system `python3` of Amazon Linux 2023. The constants below must
stay equal to their `infra.config` counterparts (a unit test checks them).

Exit codes: 0 ok (installed, already active, no pointer yet on a normal boot, dry-run);
1 failed (any error, including bad arguments). Every failure leaves APP_LINK as it was, except a
failed sync of the system files, which happens after the switch (the result is "failed", so
`release.py` restores the pointer and the next boot switches back). A failed prune of old
releases, also after the switch, does not undo a good release: the result stays "ok", it alerts
and exits 1.

Cost: S3 GETs of a few hundred KB per installed release.

Usage (as root, on the instance, e.g. over SSM):
    sudo /usr/bin/python3 /opt/price-intelligence/pi_deploy.py --dry-run   # reads only, prints the plan
    sudo /usr/bin/python3 /opt/price-intelligence/pi_deploy.py             # what the service runs
    sudo /usr/bin/python3 /opt/price-intelligence/pi_deploy.py --allow-env-update --sha <sha>
Options: --sha SHA (install that release instead of the pointer; never deploy mode),
--allow-env-update, --bucket NAME (default price-intelligence-<account>), --no-poweroff,
--dry-run.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:  # imported as infra.instance.pi_deploy (tests)
    from . import pi_boot_guard
except ImportError:  # run as /opt/price-intelligence/pi_deploy.py: its folder is sys.path[0]
    import pi_boot_guard

# --- Constants (== infra.config; a unit test keeps them in sync) -------------------------------
REGION = "eu-north-1"  # == config.REGION
BUCKET_PREFIX = "price-intelligence-"  # == config.bucket_name("")
RELEASES_PREFIX = "releases/"  # == config.RELEASES_PREFIX
RELEASES_CURRENT_KEY = "releases/current"  # == config.RELEASES_CURRENT_KEY
DEPLOY_REQUEST_KEY = "releases/deploy-request.json"  # == config.DEPLOY_REQUEST_KEY
DEPLOYS_STATE_PREFIX = "state/deploys/"  # == config.DEPLOYS_STATE_PREFIX
RELEASES_TO_KEEP = 5  # == config.RELEASES_TO_KEEP
APP_DIR = "/home/ec2-user/wayakit-price-prediction"  # == config.INSTANCE_APP_DIR
RELEASES_DIR = "/home/ec2-user/pi-releases"  # == config.INSTANCE_RELEASES_DIR
DEPLOYER_DIR = "/opt/price-intelligence"  # == config.INSTANCE_DEPLOYER_DIR
DEPLOY_SERVICE = "pi-deploy.service"  # == config.DEPLOY_SERVICE
PIPELINE_SERVICE = "pi-pipeline.service"  # == config.PIPELINE_SERVICE
CONDA_ENV = "wayakit_env"  # == config.CONDA_ENV
ENV_LOG_DIR = "PI_LOG_DIR"  # == config.ENV_LOG_DIR
ENV_ALERTS_TOPIC_ARN = "PI_ALERTS_TOPIC_ARN"  # == config.ENV_ALERTS_TOPIC_ARN
RELEASE_REQUIRED_FILES = (  # == config.RELEASE_REQUIRED_FILES
    "run_pipeline.sh",
    "environment.yml",
    "infra/instance/pi_boot_guard.py",
    "infra/instance/pi-pipeline.service",
    "infra/instance/pi-deploy.service",
    "infra/instance/pi_deploy.py",
)

# --- Instance-only constants -------------------------------------------------------------------
PI_USER = "ec2-user"
DEFAULT_LOG_DIR = "/home/ec2-user/logs"  # same default as run_pipeline.sh and bootstrap.sh
LOG_FILE = "deploy.log"
SYSTEMD_DIR = "/etc/systemd/system"
CONDA_BIN = "/home/ec2-user/miniconda/bin/conda"  # bootstrap.sh CONDA_DIR
LEGACY_SUFFIX = ".pre-f4"
RELEASE_MARKER = ".release"
LOCK_FILE = ".deploy.lock"
SYSTEM_PYTHON = "/usr/bin/python3"  # the interpreter of pi-deploy.service's ExecStart
TMP_PREFIX = ".tmp-"
# (file in the release, destination folder attribute of Paths)
SYSTEM_FILES = (
    ("infra/instance/pi-pipeline.service", "systemd_dir"),
    ("infra/instance/pi-deploy.service", "systemd_dir"),
    ("infra/instance/pi_deploy.py", "deployer_dir"),
    ("infra/instance/pi_boot_guard.py", "deployer_dir"),
)
CLOCK_SKEW_SECONDS = 60  # slack for requested_at (GitHub runner clock vs instance clock)
MAX_UNPACKED_BYTES = 1 << 30  # a release is a few MB; this only stops a runaway archive
AWS_TIMEOUT_SECONDS = 300  # release archive download
AWS_SMALL_TIMEOUT_SECONDS = 20  # STS and the small objects (pointer, request, result)
# Retries of STS and of the deploy-request reads, the calls that decide whether this is a deploy
# boot: if they failed for good (network not ready yet), nothing would write a result or power
# off, and the deploy role cannot stop the instance. Only failures are retried, never "not found".
# Worst case 6 x (20 s timeout) per call + 105 s of waits, inside TimeoutStartSec=10min.
READ_ATTEMPTS = 6
RETRY_DELAYS_SECONDS = (5, 10, 20, 30, 40)  # waits between attempts (READ_ATTEMPTS - 1)

SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")  # == release.py --request-id check
BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
ACCOUNT_RE = re.compile(r"[0-9]{12}")
AWS_ERROR_CODE_RE = re.compile(r"An error occurred \(([A-Za-z0-9_.]{1,64})\)")

EXIT_OK = 0
EXIT_FAILED = 1

# Fixed reasons: the only text that goes into the result object and the alert.
REASON_DEPLOYED = "deployed"
REASON_ALREADY_CURRENT = "already current"
REASON_NO_POINTER = "no release pointer"
REASON_BAD_POINTER = "invalid release pointer"
REASON_REQUEST_MISMATCH = "pointer does not match the deploy request"
REASON_STORE = "S3 read or write failed"
REASON_ARCHIVE_MISSING = "release archive missing"
REASON_CHECKSUM = "release checksum mismatch"
REASON_UNSAFE_ARCHIVE = "unsafe release archive"
REASON_MISSING_FILE = "release is missing a required file"
REASON_BASH_SYNTAX = "run_pipeline.sh has a syntax error"
REASON_ENV_CHANGED = "environment.yml changed: run with --allow-env-update over SSM"
REASON_CONDA = "conda env update failed"
REASON_PIPELINE_RUNNING = "the pipeline is running"
REASON_LEGACY_BACKUP_EXISTS = "legacy backup already exists"
REASON_SWITCH = "symlink switch failed"
REASON_SYSTEM_FILES = "system files sync failed"
REASON_PRUNE = "pruning old releases failed"
REASON_DEPLOYER = "the release's pi_deploy.py does not run"
REASON_LOCKED = "another pi_deploy is running"
REASON_COMMAND = "local command failed"
REASON_UNEXPECTED = "unexpected error"


class DeployError(Exception):
    """A failed step. `reason` is a REASON_* constant (result object and alert); `detail` only
    goes to the log and must never hold AWS text, the account or the bucket name."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class StoreError(DeployError):
    """An S3 call failed. The message is "aws <op> failed (exit N[, <ErrorCode>])" only."""

    def __init__(self, op: str, exit_code, error_code: str = ""):
        suffix = f", {error_code}" if error_code else ""
        super().__init__(REASON_STORE, f"aws {op} failed (exit {exit_code}{suffix})")


class UnsafeArchiveError(DeployError):
    def __init__(self, detail: str):
        super().__init__(REASON_UNSAFE_ARCHIVE, detail)


# --- Paths ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Paths:
    """Every filesystem location the deployer touches; tests point them all at tmp_path."""

    app_link: Path = Path(APP_DIR)
    releases_dir: Path = Path(RELEASES_DIR)
    deployer_dir: Path = Path(DEPLOYER_DIR)
    systemd_dir: Path = Path(SYSTEMD_DIR)
    log_dir: Path = Path(DEFAULT_LOG_DIR)

    @property
    def legacy_backup(self) -> Path:
        return self.app_link.with_name(self.app_link.name + LEGACY_SUFFIX)

    def release(self, sha: str) -> Path:
        return self.releases_dir / sha

    @classmethod
    def from_environ(cls, environ) -> "Paths":
        log_dir = (environ.get(ENV_LOG_DIR) or "").strip() or DEFAULT_LOG_DIR
        if not os.path.isabs(log_dir) or ".." in Path(log_dir).parts:
            raise DeployError(REASON_COMMAND, f"{ENV_LOG_DIR} must be an absolute path without '..'")
        return cls(log_dir=Path(log_dir))


# --- Pure helpers ------------------------------------------------------------------------------
def is_sha(text) -> bool:
    """40 lowercase hex characters (a full git commit SHA)."""
    return isinstance(text, str) and SHA_RE.fullmatch(text) is not None


def parse_pointer(data: bytes) -> str:
    """Content of `releases/current` ("<sha>\\n") -> sha. Raises DeployError otherwise."""
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        text = ""
    if not is_sha(text):
        raise DeployError(REASON_BAD_POINTER, "releases/current is not a 40-hex commit SHA")
    return text


def parse_request(data: bytes, now_epoch: float):
    """Content of `releases/deploy-request.json` -> the request dict if it puts this boot in
    deploy mode, else None (invalid JSON, wrong types, bad sha or request_id, closed, expired or
    not yet valid). Pure: never raises."""
    try:
        request = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(request, dict) or request.get("closed") is True:
        return None
    sha, request_id = request.get("sha"), request.get("request_id")
    requested_at, expires_at = request.get("requested_at"), request.get("expires_at")
    if not is_sha(sha) or not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return None
    for value in (requested_at, expires_at):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
    if not requested_at - CLOCK_SKEW_SECONDS <= now_epoch < expires_at:
        return None
    return {"sha": sha, "request_id": request_id, "requested_at": requested_at,
            "expires_at": expires_at}


def parse_sha256_file(data: bytes, expected_name: str) -> str:
    """Content of `<sha>.tar.gz.sha256` ("<hex>  <sha>.tar.gz\\n", sha256sum format) -> hex.
    The file name, if present, must be `expected_name`. Raises DeployError."""
    try:
        parts = data.decode("ascii").split()
    except UnicodeDecodeError:
        parts = []
    if not parts or not SHA256_RE.fullmatch(parts[0]) or len(parts) > 2:
        raise DeployError(REASON_CHECKSUM, "the .sha256 file is malformed")
    if len(parts) == 2 and parts[1].lstrip("*") != expected_name:
        raise DeployError(REASON_CHECKSUM, "the .sha256 file names another archive")
    return parts[0]


def safe_member_path(name: str) -> str:
    """Tar member name -> normalized relative path. Raises UnsafeArchiveError for an empty name,
    a NUL, an absolute path, any `..` component, a name that is only `.`, or the reserved
    `.release` at the top."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise UnsafeArchiveError("empty or invalid member name")
    if name.startswith("/"):
        raise UnsafeArchiveError("absolute member name")
    if ".." in name.split("/"):
        raise UnsafeArchiveError("member name with '..'")
    path = posixpath.normpath(name)
    if path in (".", "") or path.startswith("/") or path == ".." or path.startswith("../"):
        raise UnsafeArchiveError("member name outside the release")
    if path == RELEASE_MARKER:
        raise UnsafeArchiveError("member name is reserved")
    return path


def safe_link_target(member_path: str, linkname: str) -> str:
    """Symlink at `member_path` (already normalized) pointing at `linkname` -> the normalized
    path it resolves to, inside the release. Raises UnsafeArchiveError for an empty or absolute
    target or one with any `..` component, even one that stays inside: resolving text alone is
    not enough once links chain (`d1/s -> ..` then `d1/e -> s/..` leaves the release). Without
    `..` and with no member under a symlink (check_members), every link points at or below its
    own folder. The repo has no symlinks today."""
    if not isinstance(linkname, str) or not linkname or "\x00" in linkname:
        raise UnsafeArchiveError("empty symlink target")
    if linkname.startswith("/"):
        raise UnsafeArchiveError("absolute symlink target")
    if ".." in linkname.split("/"):
        raise UnsafeArchiveError("symlink target with '..'")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(member_path), linkname))
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        raise UnsafeArchiveError("symlink target outside the release")
    return resolved


def check_members(members) -> list:
    """Validate a whole archive listing before anything is written.

    `members`: iterable of (name, kind, linkname, size) with kind in {"file", "dir", "symlink",
    "other"}. Returns [(path, kind, linkname)] in archive order. Raises UnsafeArchiveError for
    any unsafe name or link, "other" kinds (hard links, devices, fifos), duplicate paths, a
    member under a symlink or under a file, or more than MAX_UNPACKED_BYTES in total."""
    plan, kinds, total = [], {}, 0
    for name, kind, linkname, size in members:
        path = safe_member_path(name)
        if kind not in ("file", "dir", "symlink"):
            raise UnsafeArchiveError("member type not allowed (only files, folders, symlinks)")
        if kind == "symlink":
            safe_link_target(path, linkname)
        if path in kinds:
            if not (kind == "dir" and kinds[path] == "dir"):
                raise UnsafeArchiveError("duplicate member")
            continue
        kinds[path] = kind
        total += max(int(size or 0), 0)
        if total > MAX_UNPACKED_BYTES:
            raise UnsafeArchiveError("archive too large")
        plan.append((path, kind, linkname))
    for path in kinds:
        parent = posixpath.dirname(path)
        while parent:
            if kinds.get(parent) in ("symlink", "file"):
                raise UnsafeArchiveError("member inside a symlink or a file")
            parent = posixpath.dirname(parent)
    return plan


def releases_to_prune(mtimes: dict, current: str, previous=None, keep: int = RELEASES_TO_KEEP) -> list:
    """Release folder name -> mtime  =>  names to delete, sorted. Only 40-hex names count (and
    only those can be returned). Always kept: `current` and `previous`; then the newest by mtime
    (ties by name) up to `keep` in total."""
    names = sorted((n for n in mtimes if is_sha(n)), key=lambda n: (-mtimes[n], n))
    kept = [n for n in (current, previous) if n and n in mtimes and is_sha(n)]
    kept = list(dict.fromkeys(kept))
    for name in names:
        if len(kept) >= keep:
            break
        if name not in kept:
            kept.append(name)
    return sorted(n for n in names if n not in kept)


def should_power_off(deploy_mode: bool, now_utc: datetime, no_poweroff: bool) -> bool:
    """Power off only after a deploy-mode boot, never inside the monthly window (the pipeline
    runs next) and never with --no-poweroff."""
    return deploy_mode and not no_poweroff and not pi_boot_guard.is_scheduled_window(now_utc)


def result_object(request_id: str, sha, ok: bool, reason: str, finished_at: int, previous) -> dict:
    return {"request_id": request_id, "sha": sha, "result": "ok" if ok else "failed",
            "reason": reason, "finished_at": int(finished_at), "previous": previous}


def aws_error_code(stderr) -> str:
    """The error code of an AWS CLI error ("An error occurred (Code) ..."), or "" (never the
    message: it can carry ARNs with the account)."""
    match = AWS_ERROR_CODE_RE.search(stderr or "")
    return match.group(1) if match else ""


# --- Side effects ------------------------------------------------------------------------------
def run_command(argv, timeout=None):
    """Default runner: argv list -> subprocess.CompletedProcess (text, captured, no stdin)."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, check=False)


def _run(runner, argv, what, timeout=None):
    """Run a local command; non-zero exit or timeout -> DeployError (the argv is not logged)."""
    try:
        proc = runner(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DeployError(REASON_COMMAND, f"{what} timed out") from None
    except OSError as exc:
        raise DeployError(REASON_COMMAND, f"{what} could not run ({exc.strerror})") from None
    if proc.returncode != 0:
        raise DeployError(REASON_COMMAND, f"{what} failed (exit {proc.returncode})")
    return proc


class AwsCliStore:
    """S3 through the AWS CLI v2. Keys are relative to the PI bucket; every call passes the
    region and `--expected-bucket-owner`."""

    def __init__(self, bucket: str, account: str, runner=run_command, aws: str = "aws"):
        self._bucket, self._account, self._runner, self._aws = bucket, account, runner, aws

    def _call(self, op, args):
        argv = [self._aws, "s3api", op, "--bucket", self._bucket, *args, "--region", REGION,
                "--expected-bucket-owner", self._account, "--no-cli-pager", "--output", "json"]
        timeout = AWS_TIMEOUT_SECONDS if ".tar.gz" in " ".join(args) else AWS_SMALL_TIMEOUT_SECONDS
        try:
            return self._runner(argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise StoreError(op, "timeout") from None
        except OSError as exc:
            raise StoreError(op, f"not runnable: {exc.strerror}") from None

    def get(self, key: str, dest_path) -> bool:
        """Download `key` to `dest_path`. False if the key does not exist; StoreError otherwise."""
        proc = self._call("get-object", ["--key", key, str(dest_path)])
        if proc.returncode == 0:
            return True
        code = aws_error_code(proc.stderr)
        if code == "NoSuchKey":
            Path(dest_path).unlink(missing_ok=True)
            return False
        raise StoreError("s3api get-object", proc.returncode, code)

    def put(self, key: str, src_path) -> None:
        proc = self._call("put-object", ["--key", key, "--body", str(src_path),
                                         "--content-type", "application/json"])
        if proc.returncode != 0:
            raise StoreError("s3api put-object", proc.returncode, aws_error_code(proc.stderr))


def account_id(runner) -> str:
    argv = ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text",
            "--region", REGION, "--no-cli-pager"]
    try:
        proc = runner(argv, timeout=AWS_SMALL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise StoreError("sts get-caller-identity", "timeout") from None
    except OSError as exc:
        raise StoreError("sts get-caller-identity", f"not runnable: {exc.strerror}") from None
    if proc.returncode != 0:
        raise StoreError("sts get-caller-identity", proc.returncode, aws_error_code(proc.stderr))
    account = (proc.stdout or "").strip()
    if not ACCOUNT_RE.fullmatch(account):
        raise StoreError("sts get-caller-identity", 0, "UnexpectedOutput")
    return account


class Log:
    """Prints `pi_deploy: <line>` to stdout (journald) and appends it with a UTC time to
    `deploy.log`. Every value in `redact` (account, bucket) is replaced before writing."""

    def __init__(self, log_file=None):
        self._file = log_file
        self.redact = []

    def __call__(self, message: str) -> None:
        for value in self.redact:
            if value:
                message = message.replace(value, "<redacted>")
        print(f"pi_deploy: {message}", flush=True)
        if self._file is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(self._file, "a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {message}\n")


def _remove(path: Path) -> None:
    """Delete a file, symlink or folder without following symlinks."""
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _chown_to_user(runner, path: Path, recursive: bool) -> None:
    argv = ["chown", *(["-R", "-h"] if recursive else []), f"{PI_USER}:{PI_USER}", str(path)]
    _run(runner, argv, "chown")


def ensure_dirs(paths: Paths, runner) -> None:
    for d in (paths.releases_dir, paths.log_dir):
        if not d.is_dir():
            d.mkdir(parents=True, mode=0o755)
            _chown_to_user(runner, d, recursive=False)


def clean_leftovers(paths: Paths, log) -> None:
    """Remove `.tmp-*` in the releases folder and `<APP_LINK>.tmp-*` symlinks (interrupted runs)."""
    for entry in sorted(paths.releases_dir.glob(TMP_PREFIX + "*")):
        log(f"removing leftover {entry.name}")
        _remove(entry)
    for entry in sorted(paths.app_link.parent.glob(paths.app_link.name + TMP_PREFIX + "*")):
        if entry.is_symlink():
            log(f"removing leftover {entry.name}")
            entry.unlink()


def active_release(paths: Paths):
    """The sha APP_LINK points at (a symlink to RELEASES_DIR/<40 hex>), else None."""
    if not paths.app_link.is_symlink():
        return None
    target = Path(os.path.realpath(paths.app_link))
    if target.parent != Path(os.path.realpath(paths.releases_dir)) or not is_sha(target.name):
        return None
    return target.name


def read_marker(folder: Path):
    marker = folder / RELEASE_MARKER
    if marker.is_symlink() or not marker.is_file():
        return None
    return marker.read_text(encoding="ascii", errors="replace").strip()


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.isreg():
        return "file"
    if member.isdir():
        return "dir"
    if member.issym():
        return "symlink"
    return "other"


def safe_extract(archive_path, dest: Path) -> None:
    """Unpack a .tar.gz into the empty folder `dest` after check_members() accepts the whole
    listing. Never follows a symlink while writing; files get 0755 or 0644."""
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            members = tar.getmembers()
            plan = check_members((m.name, _member_kind(m), m.linkname, m.size) for m in members)
            by_path = {safe_member_path(m.name): m for m in members}
            for path, kind, linkname in plan:
                target = dest / path
                if kind == "dir":
                    target.mkdir(parents=True, exist_ok=True, mode=0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                if kind == "symlink":
                    os.symlink(linkname, target)
                    continue
                member = by_path[path]
                source = tar.extractfile(member)
                if source is None:
                    raise UnsafeArchiveError("unreadable file member")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                mode = 0o755 if member.mode & 0o111 else 0o644
                with source, os.fdopen(os.open(target, flags, mode), "wb") as out:
                    shutil.copyfileobj(source, out)
                os.chmod(target, mode)
    except (tarfile.TarError, EOFError, OSError) as exc:
        # Truncated or corrupt gzip data surfaces as OSError subclasses (BadGzipFile) or EOFError.
        raise DeployError(REASON_UNSAFE_ARCHIVE, f"not a valid .tar.gz ({_describe(exc)})") from None


def validate_release(folder: Path, runner) -> None:
    """Required files present as regular files (not symlinks) and `bash -n run_pipeline.sh`."""
    for rel in RELEASE_REQUIRED_FILES:
        try:
            mode = os.lstat(folder / rel).st_mode
        except FileNotFoundError:
            mode = 0
        if not stat.S_ISREG(mode):
            raise DeployError(REASON_MISSING_FILE, f"missing or not a regular file: {rel}")
    try:
        proc = runner(["/bin/bash", "-n", str(folder / "run_pipeline.sh")], timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        raise DeployError(REASON_BASH_SYNTAX, "bash -n could not run") from None
    if proc.returncode != 0:
        text = (proc.stderr or "").replace(str(folder) + "/", "")  # the temp folder is noise
        raise DeployError(REASON_BASH_SYNTAX, "bash -n: " + " ".join(text.split())[:300])
    check_deployer_runs(folder, runner)


def check_deployer_runs(folder: Path, runner) -> None:
    """The release's pi_deploy.py must start under the system python3 before anything switches:
    once synced to /opt it runs on every boot, and an import that python3 lacks (e.g. 3.9) would
    break every later deploy. It runs from a temporary copy holding only pi_deploy.py and
    pi_boot_guard.py, as in /opt. `-E -s -B` rather than `-I`: -I would also drop the script's
    own folder from sys.path, where pi_boot_guard is imported from."""
    with tempfile.TemporaryDirectory(prefix="pi-deploy-check-") as scratch:
        for name in ("pi_deploy.py", "pi_boot_guard.py"):
            shutil.copyfile(folder / "infra" / "instance" / name, Path(scratch) / name)
        argv = [SYSTEM_PYTHON, "-E", "-s", "-B", str(Path(scratch) / "pi_deploy.py"), "--help"]
        try:
            proc = runner(argv, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            raise DeployError(REASON_DEPLOYER, "pi_deploy.py --help could not run") from None
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        raise DeployError(REASON_DEPLOYER, f"pi_deploy.py --help exit {proc.returncode}: "
                          + (lines[-1][:200] if lines else "no output"))


def install_release(sha: str, store, paths: Paths, runner, log) -> Path:
    """RELEASES_DIR/<sha> ready to switch to (reused if its `.release` matches). APP_LINK is not
    touched."""
    final = paths.release(sha)
    if final.is_dir() and not final.is_symlink() and read_marker(final) == sha:
        log(f"release {sha[:12]} already unpacked: reusing it")
        validate_release(final, runner)
        return final
    tmp = paths.releases_dir / f"{TMP_PREFIX}{sha}-{os.getpid()}"
    tmp.mkdir(mode=0o755)
    try:
        with tempfile.TemporaryDirectory(prefix="pi-deploy-") as scratch:
            archive = Path(scratch) / f"{sha}.tar.gz"
            checksum = Path(scratch) / f"{sha}.tar.gz.sha256"
            if not store.get(f"{RELEASES_PREFIX}{sha}.tar.gz", archive):
                raise DeployError(REASON_ARCHIVE_MISSING, f"releases/{sha}.tar.gz not found")
            if not store.get(f"{RELEASES_PREFIX}{sha}.tar.gz.sha256", checksum):
                raise DeployError(REASON_ARCHIVE_MISSING, f"releases/{sha}.tar.gz.sha256 not found")
            expected = parse_sha256_file(checksum.read_bytes(), f"{sha}.tar.gz")
            if file_sha256(archive) != expected:
                raise DeployError(REASON_CHECKSUM, f"sha256 of releases/{sha}.tar.gz does not match")
            safe_extract(archive, tmp)
        validate_release(tmp, runner)
        (tmp / RELEASE_MARKER).write_text(sha + "\n", encoding="ascii")
        _chown_to_user(runner, tmp, recursive=True)
        if final.exists() or final.is_symlink():
            # Not a valid copy of this release (interrupted or altered): move it aside first so
            # the name is never missing if APP_LINK happens to point at it.
            aside = paths.releases_dir / f"{TMP_PREFIX}replaced-{sha}-{os.getpid()}"
            os.rename(final, aside)
            os.rename(tmp, final)
            _remove(aside)
        else:
            os.rename(tmp, final)
    except BaseException:
        if tmp.exists():
            _remove(tmp)
        raise
    log(f"release {sha[:12]} unpacked and checked")
    return final


def check_environment(new_release: Path, paths: Paths, allow_env_update: bool, runner, log) -> None:
    """Stop if environment.yml changed, unless --allow-env-update (then update wayakit_env)."""
    old = paths.app_link / "environment.yml"
    new = new_release / "environment.yml"
    if not old.is_file() or old.read_bytes() == new.read_bytes():
        return
    if not allow_env_update:
        raise DeployError(REASON_ENV_CHANGED, "environment.yml differs from the active release")
    log(f"environment.yml changed: updating conda env {CONDA_ENV} (--allow-env-update)")
    try:
        proc = runner(["runuser", "-u", PI_USER, "--", CONDA_BIN, "env", "update",
                       "-n", CONDA_ENV, "-f", str(new)], timeout=None)
    except OSError as exc:
        raise DeployError(REASON_CONDA, f"conda could not run ({exc.strerror})") from None
    if proc.returncode != 0:
        tail = " | ".join((proc.stderr or proc.stdout or "").strip().splitlines()[-5:])
        raise DeployError(REASON_CONDA, f"conda env update exit {proc.returncode}: {tail[:500]}")
    log(f"conda env {CONDA_ENV} updated")


def pipeline_is_running(runner) -> bool:
    """`systemctl is-active --quiet pi-pipeline.service`: exit 0 = running."""
    try:
        proc = runner(["systemctl", "is-active", "--quiet", PIPELINE_SERVICE], timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        raise DeployError(REASON_COMMAND, "systemctl is-active could not run") from None
    return proc.returncode == 0


def switch_to(new_release: Path, paths: Paths, log) -> None:
    """Point APP_LINK at `new_release` with one rename(2). A legacy folder is moved to the
    `.pre-f4` backup first and moved back if the rename fails."""
    link = paths.app_link
    legacy = link.is_dir() and not link.is_symlink()
    if legacy and (paths.legacy_backup.exists() or paths.legacy_backup.is_symlink()):
        raise DeployError(REASON_LEGACY_BACKUP_EXISTS,
                          f"{paths.legacy_backup.name} exists and {link.name} is still a folder")
    if link.exists() and not link.is_symlink() and not link.is_dir():
        raise DeployError(REASON_SWITCH, f"{link.name} is a file")
    tmp_link = link.with_name(f"{link.name}{TMP_PREFIX}{os.getpid()}")
    try:
        os.symlink(str(new_release), tmp_link)
        if legacy:
            os.rename(link, paths.legacy_backup)
            log(f"legacy folder moved to {paths.legacy_backup}")
        try:
            os.replace(tmp_link, link)
        except OSError:
            if legacy:
                os.rename(paths.legacy_backup, link)
                log(f"switch failed: legacy folder moved back to {link}")
            raise
    except OSError as exc:
        if tmp_link.is_symlink():
            tmp_link.unlink()
        raise DeployError(REASON_SWITCH, f"switch failed ({exc.strerror})") from None
    log(f"{link} -> {new_release}")


def sync_system_files(release: Path, paths: Paths, runner, log, dry_run: bool = False) -> list:
    """Copy the unit files and the deployer from `release` when they differ. Python files are
    compiled first. Returns the destinations that changed (or would change)."""
    changed, unit_changed = [], False
    for rel, attr in SYSTEM_FILES:
        src = release / rel
        dst = getattr(paths, attr) / Path(rel).name
        data = src.read_bytes()
        if dst.is_file() and not dst.is_symlink() and dst.read_bytes() == data:
            continue
        if src.suffix == ".py":
            try:
                compile(data, str(src), "exec")
            except (SyntaxError, ValueError) as exc:
                raise DeployError(REASON_SYSTEM_FILES, f"{rel} does not compile ({type(exc).__name__})") from None
        changed.append(dst)
        if dry_run:
            log(f"[dry-run] would update {dst}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f"{dst.name}{TMP_PREFIX}{os.getpid()}")
        try:
            tmp.write_bytes(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, dst)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise DeployError(REASON_SYSTEM_FILES, f"writing {dst} failed ({exc.strerror})") from None
        log(f"updated {dst}")
        unit_changed = unit_changed or attr == "systemd_dir"
    if unit_changed:
        try:
            _run(runner, ["systemctl", "daemon-reload"], "systemctl daemon-reload", timeout=120)
        except DeployError as exc:
            raise DeployError(REASON_SYSTEM_FILES, exc.detail) from None
    return changed


def prune(paths: Paths, current: str, previous, log) -> list:
    """Delete the releases releases_to_prune() names. Raises DeployError(REASON_PRUNE)."""
    mtimes = {}
    try:
        for entry in paths.releases_dir.iterdir():
            if is_sha(entry.name) and entry.is_dir() and not entry.is_symlink():
                marker = entry / RELEASE_MARKER
                mtimes[entry.name] = (marker if marker.is_file() else entry).stat().st_mtime
    except OSError as exc:
        raise DeployError(REASON_PRUNE, f"listing releases failed ({exc.strerror})") from None
    doomed = releases_to_prune(mtimes, current, previous)
    for name in doomed:
        try:
            _remove(paths.release(name))
        except OSError as exc:
            raise DeployError(REASON_PRUNE, f"removing {name[:12]} failed ({exc.strerror})") from None
        log(f"pruned release {name[:12]}")
    return doomed


def prune_best_effort(paths: Paths, current: str, previous, log, out) -> None:
    """A prune failure comes after a good switch: rolling back a working release for it would be
    worse. It is logged, kept in `out.prune_error` (main alerts and exits 1) and the result stays
    ok."""
    try:
        prune(paths, current, previous, log)
    except DeployError as exc:
        out.prune_error = exc.detail
        log(f"ERROR: {REASON_PRUNE}: {exc.detail} (release {current[:12]} stays active)")


ALERT_INSTALL_FAILED = "release install on the instance failed"
ALERT_PRUNE_FAILED = "old releases could not be pruned on the instance (the new release is active)"


def send_alert(runner, topic_arn: str, reason: str, sha, log,
               headline: str = ALERT_INSTALL_FAILED) -> bool:
    """Best effort: a failed publish is logged and does not change the exit code."""
    message = f"Price Intelligence: {headline}: {reason}"
    if sha:
        message += f" (release {sha[:12]})"
    argv = ["aws", "sns", "publish", "--topic-arn", topic_arn, "--subject",
            "Price Intelligence deploy failed", "--message", message, "--region", REGION,
            "--no-cli-pager", "--output", "json"]
    try:
        proc = runner(argv, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"WARNING: alert not sent (aws sns publish: {type(exc).__name__})")
        return False
    if proc.returncode != 0:
        code = aws_error_code(proc.stderr)
        log(f"WARNING: alert not sent (aws sns publish failed (exit {proc.returncode}"
            f"{', ' + code if code else ''}))")
        return False
    log("alert sent")
    return True


@dataclass
class Outcome:
    ok: bool = True
    reason: str = REASON_DEPLOYED
    sha: object = None
    previous: object = None
    deploy_mode: bool = False
    request: dict = field(default_factory=dict)
    prune_error: str = ""  # set by prune_best_effort; the result stays ok


def with_retries(fn, what: str, log, sleep=time.sleep, attempts: int = READ_ATTEMPTS,
                 delays=RETRY_DELAYS_SECONDS):
    """fn() retried on StoreError only (a missing key is not an error), waiting delays[i] after
    failed attempt i+1. The last StoreError is raised. Each retry is logged with the safe detail
    ("aws <op> failed (exit N, Code)") only."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except StoreError as exc:
            if attempt >= attempts:
                raise
            delay = delays[min(attempt - 1, len(delays) - 1)]
            log(f"{what}: {exc.detail}; attempt {attempt}/{attempts}, retrying in {delay} s")
            sleep(delay)


def _get_bytes(store, key: str, scratch: Path):
    dest = scratch / key.replace("/", "_")
    if not store.get(key, dest):
        return None
    return dest.read_bytes()


def deploy(args, *, store, now_utc: datetime, paths: Paths, runner, log, sleep=time.sleep) -> Outcome:
    """Steps 1-9 of the module docstring. Raises DeployError; `outcome` is filled as it goes
    (the caller reads it in its handler through the `outcome` attribute of the error)."""
    out = Outcome()
    try:
        _deploy(args, out, store=store, now_utc=now_utc, paths=paths, runner=runner, log=log,
                sleep=sleep)
    except DeployError as exc:
        exc.outcome = out
        raise
    except Exception as exc:  # noqa: BLE001 - becomes a failed result, never a silent exit 0
        err = DeployError(REASON_UNEXPECTED, _describe(exc))
        err.outcome = out
        raise err from None
    return out


def _describe(exc: BaseException) -> str:
    # Only the class and, for OS errors, errno text and the local path: an exception message can
    # carry a command line with the bucket name or the topic ARN.
    if isinstance(exc, OSError):
        name = f" {exc.filename}" if exc.filename else ""
        return f"{type(exc).__name__}: {exc.strerror or errno.errorcode.get(exc.errno, '')}{name}"
    return type(exc).__name__


def _deploy(args, out: Outcome, *, store, now_utc, paths, runner, log, sleep) -> None:
    dry = args.dry_run
    with tempfile.TemporaryDirectory(prefix="pi-deploy-") as scratch_dir:
        scratch = Path(scratch_dir)
        if args.sha:
            out.sha = args.sha
            log(f"manual install of release {args.sha[:12]} (--sha): deploy request ignored")
        else:
            data = with_retries(lambda: _get_bytes(store, DEPLOY_REQUEST_KEY, scratch),
                                "reading the deploy request", log, sleep)
            request = parse_request(data, now_utc.timestamp()) if data is not None else None
            if request is not None:
                result_key = f"{DEPLOYS_STATE_PREFIX}{request['request_id']}.json"
                try:
                    handled = with_retries(lambda: _get_bytes(store, result_key, scratch),
                                           "reading the deploy result", log, sleep) is not None
                except StoreError:
                    # A valid, open request: fail as a deploy boot (result attempt + power off)
                    # rather than leave the instance on.
                    out.deploy_mode, out.request = True, request
                    raise
                if handled:
                    log(f"deploy request {request['request_id']} already handled: normal boot")
                    request = None
            if request is not None:
                out.deploy_mode, out.request = True, request
                log(f"deploy mode: request {request['request_id']} for {request['sha'][:12]}")
            else:
                log("normal boot (no valid deploy request)")
            pointer = _get_bytes(store, RELEASES_CURRENT_KEY, scratch)
            if pointer is None:
                if out.deploy_mode:
                    out.sha = out.request["sha"]
                    raise DeployError(REASON_NO_POINTER, "releases/current not found")
                log("no release pointer yet: nothing to install")
                out.reason = REASON_NO_POINTER
                return
            out.sha = parse_pointer(pointer)
            if out.deploy_mode and out.request["sha"] != out.sha:
                raise DeployError(REASON_REQUEST_MISMATCH, "releases/current differs from the request")
    sha = out.sha
    out.previous = active_release(paths)
    if out.previous == sha and read_marker(paths.release(sha)) == sha:
        log(f"release {sha[:12]} is already active")
        out.reason = REASON_ALREADY_CURRENT
        sync_system_files(paths.release(sha), paths, runner, log, dry_run=dry)
        if not dry:
            prune_best_effort(paths, sha, None, log, out)
        return
    if dry:
        _dry_run_plan(sha, out.previous, paths, args, log)
        return
    if pipeline_is_running(runner):
        raise DeployError(REASON_PIPELINE_RUNNING, f"{PIPELINE_SERVICE} is active: not switching code under it")
    release = install_release(sha, store, paths, runner, log)
    check_environment(release, paths, args.allow_env_update, runner, log)
    switch_to(release, paths, log)
    sync_system_files(release, paths, runner, log)
    out.reason = REASON_DEPLOYED
    prune_best_effort(paths, sha, out.previous, log, out)
    log(f"release {sha[:12]} active (previous: {out.previous[:12] if out.previous else 'none'})")


def _dry_run_plan(sha, previous, paths: Paths, args, log) -> None:
    final = paths.release(sha)
    if read_marker(final) == sha:
        log(f"[dry-run] would reuse {final}")
        new_env = final / "environment.yml"
        old_env = paths.app_link / "environment.yml"
        if new_env.is_file() and old_env.is_file() and new_env.read_bytes() != old_env.read_bytes():
            log("[dry-run] environment.yml differs: "
                + ("would run conda env update" if args.allow_env_update else
                   "would stop (needs --allow-env-update)"))
    else:
        log(f"[dry-run] would download, check and unpack releases/{sha}.tar.gz into {final}")
    if paths.app_link.is_dir() and not paths.app_link.is_symlink():
        log(f"[dry-run] would move the legacy folder to {paths.legacy_backup}")
    log(f"[dry-run] would point {paths.app_link} at {final} "
        f"(active now: {previous[:12] if previous else 'none'})")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"pi_deploy: {message}", file=sys.stderr)
        raise SystemExit(EXIT_FAILED)


def parse_args(argv):
    parser = _Parser(description="Install the release named by the S3 pointer (pull-on-boot).")
    parser.add_argument("--dry-run", action="store_true", help="read only and print the plan")
    parser.add_argument("--allow-env-update", action="store_true",
                        help="if environment.yml changed, run conda env update before switching")
    parser.add_argument("--bucket", help="PI bucket (default price-intelligence-<account>)")
    parser.add_argument("--no-poweroff", action="store_true", help="never power off at the end")
    parser.add_argument("--sha", help="install this release instead of the pointer (never deploy mode)")
    args = parser.parse_args(argv)
    if args.sha is not None and not is_sha(args.sha):
        parser.error("--sha must be a 40-character lowercase hex commit SHA")
    if args.bucket is not None and not BUCKET_RE.fullmatch(args.bucket):
        parser.error("--bucket is not a valid bucket name")
    return args


def main(argv=None, *, store=None, now=None, paths=None, runner=None, environ=None,
         sleep=None) -> int:
    """Return the exit code (0 ok, 1 failed). Injection points for tests: `store` (get/put),
    `now` (timezone-aware datetime used for the request, the window and finished_at), `paths`
    (Paths), `runner` (argv, timeout=None -> CompletedProcess), `environ` (mapping read for
    PI_LOG_DIR and PI_ALERTS_TOPIC_ARN), `sleep` (seconds -> None; the waits between retries of
    STS and the deploy-request reads, see with_retries). Without `store`, the account comes from
    STS."""
    args = parse_args(argv)
    environ = os.environ if environ is None else environ
    runner = runner or run_command
    sleep = sleep or time.sleep
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("now must be timezone-aware")

    def clock():
        # A test injects one fixed time; on the instance the window and finished_at are read
        # when they are needed (the install can take minutes).
        return now if now is not None else datetime.now(timezone.utc)
    log = Log()
    try:
        paths = paths or Paths.from_environ(environ)
        if not args.dry_run:
            ensure_dirs(paths, runner)
            log = Log(paths.log_dir / LOG_FILE)
    except (DeployError, OSError) as exc:
        detail = exc.detail if isinstance(exc, DeployError) else _describe(exc)
        log(f"FAILED: cannot prepare folders: {detail}")
        return EXIT_FAILED
    log(f"start{' (dry-run)' if args.dry_run else ''}")
    lock_fh = None
    try:
        if not args.dry_run:
            lock_fh = open(paths.releases_dir / LOCK_FILE, "a")
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeployError(REASON_LOCKED, "another pi_deploy holds the lock") from None
            clean_leftovers(paths, log)
        if store is None:
            account = with_retries(lambda: account_id(runner), "sts get-caller-identity", log, sleep)
            bucket = args.bucket or f"{BUCKET_PREFIX}{account}"
            log.redact = [account, bucket]
            store = AwsCliStore(bucket, account, runner)
        outcome = deploy(args, store=store, now_utc=clock(), paths=paths, runner=runner, log=log,
                         sleep=sleep)
        ok, error = True, None
    except DeployError as exc:
        outcome = getattr(exc, "outcome", None) or Outcome()
        outcome.ok, outcome.reason = False, exc.reason
        ok, error = False, exc
    except Exception as exc:  # noqa: BLE001 - lock, cleanup: a failure, never a silent exit 0
        outcome = Outcome(ok=False, reason=REASON_UNEXPECTED)
        ok, error = False, DeployError(REASON_UNEXPECTED, _describe(exc))
    finally:
        if lock_fh is not None:
            lock_fh.close()
    exit_code = EXIT_OK if ok else EXIT_FAILED
    if not ok:
        log(f"FAILED: {error.reason}{': ' + error.detail if error.detail else ''}")
    if args.dry_run:
        log(f"dry-run done ({'ok' if ok else 'failed'}); nothing changed")
        return exit_code
    if outcome.deploy_mode:
        # The request's sha, not the pointer's: release.py matches the result on it.
        result = result_object(outcome.request["request_id"], outcome.request["sha"], ok,
                               outcome.reason, int(clock().timestamp()), outcome.previous)
        try:
            with tempfile.TemporaryDirectory(prefix="pi-deploy-") as scratch:
                path = Path(scratch) / "result.json"
                path.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
                store.put(f"{DEPLOYS_STATE_PREFIX}{outcome.request['request_id']}.json", path)
            log(f"result written: {result['result']} ({result['reason']})")
        except DeployError as exc:
            log(f"FAILED: result not written: {exc.detail}")
            exit_code = EXIT_FAILED
    if ok and outcome.prune_error:
        exit_code = EXIT_FAILED  # loud (unit failed, alert) but the release and result stay ok
    if not ok or outcome.prune_error:
        topic = (environ.get(ENV_ALERTS_TOPIC_ARN) or "").strip()
        if topic:
            log.redact.append(topic)
            if ok:
                send_alert(runner, topic, REASON_PRUNE, outcome.sha, log, headline=ALERT_PRUNE_FAILED)
            else:
                send_alert(runner, topic, outcome.reason, outcome.sha, log)
        else:
            log(f"no alert: {ENV_ALERTS_TOPIC_ARN} is not set")
    if outcome.deploy_mode:
        if should_power_off(True, clock(), args.no_poweroff):
            log("deploy boot finished: powering off")
            try:
                _run(runner, ["systemctl", "poweroff", "--no-block"], "systemctl poweroff")
            except DeployError as exc:
                log(f"FAILED: {exc.detail}")
                exit_code = EXIT_FAILED
        elif args.no_poweroff:
            log("deploy boot finished: staying on (--no-poweroff)")
        else:
            log("deploy boot inside the monthly window: staying on for the pipeline")
    log(f"done: {'ok' if exit_code == EXIT_OK else 'failed'}")
    return exit_code


def _cli() -> int:
    try:
        return main()
    except SystemExit as exc:
        if exc.code in (0, None):  # --help
            return EXIT_OK
        return EXIT_FAILED
    except BaseException as exc:  # noqa: BLE001 - any error must exit 1, never 0
        print(f"pi_deploy: FAILED: {_describe(exc)}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(_cli())
