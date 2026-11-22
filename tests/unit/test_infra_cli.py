"""Unit tests of the shared CLI entry point of the infra scripts, `infra._aws.cli_main` (alias
`run_cli`), and of its use in every script (F3 review, Important 1; F4: `release.py`, which has a
subcommand, has its own cases).

Contract (pi-dev, 2026-11-18):
- prints `Profile: <config.profile()>` first (AWS_PROFILE, else wayakit-vdc), then calls
  `main(argv, **kwargs)` and always ends in SystemExit: an int return (not a bool) is the code,
  anything else is 0;
- botocore ClientError: stderr is exactly `AWS error <code> in <operation>` (`Unknown` / `AWS call`
  when missing), code 1; the AWS message (account IDs, ARNs, SSO email) is never printed;
- BotoCoreError: stderr is `AWS client error: <ClassName>`, plus the line
  `If the SSO session expired: aws sso login --profile <profile>` for login errors (matched on the
  class or any parent), code 1;
- SystemExit raised by `main` and any other exception propagate unchanged.

Per script, a ClientError whose message carries a fake account (123456789012) and a fake SSO email
(someone@example.com) is raised by a real botocore client (fake credentials, eu-north-1) whose
calls are all answered by a `before-call` hook, either on the first call (STS) or on the first
call after it. Nothing reaches the network. The `python -m infra.<script>` runs use an AWS
profile that does not exist with `AWS_CONFIG_FILE=/dev/null`: botocore raises ProfileNotFound
before any request.
"""
from __future__ import annotations

import ast
import datetime as dt
import gzip
import io
import os
import subprocess
import sys
import tarfile

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import (BotoCoreError, ClientError, EndpointConnectionError, NoCredentialsError,
                                 ProfileNotFound, SSOTokenLoadError, TokenRetrievalError,
                                 UnauthorizedSSOTokenError)

from infra import (_aws, alerts, bedrock_profiles, bucket, check_pi, config, deploy_role, ec2_instance,
                   instance_role, release, schedule, scheduler_role)
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.unit

ACCOUNT = "000000000000"           # what the fake STS answers
LEAKED_ACCOUNT = "123456789012"    # only inside AWS error messages: must never be printed
LEAKED_EMAIL = "someone@example.com"
LEAK = (f"User: arn:aws:sts::{LEAKED_ACCOUNT}:assumed-role/AWSReservedSSO_Admin_0123abcd/{LEAKED_EMAIL} "
        f"is not authorized to perform: iam:GetRole on resource: arn:aws:iam::{LEAKED_ACCOUNT}:role/x")
LEAK_PARTS = (LEAKED_ACCOUNT, LEAKED_EMAIL, "someone", "AWSReservedSSO", "not authorized", "arn:aws:sts")
PROFILE = "pi-test-profile"
HINT = f"If the SSO session expired: aws sso login --profile {PROFILE}"

SCRIPTS = (alerts, bedrock_profiles, bucket, check_pi, deploy_role, ec2_instance, instance_role, schedule,
           scheduler_role)
# The four scripts of Important 1 have no local handler: only cli_main sanitizes their errors.
NO_LOCAL_HANDLER = (deploy_role, ec2_instance, instance_role, scheduler_role)
# F4: release.py has a subcommand and required arguments, so it is not in the generic
# `--dry-run` / apply cases above; it has its own cases below (same guarantees).
SUBCOMMAND_SCRIPTS = (release,)


def _name(module):
    return module.__name__.rsplit(".", 1)[1]


# Every mode of every script (dry-run and apply; apply is safe: every call is answered by the hook).
CASES = [(m, argv) for m in SCRIPTS for argv in (["--dry-run"], [])] + [
    (instance_role, ["--audit-old-role"]),
    (alerts, ["--email", LEAKED_EMAIL]),
    (ec2_instance, ["--rollback-profile", "some-profile", "--dry-run"]),
    (ec2_instance, ["--create", "--dry-run"]),
    (schedule, ["--enable", "--dry-run"]),
    (check_pi, ["--only", "iam"]),
]
CASE_IDS = [f"{_name(m)} {' '.join(a) or '(apply)'}" for m, a in CASES]


