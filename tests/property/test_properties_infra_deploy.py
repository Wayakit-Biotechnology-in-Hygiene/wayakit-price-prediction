"""Property tests of F4 deploy (`infra/instance/pi_deploy.py` and `infra/release.py`).

Rules checked over generated inputs:
- Archive safety: `safe_member_path` equals an obvious reference, its output is relative,
  normalized and idempotent; `safe_link_target` never resolves outside the release; any listing
  accepted by `safe_extract` (real tarballs generated from hostile names, links and types) writes
  only inside the destination, and a rejected one writes nothing at all.
- Prune: never the active or the previous release, only 40-hex names, exactly `keep` left (or all
  if fewer), the rest are the newest; idempotent.
- The release.py <-> pi_deploy.py contract (they cannot import each other on the instance):
  pointer, `.sha256` line, deploy request (valid exactly in [requested_at, expires_at), closed =
  never) and result object round-trip between the two modules.
- `parse_request` and `parse_result` never raise and never accept anything that is not a valid
  object for this request.
- Power-off and the window: `should_power_off` is never True inside the monthly window; any time
  release.py accepts a deploy (no window minute in [now, now + TTL]) the boot guard keeps the
  pipeline closed for the whole request lifetime, so a deploy-only boot never runs the pipeline.
- `release.wait_for_result`: exactly ceil(T*60/15) polls on timeout, and it stops at the first
  result.

No AWS, no clock, no network: every input is generated; files only in temporary folders.
"""
from __future__ import annotations

import calendar
import gzip
import io
import json
import math
import os
import posixpath
import tarfile
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from infra import config, release
from infra.instance import pi_boot_guard as guard
from infra.instance import pi_deploy as pd

pytestmark = pytest.mark.property

UTC = timezone.utc
hexsha = st.text(alphabet="0123456789abcdef", min_size=40, max_size=40)
digests = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
request_ids = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", fullmatch=True)
instants = st.datetimes(min_value=datetime(2023, 1, 1), max_value=datetime(2099, 12, 31),
                        timezones=st.just(UTC))
# Path-ish names built from risky pieces.
pieces = st.sampled_from(["a", "b", ".", "..", "", ".release", "x.sh", "...", ". ", "a..b", "\x00"])
names = st.one_of(
    st.lists(pieces, min_size=1, max_size=5).map("/".join),
    st.lists(pieces, min_size=1, max_size=4).map(lambda p: "/" + "/".join(p)),
    st.text(max_size=20),
)


# =============================================================================================
# Archive safety
# =============================================================================================
def ref_member_unsafe(name):
    if not name or "\x00" in name or name.startswith("/"):
        return True
    if ".." in name.split("/"):
        return True
    norm = posixpath.normpath(name)
    return norm in (".", "") or norm == ".release"


@given(names)
@example("./.release")
@example("a/./../b")
@example("a//b/")
@example("./")
def test_safe_member_path_matches_the_reference_and_is_normalized(name):
    try:
        out = pd.safe_member_path(name)
    except pd.UnsafeArchiveError:
        assert ref_member_unsafe(name), name
        return
    assert not ref_member_unsafe(name), name
    assert not out.startswith("/") and ".." not in out.split("/")
    assert posixpath.normpath(out) == out and out not in (".", "", ".release")
    assert pd.safe_member_path(out) == out  # idempotent
    base = "/r/e/l"
    assert posixpath.normpath(posixpath.join(base, out)).startswith(base + "/")


@given(names, names)
@example("a/link", "../b")
@example("a/link", "../../b")
@example("link", "./")
def test_safe_link_target_never_resolves_outside(member, linkname):
    try:
        path = pd.safe_member_path(member)
    except pd.UnsafeArchiveError:
        return
    try:
        resolved = pd.safe_link_target(path, linkname)
    except pd.UnsafeArchiveError:
        if linkname and "\x00" not in linkname and not linkname.startswith("/"):
            ref = posixpath.normpath(posixpath.join(posixpath.dirname(path), linkname))
            assert ref == ".." or ref.startswith("../"), (path, linkname)
        return
    assert resolved != ".." and not resolved.startswith(("../", "/"))
    assert posixpath.normpath(posixpath.join("/r/e/l", resolved)).startswith("/r/e/l")


