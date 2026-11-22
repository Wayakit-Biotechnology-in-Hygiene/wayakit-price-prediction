"""Unit tests of `infra/release.py` (F4: publish a release and wait for the deploy-only boot).

No test reaches AWS. `botocore.stub.Stubber` sits on real STS, EC2 and S3 clients built with fake
credentials in eu-north-1: every call must be queued in order with its exact parameters (bodies,
content types, `ExpectedBucketOwner`), and botocore validates requests and responses against the
service models. A recorder keeps every operation in global order across the three clients, so
the tests assert the whole sequence (and that nothing was written on a refusal or in dry-run).
The clock (`now`) and `sleep` are injected: nothing waits.

What is covered (F4 spec, "release.py" and "Tests"):
- happy path order: STS, find the instance, check it is stopped, read `current`, put the archive,
  its `.sha256`, `previous`, `current`, the request, `StartInstances`, poll, close the request;
- rollback = deploy of an older sha through the same path;
- first deploy without a pointer (no `previous`, nothing to restore);
- failed result, a result for another sha, a timeout and an AWS error after `current` moved:
  `current` is put back to its old value and the request is closed; exit 1;
- refusals before any write: instance not stopped, inside (or 30 minutes before) the monthly
  window, malformed pointer, bad arguments, archive without the required files;
- dry-run only reads;
- `ExpectedBucketOwner` on every S3 call;
- output never shows the account, the bucket name or the instance ID.

The fake account is `000000000000`; the instance ID is the AWS documentation example.
"""
from __future__ import annotations

import copy
import datetime as dt
import gzip
import hashlib
import io
import json
import re
import tarfile

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from infra import _aws, config, release
from infra.instance import pi_boot_guard as guard

pytestmark = pytest.mark.unit