class HookedSession:
    """Session double: real botocore clients built with fake credentials whose every call is
    answered by a `before-call` hook (no request is ever signed or sent). STS answers with the
    fake account unless `fail_sts`; every other call fails with `error` ("client": a 403
    AccessDenied ClientError carrying LEAK; "token": a TokenRetrievalError carrying LEAK)."""

    def __init__(self, *, fail_sts, error="client"):
        self.fail_sts, self.error = fail_sts, error
        self.ops, self.failed = [], []

    def client(self, service, **kwargs):
        # The test sets AWS_PROFILE to a profile that does not exist (it is what the profile line
        # prints); botocore would look it up while building the client, so it is hidden meanwhile.
        saved = os.environ.pop("AWS_PROFILE", None)
        try:
            c = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                              region_name=config.REGION).client(service, **kwargs)
        finally:
            if saved is not None:
                os.environ["AWS_PROFILE"] = saved
        c.meta.events.register("before-call.*.*", self._answer)
        return c

    def _answer(self, model, **_):
        self.ops.append(model.name)
        if model.service_model.service_name == "sts" and not self.fail_sts:
            return AWSResponse("https://sts.invalid", 200, {}, None), {
                "Account": ACCOUNT, "UserId": "AIDAFAKEFAKEFAKE", "Arn": f"arn:aws:iam::{ACCOUNT}:user/fake",
                "ResponseMetadata": {"HTTPStatusCode": 200}}
        self.failed.append(model.name)
        if self.error == "token":
            raise TokenRetrievalError(provider="sso", error_msg=LEAK)
        return AWSResponse("https://aws.invalid", 403, {}, None), {
            "Error": {"Code": "AccessDenied", "Message": LEAK}, "ResponseMetadata": {"HTTPStatusCode": 403}}


def _cli(module, argv, session, capsys):
    """Exit status, stdout and stderr as the shell would see them: a SystemExit with a message (a
    script's own, e.g. bucket on HeadBucket 403) prints it to stderr and exits 1."""
    with pytest.raises(SystemExit) as exc:
        _aws.cli_main(module.main, argv, session=session)
    out, err = capsys.readouterr()
    code = exc.value.code
    if isinstance(code, str):
        return 1, out, err + code + "\n", True
    return code, out, err, False


def _assert_no_leak(text):
    for part in LEAK_PARTS:
        assert part not in text, (part, text)
    assert "Traceback" not in text, text