KINDS = {"file": tarfile.REGTYPE, "dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE,
         "hardlink": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE}
member_names = st.lists(st.sampled_from(["a", "b", "..", ".", "c.sh"]), min_size=1, max_size=3).map("/".join) | \
    st.sampled_from(["/abs", "../up", ".release", "a/../../up"])
link_targets = st.sampled_from(["b", "../b", "../../out", "/etc", "a/b", ".", "..", "c.sh"])
hostile_members = st.lists(st.tuples(member_names, st.sampled_from(sorted(KINDS)), link_targets),
                           min_size=1, max_size=6)
# Mostly-valid listings (git archive output plus in-tree links), so accepted archives are common too.
safe_members = st.lists(st.tuples(st.lists(st.sampled_from(["a", "b", "c.sh", "d"]), min_size=1, max_size=3).map("/".join),
                                  st.sampled_from(["file", "file", "dir", "symlink"]),
                                  st.sampled_from(["b", "../b", "c.sh", "../a/b", "../../x"])),
                        min_size=1, max_size=8)
members = st.one_of(hostile_members, safe_members)


def _tarball(entries):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, kind, link in entries:
            info = tarfile.TarInfo(name)
            info.type = KINDS[kind]
            data = None
            if kind == "file":
                data = b"x\n"
                info.size = len(data)
            elif kind in ("symlink", "hardlink"):
                info.linkname = link
            tar.addfile(info, io.BytesIO(data) if data else None)
    return gzip.compress(raw.getvalue(), mtime=0)