ACCOUNT = "000000000000"
IID = "i-0123456789abcdef0"
BUCKET = config.bucket_name(ACCOUNT)
SHA = "a" * 40
OLD = "b" * 40
OLDER = "c" * 40
RID = "1234567890-1"
UTC = dt.timezone.utc
# Tuesday 2026-11-10 15:00 UTC: far from the monthly window (last Thursday, 2026-11-26 21:40-23:00
# America/Mexico_City = 2026-11-27 03:40-05:00 UTC).
NOW = dt.datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
WINDOW_START_UTC = dt.datetime(2026, 11, 27, 3, 40, tzinfo=UTC)
WINDOW_END_UTC = dt.datetime(2026, 11, 27, 5, 0, tzinfo=UTC)
FIND_FILTERS = [{"Name": "tag:Name", "Values": [config.INSTANCE_NAME]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
STATE_CODES = {"pending": 0, "running": 16, "stopping": 64, "stopped": 80, "shutting-down": 32}


# =============================================================================================
# Fixtures: archive, stubbed session, fake sleep
# =============================================================================================
def make_archive(path, files=None, *, drop=()):
    """gzip tar like `git archive | gzip -n`: the required files (minimal content) plus `files`."""
    content = {name: f"# {name}\n".encode() for name in config.RELEASE_REQUIRED_FILES}
    content["run_pipeline.sh"] = b"#!/bin/bash\necho ok\n"
    content.update(files or {})
    for name in drop:
        content.pop(name, None)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in sorted(content.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    path.write_bytes(gzip.compress(raw.getvalue(), mtime=0))
    return path


@pytest.fixture
def archive(tmp_path):
    return make_archive(tmp_path / "release.tar.gz")


class StubSession:
    """Real STS/EC2/S3 clients with fake credentials, each behind a Stubber; records every
    operation (service, name, params) in global call order."""

    def __init__(self):
        boto = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                             region_name=config.REGION)
        self.clients = {s: boto.client(s, region_name=config.REGION) for s in ("sts", "ec2", "s3")}
        self.stubs = {s: Stubber(c) for s, c in self.clients.items()}
        self.ops = []
        for service, c in self.clients.items():
            c.meta.events.register("before-parameter-build.*.*", self._recorder(service))
        for stub in self.stubs.values():
            stub.activate()

    def _recorder(self, service):
        def handler(params, model, **kwargs):
            self.ops.append((service, model.name, copy.deepcopy(dict(params))))
        return handler

    def client(self, service, **kwargs):
        assert kwargs.get("region_name") == config.REGION, kwargs
        return self.clients[service]

    def add(self, service, op, response, params=None):
        self.stubs[service].add_response(op, response, params)

    def error(self, service, op, code, params=None):
        self.stubs[service].add_client_error(op, service_error_code=code, http_status_code=400,
                                             expected_params=params)

    def verify(self):
        for stub in self.stubs.values():
            stub.assert_no_pending_responses()

    @property
    def names(self):
        return [name for _, name, _ in self.ops]

    def s3_ops(self):
        return [(name, params) for service, name, params in self.ops if service == "s3"]


class FakeSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def body(data):
    return StreamingBody(io.BytesIO(data), len(data))


def s3p(**params):
    return {"Bucket": BUCKET, "ExpectedBucketOwner": ACCOUNT, **params}


# --- Stub builders -----------------------------------------------------------------------------
def stub_identity(s):
    s.add("sts", "get_caller_identity",
          {"Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/fake/fake", "UserId": "AROAFAKE:fake"}, {})


def stub_instance(s, state="stopped"):
    doc = {"InstanceId": IID, "State": {"Code": STATE_CODES[state], "Name": state}}
    s.add("ec2", "describe_instances", {"Reservations": [{"Instances": [doc]}]}, {"Filters": FIND_FILTERS})
    s.add("ec2", "describe_instances", {"Reservations": [{"Instances": [doc]}]}, {"InstanceIds": [IID]})


def stub_exists(s, key, exists):
    response = {"IsTruncated": False, "KeyCount": 1 if exists else 0, "Name": BUCKET, "Prefix": key}
    if exists:
        response["Contents"] = [{"Key": key, "Size": 41}]
    s.add("s3", "list_objects_v2", response, s3p(Prefix=key))


def stub_read(s, key, data):
    """`exists` + `get_object` if `data` is not None, else only the empty listing."""
    stub_exists(s, key, data is not None)
    if data is not None:
        s.add("s3", "get_object", {"Body": body(data)}, s3p(Key=key))


def stub_put(s, key, data, content_type):
    s.add("s3", "put_object", {"ETag": '"0"'}, s3p(Key=key, Body=data, ContentType=content_type))


def stub_reads(s, *, state="stopped", current=OLD):
    stub_identity(s)
    stub_instance(s, state)
    if state == "stopped":
        stub_read(s, config.RELEASES_CURRENT_KEY,
                  None if current is None else (current if isinstance(current, bytes) else f"{current}\n".encode()))


def request_doc(sha=SHA, rid=RID, now=NOW):
    t = int(now.timestamp())
    return {"sha": sha, "request_id": rid, "requested_at": t, "expires_at": t + 1800}


def request_bytes(doc):
    return (json.dumps(doc, sort_keys=True) + "\n").encode()


def closed_bytes(doc):
    return request_bytes({**doc, "expires_at": doc["requested_at"], "closed": True})


def stub_publish(s, archive_bytes, *, sha=SHA, old=OLD, rid=RID):
    digest = hashlib.sha256(archive_bytes).hexdigest()
    stub_put(s, f"releases/{sha}.tar.gz", archive_bytes, "application/gzip")
    stub_put(s, f"releases/{sha}.tar.gz.sha256", f"{digest}  {sha}.tar.gz\n".encode(), "text/plain")
    if old and old != sha:
        stub_put(s, "releases/previous", f"{old}\n".encode(), "text/plain")
    stub_put(s, "releases/current", f"{sha}\n".encode(), "text/plain")
    stub_put(s, "releases/deploy-request.json", request_bytes(request_doc(sha, rid)), "application/json")
    s.add("ec2", "start_instances",
          {"StartingInstances": [{"InstanceId": IID, "CurrentState": {"Code": 0, "Name": "pending"},
                                  "PreviousState": {"Code": 80, "Name": "stopped"}}]},
          {"InstanceIds": [IID]})


def result_bytes(result="ok", *, sha=SHA, rid=RID, reason=""):
    return json.dumps({"request_id": rid, "sha": sha, "result": result, "reason": reason,
                       "finished_at": int(NOW.timestamp()) + 300, "previous": OLD}).encode()


def stub_poll(s, empty_polls, final=None, *, rid=RID):
    key = f"state/deploys/{rid}.json"
    for _ in range(empty_polls):
        stub_exists(s, key, False)
    if final is not None:
        stub_read(s, key, final)


def stub_close(s, *, sha=SHA, rid=RID):
    stub_put(s, "releases/deploy-request.json", closed_bytes(request_doc(sha, rid)), "application/json")


def stub_restore(s, old=OLD):
    stub_put(s, "releases/current", f"{old}\n".encode(), "text/plain")


def argv(archive, *extra, sha=SHA, rid=RID):
    return ["deploy", "--sha", sha, "--archive", str(archive), "--request-id", rid, *extra]


def run(s, args, capsys, *, now=NOW, sleep=None):
    sleep = sleep or FakeSleep()
    rc = release.main(args, session=s, now=now, sleep=sleep)
    out = capsys.readouterr()
    return rc, out.out, out.err, sleep


def assert_clean(text):
    """No account ID, bucket name or instance ID ever reaches the output."""
    assert ACCOUNT not in text, text
    assert BUCKET not in text, text
    assert IID not in text and not re.search(r"\bi-[0-9a-f]{8,17}\b", text), text
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", text), text


def assert_owner_on_every_s3_call(s):
    calls = s.s3_ops()
    assert calls, "expected S3 calls"
    for name, params in calls:
        assert params.get("ExpectedBucketOwner") == ACCOUNT, (name, params)
        assert params.get("Bucket") == BUCKET, (name, params)


READ_ONLY = ["GetCallerIdentity", "DescribeInstances", "DescribeInstances", "ListObjectsV2", "GetObject"]
WRITES = {"PutObject", "StartInstances", "DeleteObject", "StopInstances", "CreateTags"}


# =============================================================================================
# Happy path, rollback, first deploy
# =============================================================================================
@pytest.mark.parametrize("old, sha", [(OLD, SHA), (SHA, OLD)], ids=["forward", "rollback-to-older"])
def test_happy_path_exact_call_order_and_bodies(archive, capsys, old, sha):
    """Rollback is the same command with an older sha: `previous` gets the old `current`."""
    data = archive.read_bytes()
    s = StubSession()
    stub_reads(s, current=old)
    stub_publish(s, data, sha=sha, old=old)
    stub_poll(s, 2, result_bytes(sha=sha))
    stub_close(s, sha=sha)
    rc, out, err, sleep = run(s, argv(archive, sha=sha), capsys)
    assert rc == 0, out + err
    s.verify()
    assert s.names == READ_ONLY + ["PutObject"] * 5 + ["StartInstances"] + ["ListObjectsV2"] * 3 + \
        ["GetObject", "PutObject"]
    keys = [p["Key"] for n, p in s.s3_ops() if n == "PutObject"]
    assert keys == [f"releases/{sha}.tar.gz", f"releases/{sha}.tar.gz.sha256", "releases/previous",
                    "releases/current", "releases/deploy-request.json", "releases/deploy-request.json"]
    assert sleep.calls == [15, 15, 15]
    assert_owner_on_every_s3_call(s)
    assert f"OK: release {sha} deployed" in out
    assert_clean(out + err)


def test_first_deploy_without_a_pointer(archive, capsys):
    s = StubSession()
    stub_reads(s, current=None)
    stub_publish(s, archive.read_bytes(), old=None)
    stub_poll(s, 0, result_bytes())
    stub_close(s)
    rc, out, err, _ = run(s, argv(archive), capsys)
    assert rc == 0, out + err
    s.verify()
    puts = [p["Key"] for n, p in s.s3_ops() if n == "PutObject"]
    assert "releases/previous" not in puts
    assert s.names[:4] == ["GetCallerIdentity", "DescribeInstances", "DescribeInstances", "ListObjectsV2"]
    assert s.names[4] == "PutObject", "no GetObject on a missing pointer"
    assert_clean(out + err)


def test_redeploy_of_the_current_sha_leaves_previous_alone(archive, capsys):
    s = StubSession()
    stub_reads(s, current=SHA)
    stub_publish(s, archive.read_bytes(), old=SHA)
    stub_poll(s, 0, result_bytes())
    stub_close(s)
    rc, out, err, _ = run(s, argv(archive), capsys)
    assert rc == 0, out + err
    s.verify()
    assert "releases/previous" not in [p["Key"] for n, p in s.s3_ops() if n == "PutObject"]


def test_request_object_content(archive, capsys):
    s = StubSession()
    stub_reads(s)
    stub_publish(s, archive.read_bytes())
    stub_poll(s, 0, result_bytes())
    stub_close(s)
    run(s, argv(archive), capsys)
    s.verify()
    requests = [json.loads(p["Body"]) for n, p in s.s3_ops()
                if n == "PutObject" and p["Key"] == "releases/deploy-request.json"]
    t = int(NOW.timestamp())
    assert requests[0] == {"sha": SHA, "request_id": RID, "requested_at": t, "expires_at": t + 1800}
    # Closed at the end: a later manual boot inside the TTL is not taken as deploy-only.
    assert requests[1] == {"sha": SHA, "request_id": RID, "requested_at": t, "expires_at": t, "closed": True}


def test_checksum_object_is_in_sha256sum_format(archive, capsys):
    s = StubSession()
    stub_reads(s)
    stub_publish(s, archive.read_bytes())
    stub_poll(s, 0, result_bytes())
    stub_close(s)
    run(s, argv(archive), capsys)
    line = next(p["Body"] for n, p in s.s3_ops() if n == "PutObject" and p["Key"].endswith(".sha256")).decode()
    assert re.fullmatch(rf"[0-9a-f]{{64}}  {SHA}\.tar\.gz\n", line)
    assert line.split()[0] == hashlib.sha256(archive.read_bytes()).hexdigest()


# =============================================================================================
# Failures after `current` moved: pointer restored, request closed, exit 1
# =============================================================================================
def test_failed_result_restores_the_pointer(archive, capsys):
    s = StubSession()
    stub_reads(s)
    stub_publish(s, archive.read_bytes())
    stub_poll(s, 1, result_bytes("failed", reason="sha256 mismatch"))
    stub_restore(s)
    stub_close(s)
    rc, out, err, _ = run(s, argv(archive), capsys)
    assert rc == 1
    s.verify()
    puts = [(p["Key"], p["Body"]) for n, p in s.s3_ops() if n == "PutObject"]
    assert puts[-2] == ("releases/current", f"{OLD}\n".encode())
    assert puts[-1][0] == "releases/deploy-request.json"
    assert err.startswith("FAILED:") and "sha256 mismatch" in err
    assert_clean(out + err)


@pytest.mark.parametrize("final", [
    pytest.param(result_bytes("ok", sha=OLD), id="ok-for-another-sha"),
    pytest.param(result_bytes("ok", rid="999-1"), id="ok-for-another-request"),
    pytest.param(b"not json", id="garbage"),
    pytest.param(b"[]", id="not-an-object"),
    pytest.param(json.dumps({"request_id": RID, "sha": SHA, "result": "maybe"}).encode(), id="unknown-result"),
])
def test_a_result_that_is_not_ok_for_this_deploy_counts_as_failed(archive, capsys, final):
    s = StubSession()
    stub_reads(s)
    stub_publish(s, archive.read_bytes())
    stub_poll(s, 0, final)
    stub_restore(s)
    stub_close(s)
    rc, out, err, _ = run(s, argv(archive), capsys)
    assert rc == 1, out + err
    s.verify()
    assert "FAILED:" in err


@pytest.mark.parametrize("minutes, polls", [(1, 4), (2, 8), (15, 60), (30, 120)])
def test_timeout_restores_the_pointer_after_exactly_the_polls_that_fit(archive, capsys, minutes, polls):
    s = StubSession()
    stub_reads(s)
    stub_publish(s, archive.read_bytes())
    stub_poll(s, polls)
    stub_restore(s)
    stub_close(s)
    rc, out, err, sleep = run(s, argv(archive, "--timeout-minutes", str(minutes)), capsys)
    assert rc == 1
    s.verify()
    assert sleep.calls == [15] * polls
    assert s.names.count("ListObjectsV2") == 1 + polls
    assert err.startswith(f"FAILED: no result at state/deploys/{RID}.json after {minutes} minutes")
    # The deploy role has no StopInstances: the message tells the operator to stop it by hand.
    for fragment in ("may still be running", "cannot stop it", "journalctl -u pi-deploy.service"):
        assert fragment in err, fragment
    assert_clean(out + err)


def test_default_timeout_is_15_minutes(archive, capsys):
    s = StubSession()
    stub_reads(s)
    stub_publish(s, archive.read_bytes())
    stub_poll(s, 60)
    stub_restore(s)
    stub_close(s)
    rc, _, _, sleep = run(s, argv(archive), capsys)
    assert rc == 1 and len(sleep.calls) == 60
    s.verify()


def test_failure_on_a_first_deploy_cannot_restore_and_warns(archive, capsys):
    """No old pointer and the role cannot delete: `current` stays at the new sha, with a warning."""
    s = StubSession()
    stub_reads(s, current=None)
    stub_publish(s, archive.read_bytes(), old=None)
    stub_poll(s, 0, result_bytes("failed", reason="bash -n failed"))
    stub_close(s)
    rc, out, err, _ = run(s, argv(archive), capsys)
    assert rc == 1
    s.verify()
    assert [p["Key"] for n, p in s.s3_ops() if n == "PutObject"].count("releases/current") == 1
    assert "WARNING" in err and "DeleteObject" not in s.names
    assert_clean(out + err)


@pytest.mark.parametrize("failing", ["start_instances", "request"])
def test_aws_error_after_current_moved_restores_and_propagates(archive, capsys, failing):
    data = archive.read_bytes()
    s = StubSession()
    stub_reads(s)
    digest = hashlib.sha256(data).hexdigest()
    stub_put(s, f"releases/{SHA}.tar.gz", data, "application/gzip")
    stub_put(s, f"releases/{SHA}.tar.gz.sha256", f"{digest}  {SHA}.tar.gz\n".encode(), "text/plain")
    stub_put(s, "releases/previous", f"{OLD}\n".encode(), "text/plain")
    stub_put(s, "releases/current", f"{SHA}\n".encode(), "text/plain")
    if failing == "request":
        s.error("s3", "put_object", "AccessDenied")
    else:
        stub_put(s, "releases/deploy-request.json", request_bytes(request_doc()), "application/json")
        s.error("ec2", "start_instances", "IncorrectInstanceState", {"InstanceIds": [IID]})
    stub_restore(s)
    stub_close(s)
    with pytest.raises(ClientError):
        release.main(argv(archive), session=s, now=NOW, sleep=FakeSleep())
    s.verify()
    out = capsys.readouterr()
    assert_clean(out.out + out.err)


def test_aws_error_through_cli_main_is_one_sanitized_line(archive, capsys):
    s = StubSession()
    stub_reads(s)
    stub_put(s, f"releases/{SHA}.tar.gz", archive.read_bytes(), "application/gzip")
    s.stubs["s3"].add_client_error("put_object", service_error_code="AccessDenied",
                                   service_message=f"User arn:aws:sts::{ACCOUNT}:assumed-role/x is not authorized",
                                   http_status_code=403)
    with pytest.raises(SystemExit) as exc:
        _aws.cli_main(release.main, argv(archive, "--env-credentials"), session=s, now=NOW, sleep=FakeSleep())
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert out.out.splitlines()[0] == "Profile: environment credentials"
    assert "AWS error AccessDenied in PutObject" in out.err
    assert_clean(out.out + out.err)
    # The pointer never moved: only the archive put was attempted.
    assert [p["Key"] for n, p in s.s3_ops() if n == "PutObject"] == [f"releases/{SHA}.tar.gz",
                                                                     f"releases/{SHA}.tar.gz.sha256"]


def test_restore_failure_is_reported_as_manual_action_and_still_exits_1(archive, capsys):
    s = StubSession()
    stub_reads(s)
    stub_publish(s, archive.read_bytes())
    stub_poll(s, 0, result_bytes("failed", reason="x"))
    s.error("s3", "put_object", "SlowDown")  # the restore of current
    stub_close(s)
    rc, out, err, _ = run(s, argv(archive), capsys)
    assert rc == 1
    s.verify()
    assert "MANUAL ACTION" in err and OLD in err
    assert_clean(out + err)


# =============================================================================================
# Refusals: nothing is written
# =============================================================================================
@pytest.mark.parametrize("state", ["pending", "running", "stopping"])
def test_refuses_unless_the_instance_is_stopped(archive, capsys, state):
    s = StubSession()
    stub_reads(s, state=state)
    rc, out, err, sleep = run(s, argv(archive), capsys)
    assert rc == 1
    s.verify()
    assert s.names == ["GetCallerIdentity", "DescribeInstances", "DescribeInstances"]
    assert not WRITES & set(s.names) and s.s3_ops() == [] and sleep.calls == []
    assert err.startswith("REFUSED:") and state in err and "nothing was changed" in err
    assert_clean(out + err)


# Round 2 (lead review, 2026-11-22): the whole last Thursday (America/Mexico_City local day) is
# reserved for the monthly run, not just the window and the 30 minutes before it. A deploy boot that
# hangs that day would leave the instance on and turn the 21:50 StartInstances into a no-op. The
# old boundaries (30 min before refused, 31 min before and the window end accepted) are replaced by
# the day boundaries: refused iff now or now + TTL is on that local date.
# Last Thursday of November 2026 = 2026-11-26; Mexico City is UTC-6 (no DST since 2022).
DAY_START_UTC = dt.datetime(2026, 11, 26, 6, 0, tzinfo=UTC)   # Thu 00:00 local
DAY_END_UTC = dt.datetime(2026, 11, 27, 6, 0, tzinfo=UTC)     # Fri 00:00 local (exclusive)
TTL = dt.timedelta(seconds=config.DEPLOY_REQUEST_TTL_SECONDS)


@pytest.mark.parametrize("now", [
    pytest.param(WINDOW_START_UTC, id="window-start"),
    pytest.param(WINDOW_START_UTC + dt.timedelta(minutes=45), id="inside-window"),
    pytest.param(WINDOW_END_UTC - dt.timedelta(seconds=1), id="window-last-second"),
    pytest.param(WINDOW_START_UTC - dt.timedelta(minutes=30), id="30-min-before-window"),
    pytest.param(WINDOW_START_UTC - dt.timedelta(minutes=31), id="31-min-before-window"),
    pytest.param(WINDOW_END_UTC, id="window-end"),
    pytest.param(DAY_START_UTC, id="thursday-00:00"),
    pytest.param(DAY_START_UTC + dt.timedelta(hours=12), id="thursday-noon"),
    pytest.param(DAY_END_UTC - dt.timedelta(seconds=1), id="thursday-23:59:59"),
    pytest.param(DAY_START_UTC - TTL, id="wednesday-23:30-ttl-reaches-thursday"),
    pytest.param((DAY_START_UTC - TTL + dt.timedelta(seconds=1)).astimezone(dt.timezone(dt.timedelta(hours=-6))),
                 id="same-rule-with-a-local-offset"),
    pytest.param(dt.datetime(2026, 12, 31, 12, 0, tzinfo=UTC), id="december-last-thursday"),
])
def test_refuses_the_whole_last_thursday_local_day(archive, capsys, now):
    s = StubSession()  # nothing stubbed: any AWS call fails the test
    rc, out, err, _ = run(s, argv(archive), capsys, now=now)
    assert rc == 1
    assert s.ops == []
    assert err.startswith("REFUSED: the last Thursday of the month is reserved for the monthly run (")
    assert "America/Mexico_City" in err and "deploy another day" in err
    assert_clean(out + err)


@pytest.mark.parametrize("now", [
    pytest.param(DAY_START_UTC - TTL - dt.timedelta(seconds=1), id="wednesday-23:29:59"),
    pytest.param(DAY_END_UTC, id="friday-00:00"),
    pytest.param(WINDOW_START_UTC - dt.timedelta(days=7), id="previous-thursday-at-the-window-time"),
    pytest.param(dt.datetime(2026, 11, 19, 6, 0, tzinfo=UTC), id="previous-thursday-00:00"),
])
def test_does_not_refuse_just_outside_the_reserved_day(archive, capsys, now):
    s = StubSession()
    stub_reads(s)
    rc, out, err, _ = run(s, argv(archive, "--dry-run"), capsys, now=now)
    assert rc == 0, err
    s.verify()


def test_reserved_day_helper():
    assert release.reserved_day(DAY_START_UTC) == dt.date(2026, 11, 26)
    assert release.reserved_day(DAY_START_UTC - TTL) == dt.date(2026, 11, 26)
    assert release.reserved_day(DAY_START_UTC - TTL - dt.timedelta(seconds=1)) is None
    assert release.reserved_day(DAY_END_UTC) is None
    # The window check stays as a second guard, and the window lies inside the reserved day.
    assert release.window_conflict(WINDOW_START_UTC - dt.timedelta(minutes=30)) is not None
    assert release.reserved_day(WINDOW_START_UTC - dt.timedelta(minutes=30)) is not None


def test_the_window_used_is_the_boot_guard_one():
    assert release.is_scheduled_window is guard.is_scheduled_window
    assert guard.is_scheduled_window(WINDOW_START_UTC) and not guard.is_scheduled_window(WINDOW_END_UTC)


@pytest.mark.parametrize("pointer", [b"not a sha\n", b"A" * 40 + b"\n", b"", b"a" * 39, b"a" * 41])
def test_malformed_pointer_is_refused_before_any_write(archive, capsys, pointer):
    s = StubSession()
    stub_reads(s, current=pointer)
    rc, out, err, _ = run(s, argv(archive), capsys)
    assert rc == 1
    s.verify()
    assert not WRITES & set(s.names)
    assert err.startswith("REFUSED:") and "malformed" in err


@pytest.mark.parametrize("sha", ["A" * 40, "a" * 39, "a" * 41, "g" * 40, "", " " + "a" * 40, "a" * 40 + "\n", "HEAD"])
def test_bad_sha_is_refused_without_aws(archive, capsys, sha):
    s = StubSession()
    rc, out, err, _ = run(s, argv(archive, sha=sha), capsys)
    assert rc == 1 and s.ops == []
    assert err.startswith("REFUSED:") and "--sha" in err


@pytest.mark.parametrize("rid", ["", "-1", "a/b", "../x", "a b", "a" * 65, "1_2", "x.json", "é"])
def test_bad_request_id_is_refused_without_aws(archive, capsys, rid):
    s = StubSession()
    rc, out, err, _ = run(s, argv(archive, rid=rid), capsys)
    assert rc == 1 and s.ops == []
    assert "--request-id" in err


@pytest.mark.parametrize("minutes", ["0", "31", "-1"])
def test_timeout_out_of_range_is_refused_without_aws(archive, capsys, minutes):
    s = StubSession()
    rc, out, err, _ = run(s, argv(archive, "--timeout-minutes", minutes), capsys)
    assert rc == 1 and s.ops == []


@pytest.mark.parametrize("drop", list(config.RELEASE_REQUIRED_FILES))
def test_archive_without_a_required_file_is_refused_without_aws(tmp_path, capsys, drop):
    bad = make_archive(tmp_path / "r.tar.gz", drop=(drop,))
    s = StubSession()
    rc, out, err, _ = run(s, argv(bad), capsys)
    assert rc == 1 and s.ops == []
    assert "lacks required files" in err and drop in err


@pytest.mark.parametrize("content", [b"", b"plain text", gzip.compress(b"not a tar", mtime=0)])
def test_unreadable_archive_is_refused_without_aws(tmp_path, capsys, content):
    bad = tmp_path / "r.tar.gz"
    bad.write_bytes(content)
    s = StubSession()
    rc, out, err, _ = run(s, argv(bad), capsys)
    assert rc == 1 and s.ops == []
    assert "archive" in err


def test_missing_archive_file_is_refused_without_aws(tmp_path, capsys):
    s = StubSession()
    rc, out, err, _ = run(s, argv(tmp_path / "nope.tar.gz"), capsys)
    assert rc == 1 and s.ops == []


def test_required_files_match_the_instance_deployer():
    """release.py checks the same list pi_deploy.py checks on the instance (no import there)."""
    from infra.instance import pi_deploy
    assert pi_deploy.RELEASE_REQUIRED_FILES == config.RELEASE_REQUIRED_FILES


# =============================================================================================
# Dry-run
# =============================================================================================
@pytest.mark.parametrize("current", [OLD, None], ids=["with-pointer", "first-deploy"])
def test_dry_run_only_reads_and_prints_the_plan(archive, capsys, current):
    s = StubSession()
    stub_reads(s, current=current)
    rc, out, err, sleep = run(s, argv(archive, "--dry-run"), capsys)
    assert rc == 0, err
    s.verify()
    assert set(s.names) <= {"GetCallerIdentity", "DescribeInstances", "ListObjectsV2", "GetObject"}
    assert sleep.calls == []
    assert "[dry-run] would" in out and "nothing was changed" in out
    assert f"releases/{SHA}.tar.gz" in out
    assert_owner_on_every_s3_call(s)
    assert_clean(out + err)


def test_dry_run_still_refuses_a_running_instance(archive, capsys):
    s = StubSession()
    stub_reads(s, state="running")
    rc, _, err, _ = run(s, argv(archive, "--dry-run"), capsys)
    assert rc == 1 and "REFUSED" in err


# =============================================================================================
# Arguments, session, pure helpers
# =============================================================================================
def test_naive_now_is_rejected(archive):
    with pytest.raises(ValueError):
        release.main(argv(archive), session=StubSession(), now=dt.datetime(2026, 11, 10, 15, 0), sleep=FakeSleep())


@pytest.mark.parametrize("args", [[], ["deploy"], ["deploy", "--sha", SHA], ["other"]])
def test_missing_arguments_exit_2(args):
    with pytest.raises(SystemExit) as exc:
        release.main(args, session=StubSession(), now=NOW, sleep=FakeSleep())
    assert exc.value.code == 2


def test_env_credentials_use_the_default_chain_in_the_region(monkeypatch):
    seen = []

    class FakeBoto3Session:
        def __init__(self, **kwargs):
            seen.append(kwargs)

    monkeypatch.setattr(boto3, "Session", FakeBoto3Session)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    _aws.get_session(None, env_credentials=True)
    assert seen == [{"region_name": config.REGION}], "no profile_name: the OIDC env credentials"
    monkeypatch.setenv("AWS_PROFILE", "default")
    with pytest.raises(SystemExit, match="default"):
        _aws.get_session(None, env_credentials=True)
    assert len(seen) == 1


def test_profile_line_with_and_without_env_credentials(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    assert _aws.profile_line() == "Profile: wayakit-vdc"
    assert _aws.profile_line(env_credentials=True) == "Profile: environment credentials"


def test_pure_helpers():
    assert release.archive_key(SHA) == f"releases/{SHA}.tar.gz"
    assert release.checksum_key(SHA) == f"releases/{SHA}.tar.gz.sha256"
    assert release.result_key(RID) == f"state/deploys/{RID}.json"
    assert release.pointer_body(SHA) == f"{SHA}\n"
    assert release.parse_pointer(f"  {SHA}\n".encode()) == SHA
    assert release.POLL_SECONDS == 15 and release.DEFAULT_TIMEOUT_MINUTES == 15
    assert release.result_key(RID).startswith(config.DEPLOYS_STATE_PREFIX)
    assert config.DEPLOY_REQUEST_KEY == "releases/deploy-request.json"
    assert config.RELEASES_PREVIOUS_KEY == "releases/previous"
    assert config.DEPLOYS_STATE_PREFIX == "state/deploys/"
    assert config.DEPLOY_REQUEST_TTL_SECONDS == 1800


def test_instance_state_needs_exactly_one_instance():
    s = StubSession()
    s.add("ec2", "describe_instances", {"Reservations": []}, {"InstanceIds": [IID]})
    with pytest.raises(SystemExit):
        release.instance_state(s.clients["ec2"], IID)


def test_release_source_never_deletes_stops_or_tags():
    text = (config.__file__.rsplit("/", 1)[0] + "/release.py")
    source = open(text, encoding="utf-8").read()
    for forbidden in ("delete_object", "stop_instances", "terminate_instances", "create_tags",
                      "reboot_instances", "put_bucket"):
        assert forbidden not in source, forbidden
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", source)