@pytest.fixture(autouse=True)
def _profile(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", PROFILE)


# --- Per script: ClientError and BotoCoreError through the CLI entry point -----------------------
@pytest.mark.parametrize("fail_sts", [True, False], ids=["on-sts", "after-sts"])
@pytest.mark.parametrize("module, argv", CASES, ids=CASE_IDS)
def test_client_error_through_the_cli_never_prints_the_aws_message(module, argv, fail_sts, capsys):
    s = HookedSession(fail_sts=fail_sts)
    code, out, err, own_message = _cli(module, argv, s, capsys)
    assert out.splitlines()[0] == f"Profile: {PROFILE}"
    assert code == 1, out + err
    assert s.failed, "precondition: an AWS call failed"
    _assert_no_leak(out + err)
    if own_message:  # bucket: a 403 on HeadBucket is "exists but not accessible", by design
        assert (module, s.failed) == (bucket, ["HeadBucket"]), err
        return
    assert "AccessDenied" in out + err
    failed_op = s.failed[0]
    if fail_sts:
        assert failed_op == "GetCallerIdentity"
    if module is not check_pi or fail_sts:
        # check_pi turns a failure inside a section into a FAIL line with the code only.
        assert failed_op in out + err, (failed_op, out + err)
    if module in NO_LOCAL_HANDLER or fail_sts and module not in (alerts, bedrock_profiles):
        assert err == f"AWS error AccessDenied in {failed_op}\n"


@pytest.mark.parametrize("fail_sts", [True, False], ids=["on-sts", "after-sts"])
@pytest.mark.parametrize("module, argv", CASES, ids=CASE_IDS)
def test_botocore_error_through_the_cli_prints_only_the_class_name(module, argv, fail_sts, capsys):
    s = HookedSession(fail_sts=fail_sts, error="token")
    code, out, err, _ = _cli(module, argv, s, capsys)
    assert out.splitlines()[0] == f"Profile: {PROFILE}"
    assert code == 1, out + err
    _assert_no_leak(out + err)
    assert "TokenRetrievalError" in out + err
    if "AWS client error: TokenRetrievalError" in err:  # handled by cli_main itself
        assert err == f"AWS client error: TokenRetrievalError\n{HINT}\n"
    if module in NO_LOCAL_HANDLER:
        assert err == f"AWS client error: TokenRetrievalError\n{HINT}\n"


@pytest.mark.parametrize("module", SCRIPTS, ids=_name)
def test_profile_line_is_the_first_line_and_defaults_to_wayakit_vdc(module, capsys, monkeypatch):
    monkeypatch.delenv("AWS_PROFILE")
    code, out, _, _ = _cli(module, ["--dry-run"], HookedSession(fail_sts=True), capsys)
    assert code == 1
    assert out.splitlines()[0] == "Profile: wayakit-vdc"
    assert ACCOUNT not in out


def test_every_infra_script_main_block_is_exactly_cli_main():
    """Every `infra/*.py` with a `__main__` block runs `_aws.cli_main(main)` and nothing else."""
    found = []
    for path in sorted((REPO_ROOT / "infra").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'":
                found.append(path.stem)
                assert [ast.unparse(n) for n in node.body] == ["_aws.cli_main(main)"], path.name
    assert sorted(found) == sorted(_name(m) for m in SCRIPTS + SUBCOMMAND_SCRIPTS)


@pytest.mark.parametrize("module", SCRIPTS, ids=_name)
def test_python_dash_m_with_a_missing_profile_exits_1_without_a_traceback(module):
    """The real entry point, offline: the profile does not exist, so botocore raises
    ProfileNotFound before any request."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
               AWS_EC2_METADATA_DISABLED="true", AWS_PROFILE="pi-test-missing-profile",
               HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
    r = subprocess.run([sys.executable, "-m", f"infra.{_name(module)}", "--dry-run"], cwd=REPO_ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.splitlines()[0] == "Profile: pi-test-missing-profile"
    assert "ProfileNotFound" in r.stdout + r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    if module not in (alerts, bedrock_profiles):  # those two report it with their own line
        assert r.stderr == ("AWS client error: ProfileNotFound\n"
                            "If the SSO session expired: aws sso login --profile pi-test-missing-profile\n")


# --- release.py (F4) through the CLI entry point ---------------------------------------------------
RELEASE_NOW = dt.datetime(2026, 11, 10, 15, 0, tzinfo=dt.timezone.utc)  # far from the monthly window


def _release_argv(tmp_path, *extra):
    """A valid archive (the local checks pass) so the run reaches AWS."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name in config.RELEASE_REQUIRED_FILES:
            data = b"#!/bin/bash\n" if name.endswith(".sh") else b"# x\n"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    archive = tmp_path / "release.tar.gz"
    archive.write_bytes(gzip.compress(raw.getvalue(), mtime=0))
    return ["deploy", "--sha", "a" * 40, "--archive", str(archive), "--request-id", "1-1", *extra]


@pytest.mark.parametrize("error", ["client", "token"])
@pytest.mark.parametrize("fail_sts", [True, False], ids=["on-sts", "after-sts"])
@pytest.mark.parametrize("extra", [["--dry-run"], [], ["--env-credentials"]], ids=["dry-run", "apply", "env"])
def test_release_errors_through_the_cli_never_print_the_aws_message(tmp_path, capsys, error, fail_sts, extra):
    s = HookedSession(fail_sts=fail_sts, error=error)
    with pytest.raises(SystemExit) as exc:
        _aws.cli_main(release.main, _release_argv(tmp_path, *extra), session=s, now=RELEASE_NOW,
                      sleep=lambda _: None)
    out, err = capsys.readouterr()
    assert exc.value.code == 1, out + err
    expected_profile = "environment credentials" if "--env-credentials" in extra else PROFILE
    assert out.splitlines()[0] == f"Profile: {expected_profile}"
    assert s.failed == ["GetCallerIdentity" if fail_sts else "DescribeInstances"]
    _assert_no_leak(out + err)
    if error == "client":
        assert err == f"AWS error AccessDenied in {s.failed[0]}\n"
    else:
        assert err.startswith("AWS client error: TokenRetrievalError\n")
    assert "PutObject" not in s.ops and "StartInstances" not in s.ops


def test_release_python_dash_m_with_a_missing_profile_exits_1_without_a_traceback():
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
               AWS_EC2_METADATA_DISABLED="true", AWS_PROFILE="pi-test-missing-profile",
               HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
    argv = ["deploy", "--sha", "a" * 40, "--archive", "/nonexistent/release.tar.gz", "--request-id", "1-1",
            "--dry-run"]
    r = subprocess.run([sys.executable, "-m", "infra.release", *argv], cwd=REPO_ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.splitlines()[0] == "Profile: pi-test-missing-profile"
    assert "Traceback" not in r.stderr, r.stderr
    assert r.stderr == ("AWS client error: ProfileNotFound\n"
                        "If the SSO session expired: aws sso login --profile pi-test-missing-profile\n")


def test_release_python_dash_m_refuses_the_default_profile_with_env_credentials():
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
               AWS_EC2_METADATA_DISABLED="true", AWS_PROFILE="default",
               HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
    argv = ["deploy", "--sha", "a" * 40, "--archive", "/nonexistent/release.tar.gz", "--request-id", "1-1",
            "--env-credentials"]
    r = subprocess.run([sys.executable, "-m", "infra.release", *argv], cwd=REPO_ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.splitlines()[0] == "Profile: environment credentials"
    assert "Refusing the AWS profile 'default'" in r.stderr and "Traceback" not in r.stderr


# --- cli_main itself -----------------------------------------------------------------------------
def _client_error(code="AccessDenied", op="GetRole", message=LEAK):
    error = {"Message": message}
    if code is not None:
        error["Code"] = code
    return ClientError({"Error": error}, op)


def _raising(exc):
    def main(argv=None, session=None):
        raise exc
    return main


def test_run_cli_is_the_same_function():
    assert _aws.run_cli is _aws.cli_main


def test_profile_line():
    assert _aws.profile_line() == f"Profile: {PROFILE}"


@pytest.mark.parametrize("value, code", [(0, 0), (1, 1), (3, 3), (None, 0), ({"state": "DISABLED"}, 0),
                                         (True, 0), (False, 0), ("1", 0)])
def test_return_value_becomes_the_exit_code(value, code, capsys):
    with pytest.raises(SystemExit) as exc:
        _aws.cli_main(lambda argv=None: value)
    assert exc.value.code == code
    assert capsys.readouterr().out == f"Profile: {PROFILE}\n"


def test_argv_and_kwargs_are_passed_through(capsys):
    seen = []

    def main(argv=None, session=None):
        seen.append((argv, session))
        return 0

    sentinel = object()
    with pytest.raises(SystemExit):
        _aws.cli_main(main, ["--dry-run"], session=sentinel)
    assert seen == [(["--dry-run"], sentinel)]


def test_profile_line_is_printed_before_main_runs(capsys):
    def main(argv=None):
        print("from main")
        return 0

    with pytest.raises(SystemExit):
        _aws.cli_main(main)
    assert capsys.readouterr().out == f"Profile: {PROFILE}\nfrom main\n"


@pytest.mark.parametrize("exc, line", [
    (_client_error(), "AWS error AccessDenied in GetRole"),
    (_client_error(code=None), "AWS error Unknown in GetRole"),
    (_client_error(op=None), "AWS error AccessDenied in AWS call"),
    (_client_error("ValidationException", "UpdateSchedule", f"bad {LEAKED_ACCOUNT}"),
     "AWS error ValidationException in UpdateSchedule"),
], ids=["access-denied", "no-code", "no-operation", "validation"])
def test_client_error_prints_one_sanitized_line(exc, line, capsys):
    with pytest.raises(SystemExit) as raised:
        _aws.cli_main(_raising(exc))
    out, err = capsys.readouterr()
    assert raised.value.code == 1
    assert out == f"Profile: {PROFILE}\n"
    assert err == line + "\n"
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


class _MyCredentialsError(NoCredentialsError):
    """A subclass of a login error: matched through the parent classes."""


@pytest.mark.parametrize("exc, hint", [
    (TokenRetrievalError(provider="sso", error_msg=LEAK), True),
    (NoCredentialsError(), True),
    (SSOTokenLoadError(error_msg=LEAK), True),
    (UnauthorizedSSOTokenError(), True),
    (ProfileNotFound(profile=LEAKED_EMAIL), True),
    (_MyCredentialsError(), True),
    (EndpointConnectionError(endpoint_url=f"https://{LEAKED_ACCOUNT}.example.invalid"), False),
    (BotoCoreError(), False),
], ids=lambda v: type(v).__name__ if isinstance(v, Exception) else str(v))
def test_botocore_error_prints_only_the_class_name(exc, hint, capsys):
    with pytest.raises(SystemExit) as raised:
        _aws.cli_main(_raising(exc))
    out, err = capsys.readouterr()
    assert raised.value.code == 1
    assert out == f"Profile: {PROFILE}\n"
    expected = f"AWS client error: {type(exc).__name__}\n" + (f"{HINT}\n" if hint else "")
    assert err == expected
    _assert_no_leak(out + err)


@pytest.mark.parametrize("exc", [SystemExit("Role not found: run scheduler_role first."), SystemExit(2),
                                 ValueError("not an AWS error"), KeyboardInterrupt()],
                         ids=["system-exit-message", "system-exit-code", "value-error", "keyboard"])
def test_other_exceptions_propagate_unchanged(exc, capsys):
    with pytest.raises(type(exc)) as raised:
        _aws.cli_main(_raising(exc))
    assert raised.value is exc
    out, err = capsys.readouterr()
    assert out == f"Profile: {PROFILE}\n" and err == ""


def test_profile_default_is_refused_after_the_profile_line(capsys, monkeypatch):
    """No boto3 session is built for the profile `default`: get_session raises SystemExit."""
    monkeypatch.setenv("AWS_PROFILE", "default")
    with pytest.raises(SystemExit) as raised:
        _aws.cli_main(instance_role.main, ["--dry-run"])
    assert "Refusing the AWS profile 'default'" in str(raised.value.code)
    assert capsys.readouterr().out == "Profile: default\n"


def test_schedule_cli_keeps_the_old_name(capsys, monkeypatch):
    monkeypatch.setattr(schedule, "main", _raising(_client_error("AccessDenied", "GetSchedule")))
    with pytest.raises(SystemExit) as raised:
        schedule._cli()
    assert raised.value.code == 1
    assert capsys.readouterr().err == "AWS error AccessDenied in GetSchedule\n"