def _snapshot(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


@given(members)
@example([("a", "symlink", "../../out"), ("a/b", "file", "")])
@example([("a", "symlink", "."), ("a/b", "file", "")])
@example([("a", "file", ""), ("a", "symlink", "b")])
@example([("c.sh", "hardlink", "/etc")])
def test_safe_extract_writes_only_inside_dest_or_nothing(entries):
    with tempfile.TemporaryDirectory() as sandbox:
        sandbox = Path(sandbox)
        dest, outside = sandbox / "dest", sandbox / "outside"
        dest.mkdir()
        outside.mkdir()
        (outside / "keep").write_text("k")
        archive = sandbox / "r.tar.gz"
        archive.write_bytes(_tarball(entries))
        before_outside = _snapshot(outside)
        try:
            pd.safe_extract(archive, dest)
        except pd.DeployError:
            assert _snapshot(dest) == [], "a rejected archive writes nothing"
        else:
            for p in dest.rglob("*"):
                if p.is_symlink():
                    real = Path(os.path.realpath(p))
                    assert real == dest.resolve() or dest.resolve() in real.parents, (p, real)
                else:
                    assert dest.resolve() in p.resolve().parents
                assert p.is_symlink() or p.is_file() or p.is_dir()
            assert not (dest / ".release").exists()
        assert _snapshot(outside) == before_outside
        assert sorted(x.name for x in sandbox.iterdir()) == ["dest", "outside", "r.tar.gz"]


@given(st.lists(st.tuples(member_names, st.sampled_from(["file", "dir", "symlink", "other"]), link_targets,
                          st.integers(0, 10)), max_size=8))
def test_check_members_accepts_only_safe_unique_listings(listing):
    try:
        plan = pd.check_members(listing)
    except pd.UnsafeArchiveError:
        return
    paths = [p for p, _, _ in plan]
    assert len(paths) == len(set(paths))
    kinds = dict((p, k) for p, k, _ in plan)
    for path, kind, link in plan:
        assert kind in ("file", "dir", "symlink")
        assert pd.safe_member_path(path) == path
        parent = posixpath.dirname(path)
        while parent:
            assert kinds.get(parent) not in ("file", "symlink"), (path, parent)
            parent = posixpath.dirname(parent)
        if kind == "symlink":
            pd.safe_link_target(path, link)


# =============================================================================================
# Prune
# =============================================================================================
folder_names = st.one_of(hexsha, st.sampled_from(["not-a-release", ".tmp-x", "A" * 40, "a" * 39]))


@given(st.dictionaries(folder_names, st.integers(0, 50), max_size=12), st.data(), st.integers(1, 8))
def test_releases_to_prune(mtimes, data, keep):
    shas = sorted(n for n in mtimes if pd.is_sha(n))
    current = data.draw(st.sampled_from(shas)) if shas else data.draw(hexsha)
    previous = data.draw(st.one_of(st.none(), st.sampled_from(shas) if shas else st.none()))
    doomed = pd.releases_to_prune(mtimes, current, previous, keep)
    assert doomed == sorted(doomed)
    assert set(doomed) <= set(shas)
    assert current not in doomed and previous not in doomed
    kept = [n for n in shas if n not in doomed]
    protected = {n for n in (current, previous) if n in shas}
    assert len(kept) == max(min(keep, len(shas)), len(protected))
    # The others kept are the newest of the rest (ties by name).
    others = sorted((n for n in shas if n not in protected), key=lambda n: (-mtimes[n], n))
    assert set(kept) - protected == set(others[:max(0, len(kept) - len(protected))])
    # Idempotent.
    assert pd.releases_to_prune({n: mtimes[n] for n in kept}, current, previous, keep) == []


# =============================================================================================
# release.py <-> pi_deploy.py contract
# =============================================================================================
@given(hexsha, st.sampled_from(["", " ", "\n", "\r\n", "  \n"]))
def test_pointer_round_trip(sha, pad):
    body = (pad + release.pointer_body(sha) + pad).encode()
    assert pd.parse_pointer(body) == sha == release.parse_pointer(body)


@given(st.binary(max_size=60))
@example(b"A" * 40)
@example(b"a" * 40 + b" x")
def test_both_pointer_parsers_agree(body):
    def outcome(fn, exc):
        try:
            return fn(body)
        except exc:
            return None
    assert outcome(pd.parse_pointer, pd.DeployError) == outcome(release.parse_pointer, ValueError)


@given(digests, hexsha)
def test_checksum_line_round_trip(digest, sha):
    body = release.checksum_body(digest, sha).encode()
    assert pd.parse_sha256_file(body, f"{sha}.tar.gz") == digest


@given(digests, hexsha, hexsha)
def test_checksum_line_for_another_archive_is_rejected(digest, sha, other):
    assume(sha != other)
    with pytest.raises(pd.DeployError):
        pd.parse_sha256_file(release.checksum_body(digest, other).encode(), f"{sha}.tar.gz")


@given(hexsha, request_ids, instants, st.integers(-4000, 4000))
def test_request_is_deploy_mode_exactly_while_valid(sha, rid, now, offset):
    req = release.build_request(sha, rid, now)
    body = release.request_body(req)
    t = req["requested_at"] + offset
    parsed = pd.parse_request(body, float(t))
    valid = req["requested_at"] - pd.CLOCK_SKEW_SECONDS <= t < req["expires_at"]
    assert (parsed is not None) == valid
    if valid:
        assert parsed == {k: req[k] for k in ("sha", "request_id", "requested_at", "expires_at")}
    # The closed request release.py writes at the end is never deploy mode.
    assert pd.parse_request(release.request_body(release.closed_request(req)), float(t)) is None
    assert req["expires_at"] - req["requested_at"] == config.DEPLOY_REQUEST_TTL_SECONDS


@given(st.one_of(st.binary(max_size=80), st.text(max_size=80).map(str.encode),
                 st.recursive(st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=10),
                              lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=12), c, max_size=5),
                              max_leaves=10).map(lambda v: json.dumps(v).encode())),
       st.floats(allow_nan=True, allow_infinity=True))
def test_parse_request_never_raises(body, now):
    out = pd.parse_request(body, now)
    assert out is None or (pd.is_sha(out["sha"]) and out["requested_at"] - pd.CLOCK_SKEW_SECONDS <= now
                           < out["expires_at"])


@given(hexsha, request_ids, st.booleans(), st.sampled_from([pd.REASON_DEPLOYED, pd.REASON_CHECKSUM,
                                                              pd.REASON_ENV_CHANGED, pd.REASON_ALREADY_CURRENT]),
       st.integers(0, 2**31), st.one_of(st.none(), hexsha))
def test_result_object_round_trip(sha, rid, ok, reason, finished, previous):
    obj = pd.result_object(rid, sha, ok, reason, finished, previous)
    body = (json.dumps(obj, sort_keys=True) + "\n").encode()
    assert release.parse_result(body, sha, rid) == ("ok" if ok else "failed", reason)


@given(st.binary(max_size=100) | st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=10),
    lambda c: st.lists(c, max_size=3) | st.dictionaries(st.sampled_from(["request_id", "sha", "result", "reason", "x"]),
                                                        c, max_size=5),
    max_leaves=8).map(lambda v: json.dumps(v).encode()), hexsha, request_ids)
def test_parse_result_only_accepts_ok_for_this_request(body, sha, rid):
    outcome, reason = release.parse_result(body, sha, rid)
    assert outcome in ("ok", "failed")
    assert len(reason) <= release.REASON_MAX_CHARS and all(" " <= ch <= "~" for ch in reason)
    if outcome == "ok":
        data = json.loads(body)
        assert data["request_id"] == rid and data["sha"] == sha and data["result"] == "ok"


# =============================================================================================
# Window and power-off: a deploy-only boot never runs the pipeline
# =============================================================================================
def window_start(local_day, tz):
    return datetime.combine(local_day, time(21, 40), ZoneInfo(tz))


def last_thursday(year, month):
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - 3) % 7)


# Boundaries around the 2026-11-26 window (21:40-23:00 Mexico City = 03:40-05:00 UTC next day).
EDGES = [datetime(2026, 11, 27, 3, 9, 59, tzinfo=UTC), datetime(2026, 11, 27, 3, 10, tzinfo=UTC),
         datetime(2026, 11, 27, 4, 59, 59, tzinfo=UTC), datetime(2026, 11, 27, 5, 0, tzinfo=UTC),
         datetime(2026, 12, 31, 3, 10, tzinfo=UTC), datetime(2027, 1, 1, 3, 40, tzinfo=UTC)]


@given(instants)
@example(EDGES[0])
@example(EDGES[1])
@example(EDGES[2])
@example(EDGES[3])
@example(EDGES[4])
@example(EDGES[5])
def test_window_conflict_matches_a_reference(now):
    """Refused iff `now` is in [window start - TTL, window end) of a month's last Thursday."""
    tz = config.SCHEDULE_TIMEZONE
    local = now.astimezone(ZoneInfo(tz))
    ttl = timedelta(seconds=config.DEPLOY_REQUEST_TTL_SECONDS)
    expected = False
    for d in (local.date(), local.date() + timedelta(days=1)):
        start = window_start(last_thursday(d.year, d.month), tz)
        end = start + timedelta(minutes=80)
        expected = expected or (start - ttl <= now < end)
    assert (release.window_conflict(now) is not None) == expected


@given(instants)
@example(EDGES[0])
@example(EDGES[3])
def test_an_accepted_deploy_never_boots_inside_the_window(now):
    """If release.py accepts (no conflict), the boot guard says "outside" during the whole request
    lifetime, so pi-pipeline.service skips ExecStart on that boot, and pi_deploy may power off."""
    assume(release.window_conflict(now) is None)
    ttl = config.DEPLOY_REQUEST_TTL_SECONDS
    for seconds in range(0, ttl + 1, 30):
        moment = now + timedelta(seconds=seconds)
        assert not guard.is_scheduled_window(moment)
        assert pd.should_power_off(True, moment, False)


@given(instants, st.booleans(), st.booleans())
def test_should_power_off_never_inside_the_window(now, deploy_mode, no_poweroff):
    result = pd.should_power_off(deploy_mode, now, no_poweroff)
    assert result == (deploy_mode and not no_poweroff and not guard.is_scheduled_window(now))
    if guard.is_scheduled_window(now):
        assert result is False


@given(st.integers(2023, 2099), st.integers(1, 12), st.integers(0, 79))
def test_every_minute_of_every_window_keeps_the_instance_on(year, month, minute):
    moment = (window_start(last_thursday(year, month), config.SCHEDULE_TIMEZONE)
              + timedelta(minutes=minute)).astimezone(UTC)
    assert guard.is_scheduled_window(moment)
    assert not pd.should_power_off(True, moment, False)
    assert release.window_conflict(moment) is not None


# =============================================================================================
# release.wait_for_result
# =============================================================================================
class _Store:
    def __init__(self, arrive_at, body):
        self.arrive_at, self.body, self.reads = arrive_at, body, 0

    def read(self, key):
        self.reads += 1
        return self.body if self.arrive_at is not None and self.reads >= self.arrive_at else None


@given(st.integers(1, 30), st.one_of(st.none(), st.integers(1, 130)), hexsha, request_ids, st.booleans())
def test_wait_for_result_polls(minutes, arrive_at, sha, rid, ok):
    polls = math.ceil(minutes * 60 / release.POLL_SECONDS)
    body = json.dumps({"request_id": rid, "sha": sha, "result": "ok" if ok else "failed", "reason": "r"}).encode()
    store, sleeps = _Store(arrive_at, body), []
    outcome, _ = release.wait_for_result(store, sha, rid, minutes, sleeps.append)
    if arrive_at is not None and arrive_at <= polls:
        assert outcome == ("ok" if ok else "failed")
        assert store.reads == arrive_at
    else:
        assert outcome == "timeout"
        assert store.reads == polls
    assert sleeps == [release.POLL_SECONDS] * store.reads


# =============================================================================================
# pi_deploy.with_retries (F4 lead finding: STS and the small reads are retried)
# =============================================================================================
@given(st.integers(0, 10), st.one_of(st.none(), st.booleans(), hexsha), st.integers(1, 8))
@example(5, "ok", 6)
@example(6, "ok", 6)
def test_with_retries_attempts_and_waits(failures, value, attempts):
    """k transient StoreErrors: success iff k < attempts; exactly min(k+1, attempts) calls; the
    waits are the first min(k, attempts-1) delays; every retry is logged without AWS text."""
    calls, sleeps, log = [], [], []

    def fn():
        calls.append(1)
        if len(calls) <= failures:
            raise pd.StoreError("s3api get-object", 254, "SlowDown")
        return value

    try:
        out = pd.with_retries(fn, "reading x", log.append, sleeps.append, attempts=attempts)
    except pd.StoreError as exc:
        assert failures >= attempts
        assert exc.detail == "aws s3api get-object failed (exit 254, SlowDown)"
    else:
        assert failures < attempts and out == value
    assert len(calls) == min(failures + 1, attempts)
    n = min(failures, attempts - 1)
    delays = pd.RETRY_DELAYS_SECONDS
    assert sleeps == [delays[min(i, len(delays) - 1)] for i in range(n)]
    assert len(log) == n
    assert all(line.startswith("reading x: aws s3api get-object failed (exit 254, SlowDown); attempt ")
               for line in log)
    if attempts == pd.READ_ATTEMPTS:
        assert sum(sleeps) <= sum(delays)
