"""Unit tests of the instance side of F4: `infra/instance/pi_deploy.py` and `pi-deploy.service`.

No test reaches AWS, systemd or this machine's folders:
- `pi_deploy.main(argv, store=, now=, paths=, runner=, environ=)` runs with every location of
  `Paths` inside `tmp_path`, a `FakeStore` (S3 as a dict: `get(key, dest) -> bool`,
  `put(key, src)`), a fixed clock and a `FakeRunner` that records every command. The runner only
  really runs `/bin/bash -n <file>` (a syntax check, no side effects); `chown`, `systemctl`,
  `runuser`/conda and `aws` are recorded and answered with a fixed exit code.
- The AWS CLI path (`AwsCliStore`, `account_id`) is driven by `FakeAwsCli`, a runner that answers
  `aws sts` / `aws s3api` argv lists from a dict, so the exact flags (`--region`,
  `--expected-bucket-owner`) and the redaction of the account and the bucket are checked.
- Unit files are parsed with configparser.

What is covered (F4 spec, "Tests"): happy path (atomic switch, `.release`, prune keeps 5 with the
active and previous ones), checksum mismatch, unsafe archive members, missing required file,
`bash -n` failure, environment.yml change refused or allowed with the flag, legacy-folder
migration and its rollback, already-current no-op, missing pointer (normal boot ok, deploy boot
fails), expired/closed/handled requests are normal boots, power-off only on a deploy boot and
never inside the monthly window, the result object, no account or bucket in the output, and the
unit ordering that keeps a deploy-only boot from ever running the pipeline.

Fake account `000000000000`; shas are made of one repeated hex digit.
"""
from __future__ import annotations

import ast
import configparser
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from infra import config
from infra.instance import pi_boot_guard as guard
from infra.instance import pi_deploy as pd
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.unit

ACCOUNT = "000000000000"
BUCKET = f"price-intelligence-{ACCOUNT}"
TOPIC = f"arn:aws:sns:eu-north-1:{ACCOUNT}:price-intelligence-alerts"
SHA = "a" * 40
OLD = "b" * 40
RID = "1234567890-1"
UTC = timezone.utc
NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # a Tuesday: far from the monthly window
WINDOW_NOW = datetime(2026, 11, 27, 3, 45, tzinfo=UTC)  # 2026-11-26 21:45 Mexico City (last Thursday)
INSTANCE_DIR = REPO_ROOT / "infra" / "instance"
SYSTEM_FILES = {  # file in the release -> real content from this repo
    "infra/instance/pi-pipeline.service": (INSTANCE_DIR / "pi-pipeline.service").read_bytes(),
    "infra/instance/pi-deploy.service": (INSTANCE_DIR / "pi-deploy.service").read_bytes(),
    "infra/instance/pi_deploy.py": (INSTANCE_DIR / "pi_deploy.py").read_bytes(),
    "infra/instance/pi_boot_guard.py": (INSTANCE_DIR / "pi_boot_guard.py").read_bytes(),
}


# =============================================================================================
# Doubles
# =============================================================================================
class FakeStore:
    """S3 as a dict. `fail` = keys whose get raises the StoreError the real store raises;
    `fail_put` = keys whose put raises it."""

    def __init__(self, objects=None, fail=(), fail_put=(), flaky=None):
        self.objects = dict(objects or {})
        self.fail = set(fail)
        self.fail_put = set(fail_put)
        self.flaky = dict(flaky or {})  # key -> number of gets that fail before it works
        self.gets, self.puts = [], []

    def get(self, key, dest_path):
        self.gets.append(key)
        if self.flaky.get(key, 0) > 0:
            self.flaky[key] -= 1
            raise pd.StoreError("s3api get-object", 254, "SlowDown")
        if key in self.fail:
            raise pd.StoreError("s3api get-object", 254, "AccessDenied")
        if key not in self.objects:
            return False
        Path(dest_path).write_bytes(self.objects[key])
        return True

    def put(self, key, src_path):
        if key in self.fail_put:
            raise pd.StoreError("s3api put-object", 254, "AccessDenied")
        data = Path(src_path).read_bytes()
        self.puts.append((key, data))
        self.objects[key] = data

    def result(self, rid=RID):
        key = f"state/deploys/{rid}.json"
        return json.loads(self.objects[key]) if key in self.objects else None


class FakeRunner:
    """Records argv lists. Only `/bin/bash -n` really runs. `fail` = set of argv prefixes (tuples)
    that exit 1; `pipeline_active` answers `systemctl is-active`."""

    def __init__(self, *, fail=(), pipeline_active=False, on_call=None):
        self.calls = []
        self.fail = [tuple(f) for f in fail]
        self.pipeline_active = pipeline_active
        self.on_call = on_call
        self.sleeps = []  # waits between retries (injected as main's `sleep`)

    def __call__(self, argv, timeout=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if self.on_call:
            self.on_call(argv)
        if argv[:2] == ["/bin/bash", "-n"]:
            return subprocess.run(argv, capture_output=True, text=True, timeout=30,
                                  env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        rc = 0
        if argv[:3] == ["systemctl", "is-active", "--quiet"]:
            rc = 0 if self.pipeline_active else 3
        if any(tuple(argv[:len(f)]) == f for f in self.fail):
            rc = 1
        return subprocess.CompletedProcess(argv, rc, "", "")

    def named(self, *prefix):
        return [c for c in self.calls if tuple(c[:len(prefix)]) == prefix]

    @property
    def powered_off(self):
        return ["systemctl", "poweroff", "--no-block"] in self.calls


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Retries must never really wait in a test: every call injects `sleep`; a forgotten one fails
    here instead of waiting up to 105 s."""
    def refuse(seconds):
        raise AssertionError(f"real time.sleep({seconds}) called: inject sleep=")
    monkeypatch.setattr(pd.time, "sleep", refuse)


def make_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return pd.Paths(app_link=home / "wayakit-price-prediction", releases_dir=home / "pi-releases",
                    deployer_dir=tmp_path / "opt" / "price-intelligence", systemd_dir=tmp_path / "systemd",
                    log_dir=tmp_path / "logs")


def release_files(overrides=None):
    """Release content; `overrides` maps a path to new bytes, or to None to drop it."""
    files = {
        "run_pipeline.sh": b"#!/bin/bash\nset -e\necho run\n",
        "environment.yml": b"name: wayakit-price-prediction\ndependencies:\n  - python=3.10\n",
        "README.md": b"# PI\n",
        "ml_model/1a_preprocess_data.py": b"print('x')\n",
        **SYSTEM_FILES,
    }
    for name, data in (overrides or {}).items():
        if data is None:
            files.pop(name, None)
        else:
            files[name] = data
    return files


def tar_gz(files, extra_members=()):
    """gzip tar like `git archive | gzip -n` (sorted regular files, mode 0644, 0755 for .sh)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
        for info, data in extra_members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return gzip.compress(raw.getvalue(), mtime=0)


def publish(store, sha=SHA, files=None, *, archive=None, checksum=None, pointer=True):
    data = archive if archive is not None else tar_gz(files or release_files())
    store.objects[f"releases/{sha}.tar.gz"] = data
    digest = checksum or hashlib.sha256(data).hexdigest()
    store.objects[f"releases/{sha}.tar.gz.sha256"] = f"{digest}  {sha}.tar.gz\n".encode()
    if pointer:
        store.objects["releases/current"] = f"{sha}\n".encode()
    return data


def request(store, sha=SHA, rid=RID, *, at=NOW, ttl=1800, **extra):
    """The deploy request release.py writes (requested a minute before boot)."""
    t = int(at.timestamp()) - 60
    doc = {"sha": sha, "request_id": rid, "requested_at": t, "expires_at": t + ttl, **extra}
    store.objects["releases/deploy-request.json"] = (json.dumps(doc, sort_keys=True) + "\n").encode()
    return doc


def install_active(paths, sha, files=None):
    """An already-installed release folder and APP_LINK pointing at it."""
    folder = paths.releases_dir / sha
    folder.mkdir(parents=True)
    for name, data in (files or release_files()).items():
        p = folder / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (folder / ".release").write_text(sha + "\n")
    if paths.app_link.is_symlink():
        paths.app_link.unlink()
    paths.app_link.symlink_to(folder)
    return folder


def run(tmp_path, store, *argv, now=NOW, runner=None, environ=None, paths=None, capsys=None):
    paths = paths or make_paths(tmp_path)
    runner = runner or FakeRunner()
    rc = pd.main(list(argv), store=store, now=now, paths=paths, runner=runner, environ=environ or {},
                 sleep=runner.sleeps.append)
    out = capsys.readouterr().out if capsys else ""
    return rc, paths, runner, out


def target(paths):
    return Path(os.readlink(paths.app_link)) if paths.app_link.is_symlink() else None


def leftovers(paths):
    found = list(paths.releases_dir.glob(".tmp-*")) if paths.releases_dir.exists() else []
    return found + list(paths.app_link.parent.glob(paths.app_link.name + ".tmp-*"))


def assert_untouched(paths, old_target):
    assert target(paths) == old_target
    assert not leftovers(paths), leftovers(paths)


# =============================================================================================
# Happy path
# =============================================================================================
def test_deploy_boot_happy_path(tmp_path, capsys):
    store = FakeStore()
    publish(store)
    request(store)
    rc, paths, runner, out = run(tmp_path, store, capsys=capsys)
    assert rc == 0, out
    final = paths.releases_dir / SHA
    assert target(paths) == final and final.is_dir()
    assert (final / ".release").read_text() == SHA + "\n"
    assert (final / "run_pipeline.sh").read_bytes() == release_files()["run_pipeline.sh"]
    assert os.stat(final / "run_pipeline.sh").st_mode & 0o777 == 0o755
    assert os.stat(final / "README.md").st_mode & 0o777 == 0o644
    assert not leftovers(paths)
    # Result object (exact) and power-off at the very end.
    assert store.result() == {"request_id": RID, "sha": SHA, "result": "ok", "reason": "deployed",
                              "finished_at": int(NOW.timestamp()), "previous": None}
    assert [k for k, _ in store.puts] == [f"state/deploys/{RID}.json"]
    assert runner.calls[-1] == ["systemctl", "poweroff", "--no-block"]
    # Owned by ec2-user (recursively, without following links) before the switch.
    assert any(c[:4] == ["chown", "-R", "-h", "ec2-user:ec2-user"] and ".tmp-" in c[4] for c in runner.calls)
    # bash -n ran on the unpacked copy, never on the live path.
    bash = runner.named("/bin/bash", "-n")
    assert len(bash) == 1 and ".tmp-" in bash[0][2] and bash[0][2].endswith("run_pipeline.sh")
    # System files synced from the release; daemon-reload because units changed.
    for rel, data in SYSTEM_FILES.items():
        dst = (paths.systemd_dir if rel.endswith(".service") else paths.deployer_dir) / Path(rel).name
        assert dst.read_bytes() == data, dst
        assert os.stat(dst).st_mode & 0o777 == 0o644
    assert ["systemctl", "daemon-reload"] in runner.calls
    # The log goes to PI_LOG_DIR/deploy.log, nothing is written inside the release but .release.
    log = (paths.log_dir / "deploy.log").read_text()
    assert "deploy mode" in log and "powering off" in log
    assert sorted(p.name for p in final.iterdir()) == sorted({".release", "run_pipeline.sh", "environment.yml",
                                                               "README.md", "ml_model", "infra"})


def test_switch_is_one_atomic_rename_and_the_old_release_stays_active_until_then(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    old_folder = install_active(paths, OLD)
    store = FakeStore()
    publish(store)
    request(store)
    seen = []
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        if Path(dst) == paths.app_link:
            # Right before the rename the live link still points at the old release and the new
            # folder is complete (it has its .release marker).
            seen.append((Path(src).name, Path(os.readlink(dst)), (paths.releases_dir / SHA / ".release").is_file()))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(pd.os, "replace", spy)
    rc, *_ = run(tmp_path, store, paths=paths)
    assert rc == 0
    assert len(seen) == 1
    tmp_name, before, complete = seen[0]
    assert tmp_name.startswith("wayakit-price-prediction.tmp-")
    assert before == old_folder and complete
    assert target(paths) == paths.releases_dir / SHA
    assert store.result()["previous"] == OLD


def test_normal_boot_installs_a_new_pointer_but_writes_no_result_and_never_powers_off(tmp_path):
    store = FakeStore()
    publish(store)
    rc, paths, runner, _ = run(tmp_path, store)
    assert rc == 0
    assert target(paths) == paths.releases_dir / SHA
    assert store.puts == [] and not runner.powered_off


def test_normal_boot_already_current_downloads_nothing(tmp_path):
    paths = make_paths(tmp_path)
    install_active(paths, SHA)
    store = FakeStore()
    publish(store)
    rc, _, runner, _ = run(tmp_path, store, paths=paths)
    assert rc == 0
    assert f"releases/{SHA}.tar.gz" not in store.gets
    assert target(paths) == paths.releases_dir / SHA
    assert not runner.powered_off and store.puts == []
    assert "already active" in (paths.log_dir / "deploy.log").read_text()


def test_deploy_boot_already_current_reports_ok_and_powers_off(tmp_path):
    paths = make_paths(tmp_path)
    install_active(paths, SHA)
    store = FakeStore()
    publish(store)
    request(store)
    rc, _, runner, _ = run(tmp_path, store, paths=paths)
    assert rc == 0
    assert store.result()["result"] == "ok" and store.result()["reason"] == "already current"
    assert runner.powered_off


def test_rollback_to_an_older_release_still_on_disk_reuses_it(tmp_path):
    """Rollback = deploy of an older sha: same path; a folder with a matching .release is reused."""
    paths = make_paths(tmp_path)
    old_folder = install_active(paths, OLD)
    install_active(paths, SHA)  # newer one active
    store = FakeStore()
    store.objects["releases/current"] = f"{OLD}\n".encode()  # archive not even needed
    request(store, sha=OLD)
    rc, _, runner, _ = run(tmp_path, store, paths=paths)
    assert rc == 0
    assert target(paths) == old_folder
    assert f"releases/{OLD}.tar.gz" not in store.gets
    assert store.result() == {"request_id": RID, "sha": OLD, "result": "ok", "reason": "deployed",
                              "finished_at": int(NOW.timestamp()), "previous": SHA}
    assert len(runner.named("/bin/bash", "-n")) == 1, "a reused folder is still checked"


def test_a_folder_with_a_wrong_marker_is_replaced_not_reused(tmp_path):
    paths = make_paths(tmp_path)
    stale = paths.releases_dir / SHA
    stale.mkdir(parents=True)
    (stale / ".release").write_text(OLD + "\n")
    (stale / "junk").write_text("x")
    store = FakeStore()
    publish(store)
    rc, *_ = run(tmp_path, store, paths=paths)
    assert rc == 0
    assert (stale / ".release").read_text() == SHA + "\n" and not (stale / "junk").exists()
    assert not leftovers(paths)


def test_leftovers_of_an_interrupted_run_are_cleaned(tmp_path):
    paths = make_paths(tmp_path)
    paths.releases_dir.mkdir(parents=True)
    (paths.releases_dir / f".tmp-{SHA}-999").mkdir()
    (paths.releases_dir / f".tmp-{SHA}-999" / "x").write_text("x")
    (paths.app_link.parent / "wayakit-price-prediction.tmp-999").symlink_to(paths.releases_dir)
    store = FakeStore()
    rc, *_ = run(tmp_path, store, paths=paths)  # no pointer: normal boot, nothing to install
    assert rc == 0
    assert not leftovers(paths)


# =============================================================================================
# Prune
# =============================================================================================
def test_prune_keeps_five_including_the_active_and_the_previous_one(tmp_path):
    paths = make_paths(tmp_path)
    shas = [c * 40 for c in "0123456"]  # 7 old releases; "0..." is the oldest
    for i, sha in enumerate(shas):
        folder = install_active(paths, sha) if sha == shas[0] else None
        if folder is None:
            folder = paths.releases_dir / sha
            folder.mkdir(parents=True)
            (folder / ".release").write_text(sha + "\n")
        stamp = 1_700_000_000 + i * 100
        os.utime(folder / ".release", (stamp, stamp))
    # The active one is the OLDEST: it is the "previous" after the switch and must survive.
    paths.app_link.unlink()
    paths.app_link.symlink_to(paths.releases_dir / shas[0])
    (paths.releases_dir / "not-a-release").mkdir()
    store = FakeStore()
    publish(store)
    rc, *_ = run(tmp_path, store, paths=paths)
    assert rc == 0
    left = sorted(p.name for p in paths.releases_dir.iterdir() if pd.is_sha(p.name))
    assert len(left) == 5
    assert SHA in left and shas[0] in left
    assert set(left) == {SHA, shas[0], shas[6], shas[5], shas[4]}
    assert (paths.releases_dir / "not-a-release").is_dir(), "only 40-hex folders are pruned"


# =============================================================================================
# Failures before the switch: APP_LINK untouched, failed result, still powers off
# =============================================================================================
def _failing_deploy(tmp_path, store, *, runner=None, environ=None, argv=()):
    paths = make_paths(tmp_path)
    old_folder = install_active(paths, OLD)
    request(store)
    rc, _, runner, _ = run(tmp_path, store, *argv, paths=paths, runner=runner, environ=environ)
    return rc, paths, runner, old_folder


def _assert_failed(rc, paths, runner, store, old_folder, reason, *, unpacked=False):
    """`unpacked`: the failure comes after the release folder was built and checked (environment
    checks): it stays on disk to be reused by the `--allow-env-update` rerun; APP_LINK is untouched."""
    assert rc == 1
    assert_untouched(paths, old_folder)
    result = store.result()
    assert result is not None and result["result"] == "failed" and result["reason"] == reason, result
    assert result["sha"] == SHA and result["previous"] == OLD
    assert runner.powered_off, "a failed deploy boot still powers off"
    assert (paths.releases_dir / SHA).exists() == unpacked
    if unpacked:
        assert (paths.releases_dir / SHA / ".release").read_text() == SHA + "\n"


def test_checksum_mismatch_keeps_current(tmp_path):
    store = FakeStore()
    publish(store, checksum="0" * 64)
    rc, paths, runner, old = _failing_deploy(tmp_path, store)
    _assert_failed(rc, paths, runner, store, old, pd.REASON_CHECKSUM)
    assert runner.named("/bin/bash") == [], "nothing is unpacked or checked after a bad checksum"


@pytest.mark.parametrize("body", [b"", b"zz  x\n", b"0" * 63 + b"\n", b"0" * 64 + b"  other.tar.gz\n",
                                  b"0" * 64 + b"  a b c\n"])
def test_malformed_checksum_file_keeps_current(tmp_path, body):
    store = FakeStore()
    publish(store)
    store.objects[f"releases/{SHA}.tar.gz.sha256"] = body
    rc, paths, runner, old = _failing_deploy(tmp_path, store)
    _assert_failed(rc, paths, runner, store, old, pd.REASON_CHECKSUM)


@pytest.mark.parametrize("missing", [f"releases/{SHA}.tar.gz", f"releases/{SHA}.tar.gz.sha256"])
def test_missing_archive_or_checksum_keeps_current(tmp_path, missing):
    store = FakeStore()
    publish(store)
    del store.objects[missing]
    rc, paths, runner, old = _failing_deploy(tmp_path, store)
    _assert_failed(rc, paths, runner, store, old, pd.REASON_ARCHIVE_MISSING)


def _member(name, kind, linkname=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    data = None
    if kind == tarfile.REGTYPE:
        data = b"evil\n"
        info.size = len(data)
    return info, data


UNSAFE = {
    "absolute": _member("/tmp/pi-evil", tarfile.REGTYPE),
    "dotdot": _member("../evil", tarfile.REGTYPE),
    "nested-dotdot": _member("infra/../../evil", tarfile.REGTYPE),
    "symlink-absolute": _member("link", tarfile.SYMTYPE, "/etc/passwd"),
    "symlink-escape": _member("infra/link", tarfile.SYMTYPE, "../../outside"),
    "hardlink": _member("hard", tarfile.LNKTYPE, "run_pipeline.sh"),
    "fifo": _member("fifo", tarfile.FIFOTYPE),
    "char-device": _member("dev", tarfile.CHRTYPE),
    "block-device": _member("blk", tarfile.BLKTYPE),
    "file-under-symlink": None,  # built below
    "reserved-marker": _member(".release", tarfile.REGTYPE),
    "duplicate": _member("README.md", tarfile.REGTYPE),
}


@pytest.mark.parametrize("case", sorted(UNSAFE))
def test_unsafe_archive_members_are_rejected_and_nothing_is_written(tmp_path, case):
    if case == "file-under-symlink":
        members = [_member("sub", tarfile.SYMTYPE, "infra"), _member("sub/x", tarfile.REGTYPE)]
    else:
        members = [UNSAFE[case]]
    store = FakeStore()
    publish(store, archive=tar_gz(release_files(), members))
    rc, paths, runner, old = _failing_deploy(tmp_path, store)
    _assert_failed(rc, paths, runner, store, old, pd.REASON_UNSAFE_ARCHIVE)
    assert not (tmp_path / "home" / "evil").exists() and not (tmp_path / "evil").exists()
    assert not Path("/tmp/pi-evil").exists()


def test_in_tree_symlinks_are_allowed(tmp_path):
    members = [_member("docs/link-to-readme", tarfile.SYMTYPE, "../README.md")]
    store = FakeStore()
    publish(store, archive=tar_gz(release_files(), members))
    rc, paths, *_ = run(tmp_path, store)
    assert rc == 0
    link = paths.releases_dir / SHA / "docs" / "link-to-readme"
    assert link.is_symlink() and os.readlink(link) == "../README.md"


@pytest.mark.parametrize("content", [b"not gzip", gzip.compress(b"not a tar", mtime=0), b""])
def test_corrupt_archive_with_a_matching_checksum_is_rejected(tmp_path, content):
    store = FakeStore()
    publish(store, archive=content)
    rc, paths, runner, old = _failing_deploy(tmp_path, store)
    _assert_failed(rc, paths, runner, store, old, pd.REASON_UNSAFE_ARCHIVE)


@pytest.mark.parametrize("missing", list(config.RELEASE_REQUIRED_FILES))
def test_missing_required_file_keeps_current(tmp_path, missing):
    store = FakeStore()
    publish(store, files=release_files({missing: None}))
    rc, paths, runner, old = _failing_deploy(tmp_path, store)
    _assert_failed(rc, paths, runner, store, old, pd.REASON_MISSING_FILE)


def test_required_file_as_a_symlink_is_not_enough(tmp_path):
    files = release_files({"run_pipeline.sh": None})
    members = [_member("run_pipeline.sh", tarfile.SYMTYPE, "README.md")]
    store = FakeStore()
    publish(store, archive=tar_gz(files, members))
    rc, paths, runner, old = _failing_deploy(tmp_path, store)
    _assert_failed(rc, paths, runner, store, old, pd.REASON_MISSING_FILE)


def test_bash_syntax_error_keeps_current(tmp_path):
    store = FakeStore()
    publish(store, files=release_files({"run_pipeline.sh": b"#!/bin/bash\nif then fi (\n"}))
    rc, paths, runner, old = _failing_deploy(tmp_path, store)
    _assert_failed(rc, paths, runner, store, old, pd.REASON_BASH_SYNTAX)
    assert len(runner.named("/bin/bash", "-n")) == 1


def test_pipeline_running_refuses_to_switch(tmp_path):
    store = FakeStore()
    publish(store)
    rc, paths, runner, old = _failing_deploy(tmp_path, store, runner=FakeRunner(pipeline_active=True))
    _assert_failed(rc, paths, runner, store, old, pd.REASON_PIPELINE_RUNNING)


# --- environment.yml -----------------------------------------------------------------------------
NEW_ENV = b"name: wayakit-price-prediction\ndependencies:\n  - python=3.10\n  - pandas=2.3.3\n"


def test_environment_change_is_refused_without_the_flag(tmp_path):
    store = FakeStore()
    publish(store, files=release_files({"environment.yml": NEW_ENV}))
    rc, paths, runner, old = _failing_deploy(tmp_path, store)
    _assert_failed(rc, paths, runner, store, old, pd.REASON_ENV_CHANGED, unpacked=True)
    assert runner.named("runuser") == []


def test_environment_change_with_the_flag_updates_conda_before_the_switch(tmp_path):
    store = FakeStore()
    publish(store, files=release_files({"environment.yml": NEW_ENV}))
    paths = make_paths(tmp_path)
    old = install_active(paths, OLD)
    at_conda = []
    runner = FakeRunner(on_call=lambda argv: at_conda.append(target(paths)) if argv[0] == "runuser" else None)
    rc, *_ = run(tmp_path, store, "--allow-env-update", paths=paths, runner=runner)
    assert rc == 0
    conda = runner.named("runuser")
    assert conda == [["runuser", "-u", "ec2-user", "--", "/home/ec2-user/miniconda/bin/conda", "env", "update",
                      "-n", "wayakit_env", "-f", str(paths.releases_dir / SHA / "environment.yml")]]
    assert at_conda == [old], "conda runs while the old release is still active"
    assert target(paths) == paths.releases_dir / SHA


def test_failed_conda_update_keeps_current(tmp_path):
    store = FakeStore()
    publish(store, files=release_files({"environment.yml": NEW_ENV}))
    rc, paths, runner, old = _failing_deploy(tmp_path, store, runner=FakeRunner(fail=[("runuser",)]),
                                             argv=("--allow-env-update",))
    _assert_failed(rc, paths, runner, store, old, pd.REASON_CONDA, unpacked=True)


def test_same_environment_never_calls_conda(tmp_path):
    store = FakeStore()
    publish(store)
    paths = make_paths(tmp_path)
    install_active(paths, OLD)
    rc, _, runner, _ = run(tmp_path, store, "--allow-env-update", paths=paths)
    assert rc == 0 and runner.named("runuser") == []


# =============================================================================================
# Legacy folder (first F4 install)
# =============================================================================================
def _legacy(paths):
    paths.app_link.mkdir(parents=True)
    (paths.app_link / ".git").mkdir()
    (paths.app_link / "scraper").mkdir()
    (paths.app_link / "scraper" / "competitors_complete.csv").write_text("kept\n")
    (paths.app_link / "environment.yml").write_bytes(release_files()["environment.yml"])


def test_legacy_folder_is_moved_to_pre_f4_and_replaced_by_the_symlink(tmp_path):
    paths = make_paths(tmp_path)
    _legacy(paths)
    store = FakeStore()
    publish(store)
    request(store)
    rc, _, runner, _ = run(tmp_path, store, paths=paths)
    assert rc == 0
    backup = paths.app_link.with_name("wayakit-price-prediction.pre-f4")
    assert backup.is_dir() and not backup.is_symlink()
    assert (backup / "scraper" / "competitors_complete.csv").read_text() == "kept\n"
    assert target(paths) == paths.releases_dir / SHA
    assert store.result()["previous"] is None


def test_legacy_folder_with_an_existing_backup_is_refused(tmp_path):
    paths = make_paths(tmp_path)
    _legacy(paths)
    backup = paths.app_link.with_name("wayakit-price-prediction.pre-f4")
    backup.mkdir()
    store = FakeStore()
    publish(store)
    request(store)
    rc, _, runner, _ = run(tmp_path, store, paths=paths)
    assert rc == 1
    assert paths.app_link.is_dir() and not paths.app_link.is_symlink()
    assert (paths.app_link / "scraper" / "competitors_complete.csv").exists()
    assert store.result()["reason"] == pd.REASON_LEGACY_BACKUP_EXISTS
    assert not leftovers(paths)


def test_legacy_folder_is_put_back_if_the_switch_fails(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    _legacy(paths)
    store = FakeStore()
    publish(store)
    request(store)
    real_replace = os.replace

    def failing(src, dst, *args, **kwargs):
        if Path(dst) == paths.app_link:
            raise OSError(5, "Input/output error")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(pd.os, "replace", failing)
    rc, _, runner, _ = run(tmp_path, store, paths=paths)
    assert rc == 1
    assert paths.app_link.is_dir() and not paths.app_link.is_symlink()
    assert (paths.app_link / "scraper" / "competitors_complete.csv").read_text() == "kept\n"
    assert not paths.app_link.with_name("wayakit-price-prediction.pre-f4").exists()
    assert store.result()["reason"] == pd.REASON_SWITCH
    assert not leftovers(paths)
    assert runner.powered_off


def test_switch_failure_on_a_symlink_keeps_the_old_target(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    old = install_active(paths, OLD)
    store = FakeStore()
    publish(store)
    request(store)
    real_replace = os.replace
    monkeypatch.setattr(pd.os, "replace", lambda s, d, *a, **k: (_ for _ in ()).throw(OSError(28, "No space"))
                        if Path(d) == paths.app_link else real_replace(s, d, *a, **k))
    rc, _, runner, _ = run(tmp_path, store, paths=paths)
    assert rc == 1
    assert_untouched(paths, old)
    assert store.result()["reason"] == pd.REASON_SWITCH


# =============================================================================================
# Pointer and request
# =============================================================================================
def test_missing_pointer_on_a_normal_boot_is_ok(tmp_path, capsys):
    store = FakeStore()
    rc, paths, runner, out = run(tmp_path, store, capsys=capsys)
    assert rc == 0
    assert "no release pointer yet" in out
    assert not paths.app_link.exists() and store.puts == [] and not runner.powered_off


def test_missing_pointer_on_a_deploy_boot_fails_and_powers_off(tmp_path):
    store = FakeStore()
    request(store)
    rc, paths, runner, _ = run(tmp_path, store)
    assert rc == 1
    assert store.result()["result"] == "failed" and store.result()["reason"] == pd.REASON_NO_POINTER
    assert runner.powered_off


@pytest.mark.parametrize("pointer", [b"", b"HEAD\n", b"A" * 40, b"a" * 39, b"\xff" * 40])
def test_invalid_pointer_fails(tmp_path, pointer):
    store = FakeStore({"releases/current": pointer})
    rc, paths, runner, _ = run(tmp_path, store)
    assert rc == 1 and not paths.app_link.exists() and not runner.powered_off


def test_pointer_that_differs_from_the_request_fails(tmp_path):
    store = FakeStore()
    publish(store, sha=OLD)
    request(store, sha=SHA)
    rc, paths, runner, _ = run(tmp_path, store)
    assert rc == 1
    assert store.result()["reason"] == pd.REASON_REQUEST_MISMATCH and store.result()["sha"] == SHA
    assert not paths.app_link.exists() and runner.powered_off


@pytest.mark.parametrize("change", [
    pytest.param(lambda s: request(s, at=NOW - timedelta(minutes=31)), id="expired"),
    pytest.param(lambda s: request(s, at=NOW + timedelta(minutes=10)), id="not-yet-valid"),
    pytest.param(lambda s: request(s, closed=True), id="closed"),
    pytest.param(lambda s: s.objects.__setitem__("releases/deploy-request.json", b"{not json"), id="garbage"),
    pytest.param(lambda s: (request(s), s.objects.__setitem__(f"state/deploys/{RID}.json", b"{}")), id="handled"),
])
def test_a_request_that_is_not_valid_now_means_a_normal_boot(tmp_path, change):
    """A manual start after a deploy (expired, closed or already handled request) must never be
    powered off by the deployer."""
    store = FakeStore()
    publish(store)
    change(store)
    before = dict(store.objects)
    rc, paths, runner, _ = run(tmp_path, store)
    assert rc == 0
    assert target(paths) == paths.releases_dir / SHA
    assert not runner.powered_off
    assert store.puts == [] and store.objects == before


def test_request_expiry_boundary(tmp_path):
    """Valid while requested_at <= now < expires_at."""
    store = FakeStore()
    publish(store)
    t = int(NOW.timestamp())
    store.objects["releases/deploy-request.json"] = json.dumps(
        {"sha": SHA, "request_id": RID, "requested_at": t - 1800, "expires_at": t}).encode()
    rc, _, runner, _ = run(tmp_path, store)
    assert rc == 0 and not runner.powered_off and store.result() is None
    store.objects["releases/deploy-request.json"] = json.dumps(
        {"sha": SHA, "request_id": RID, "requested_at": t - 1799, "expires_at": t + 1}).encode()
    rc, _, runner, _ = run(tmp_path, store)
    assert rc == 0 and runner.powered_off and store.result()["result"] == "ok"


# =============================================================================================
# Power-off: only after a deploy boot, never inside the monthly window
# =============================================================================================
def test_deploy_boot_inside_the_monthly_window_never_powers_off(tmp_path):
    store = FakeStore()
    publish(store)
    request(store, at=WINDOW_NOW)
    assert guard.is_scheduled_window(WINDOW_NOW)
    rc, _, runner, _ = run(tmp_path, store, now=WINDOW_NOW)
    assert rc == 0 and store.result()["result"] == "ok"
    assert not runner.powered_off
    assert not any("poweroff" in " ".join(c) for c in runner.calls)


def test_failed_deploy_boot_inside_the_window_never_powers_off(tmp_path):
    store = FakeStore()
    publish(store, checksum="0" * 64)
    request(store, at=WINDOW_NOW)
    rc, _, runner, _ = run(tmp_path, store, now=WINDOW_NOW)
    assert rc == 1 and not runner.powered_off


def test_no_poweroff_flag(tmp_path):
    store = FakeStore()
    publish(store)
    request(store)
    rc, _, runner, _ = run(tmp_path, store, "--no-poweroff")
    assert rc == 0 and not runner.powered_off and store.result()["result"] == "ok"


@pytest.mark.parametrize("deploy_mode", [True, False])
@pytest.mark.parametrize("now, inside", [(NOW, False), (WINDOW_NOW, True)])
@pytest.mark.parametrize("no_poweroff", [True, False])
def test_should_power_off_truth_table(deploy_mode, now, inside, no_poweroff):
    assert pd.should_power_off(deploy_mode, now, no_poweroff) == (deploy_mode and not no_poweroff and not inside)


def test_failed_poweroff_command_makes_the_run_fail(tmp_path):
    store = FakeStore()
    publish(store)
    request(store)
    rc, _, runner, _ = run(tmp_path, store, runner=FakeRunner(fail=[("systemctl", "poweroff")]))
    assert rc == 1 and store.result()["result"] == "ok"


def test_store_error_reading_the_request_is_a_failed_normal_boot(tmp_path):
    """Documented behaviour: if the request still cannot be read after the 6 attempts (F4 lead
    finding: retries added), the boot cannot know it is a deploy boot, so it neither writes a
    result nor powers off (release.py times out, restores the pointer and tells the operator to
    stop the instance). The run fails (exit 1) and the alert is sent."""
    store = FakeStore(fail={"releases/deploy-request.json"})
    publish(store)
    rc, paths, runner, _ = run(tmp_path, store, environ={"PI_ALERTS_TOPIC_ARN": TOPIC})
    assert rc == 1
    assert runner.sleeps == [5, 10, 20, 30, 40]
    assert store.gets.count("releases/deploy-request.json") == 6
    assert "releases/current" not in store.gets, "nothing else is read after the request failed"
    assert not runner.powered_off and store.puts == []
    assert len(runner.named("aws", "sns", "publish")) == 1


def test_failed_result_write_makes_the_run_fail_but_still_powers_off(tmp_path):
    store = FakeStore(fail_put={f"state/deploys/{RID}.json"})
    publish(store)
    request(store)
    rc, _, runner, _ = run(tmp_path, store)
    assert rc == 1 and runner.powered_off


# =============================================================================================
# A deploy-only boot never runs the pipeline
# =============================================================================================
def test_the_deployer_never_starts_the_pipeline_or_any_unit(tmp_path):
    store = FakeStore()
    publish(store)
    request(store)
    rc, _, runner, _ = run(tmp_path, store)
    assert rc == 0
    for argv in runner.calls:
        text = " ".join(argv)
        if argv[:2] == ["/bin/bash", "-n"]:
            continue
        assert "run_pipeline.sh" not in text, argv
        if argv[0] == "systemctl":
            assert argv[1] in ("is-active", "daemon-reload", "poweroff"), argv


def _unit(path):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(path.read_text(encoding="utf-8"))
    return parser


DEPLOY_UNIT = INSTANCE_DIR / "pi-deploy.service"
PIPELINE_UNIT = INSTANCE_DIR / "pi-pipeline.service"
HARD_DEPS = ("Requires", "BindsTo", "Requisite", "PartOf", "Upholds", "OnSuccess", "OnFailure", "Conflicts")


def test_deploy_unit_keys():
    unit = _unit(DEPLOY_UNIT)
    assert unit.sections() == ["Unit", "Service", "Install"]
    assert dict(unit["Unit"]) == {"Description": "Price Intelligence release install (pull-on-boot, F4)",
                                  "Wants": "network-online.target", "After": "network-online.target",
                                  "Before": "pi-pipeline.service"}
    assert dict(unit["Service"]) == {
        "Type": "oneshot", "User": "root", "EnvironmentFile": "-/etc/price-intelligence.env",
        "ExecStart": f"/usr/bin/python3 {config.INSTANCE_DEPLOYER_DIR}/pi_deploy.py",
        "TimeoutStartSec": "10min"}
    assert dict(unit["Install"]) == {"WantedBy": "multi-user.target"}
    assert config.DEPLOY_SERVICE == DEPLOY_UNIT.name


def test_units_are_only_ordered_never_hard_dependent():
    """A failed install must not block the monthly run; and the deploy unit never pulls the
    pipeline in. After/Before only."""
    deploy, pipeline = _unit(DEPLOY_UNIT), _unit(PIPELINE_UNIT)
    assert "pi-pipeline.service" in deploy["Unit"]["Before"].split()
    assert "pi-deploy.service" in pipeline["Unit"]["After"].split()
    for unit in (deploy, pipeline):
        for section in unit.sections():
            for key in HARD_DEPS:
                assert key not in unit[section], (section, key)
    assert "pi-pipeline" not in deploy["Service"]["ExecStart"]
    assert "run_pipeline.sh" not in "".join(line for line in DEPLOY_UNIT.read_text().splitlines()
                                            if not line.startswith("#"))
    assert not any(k.startswith(("ExecStartPost", "ExecStop", "Restart")) for k in deploy["Service"])


def test_pipeline_unit_keeps_the_boot_guard_as_exec_condition():
    service = _unit(PIPELINE_UNIT)["Service"]
    assert service["ExecCondition"] == f"/usr/bin/python3 {config.INSTANCE_APP_DIR}/infra/instance/pi_boot_guard.py"
    assert service["ExecStart"] == f"/bin/bash {config.INSTANCE_APP_DIR}/run_pipeline.sh"


@pytest.mark.parametrize("when", [NOW, NOW + timedelta(minutes=10), WINDOW_NOW - timedelta(minutes=31)])
def test_deploy_boot_time_outside_the_window_makes_the_guard_skip_the_pipeline(when):
    """The pipeline unit runs after pi-deploy.service; on a deploy boot (outside the window, which
    release.py enforces) its ExecCondition exits 1 = skip ExecStart."""
    rc = guard.main(["--now", when.isoformat()])
    assert rc == guard.EXIT_OUTSIDE


# =============================================================================================
# AWS CLI store: exact flags, error handling, redaction
# =============================================================================================
class FakeAwsCli:
    """Runner answering `aws` argv lists from an in-memory bucket; other commands as FakeRunner."""

    def __init__(self, objects=None, *, fail_ops=(), stderr=None, base=None, sts_failures=0):
        self.objects = dict(objects or {})
        self.fail_ops = set(fail_ops)
        self.sts_failures = sts_failures  # STS answers Throttling this many times first
        self.timeouts = []
        self.stderr = stderr or (f"An error occurred (AccessDenied) when calling the GetObject operation: "
                                 f"User: arn:aws:sts::{ACCOUNT}:assumed-role/x/i-0123456789abcdef0 is not "
                                 f"authorized on arn:aws:s3:::{BUCKET}/releases/current")
        self.base = base or FakeRunner()
        self.aws_calls = []

    @property
    def calls(self):
        return self.base.calls

    def __call__(self, argv, timeout=None):
        argv = [str(a) for a in argv]
        if argv[0] != "aws":
            return self.base(argv, timeout)
        self.base.calls.append(argv)
        self.aws_calls.append(argv)
        self.timeouts.append((argv[1], argv[2], argv[argv.index("--key") + 1] if "--key" in argv else None, timeout))
        if argv[1:3] == ["sts", "get-caller-identity"]:
            if self.sts_failures > 0:
                self.sts_failures -= 1
                return subprocess.CompletedProcess(
                    argv, 255, "", f"An error occurred (Throttling) when calling the GetCallerIdentity operation: "
                                   f"Rate exceeded for arn:aws:sts::{ACCOUNT}:assumed-role/x")
            return subprocess.CompletedProcess(argv, 0, ACCOUNT + "\n", "")
        if argv[1] == "sns":
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        op = argv[2]
        key = argv[argv.index("--key") + 1]
        if op in self.fail_ops:
            return subprocess.CompletedProcess(argv, 254, "", self.stderr)
        if op == "get-object":
            if key not in self.objects:
                return subprocess.CompletedProcess(
                    argv, 254, "", "An error occurred (NoSuchKey) when calling the GetObject operation: x")
            Path(argv[argv.index("--key") + 2]).write_bytes(self.objects[key])
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if op == "put-object":
            self.objects[key] = Path(argv[argv.index("--body") + 1]).read_bytes()
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        raise AssertionError(argv)


def _cli_run(tmp_path, cli, capsys, *argv, environ=None):
    paths = make_paths(tmp_path)
    rc = pd.main(list(argv), store=None, now=NOW, paths=paths, runner=cli, environ=environ or {},
                 sleep=cli.base.sleeps.append)
    out = capsys.readouterr()
    log = (paths.log_dir / "deploy.log").read_text() if (paths.log_dir / "deploy.log").exists() else ""
    return rc, paths, out.out + out.err, log


def _assert_no_account_or_bucket(text):
    assert ACCOUNT not in text, text
    assert BUCKET not in text, text
    assert "arn:aws" not in text, text
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", text), text


def test_aws_cli_store_happy_path_flags_and_redaction(tmp_path, capsys):
    store = FakeStore()
    publish(store)
    request(store)
    cli = FakeAwsCli(store.objects)
    rc, paths, out, log = _cli_run(tmp_path, cli, capsys)
    assert rc == 0, out
    assert target(paths) == paths.releases_dir / SHA
    s3 = [c for c in cli.aws_calls if c[1] == "s3api"]
    assert s3, "S3 was used"
    for argv in s3:
        assert argv[argv.index("--bucket") + 1] == BUCKET
        assert argv[argv.index("--region") + 1] == "eu-north-1"
        assert argv[argv.index("--expected-bucket-owner") + 1] == ACCOUNT
    sts = [c for c in cli.aws_calls if c[1] == "sts"]
    assert len(sts) == 1 and sts[0][sts[0].index("--region") + 1] == "eu-north-1"
    assert f"state/deploys/{RID}.json" in cli.objects
    _assert_no_account_or_bucket(out)
    _assert_no_account_or_bucket(log)


def test_aws_cli_errors_print_only_the_operation_exit_code_and_error_code(tmp_path, capsys):
    store = FakeStore()
    publish(store)
    cli = FakeAwsCli(store.objects, fail_ops={"get-object"})
    rc, paths, out, log = _cli_run(tmp_path, cli, capsys, environ={"PI_ALERTS_TOPIC_ARN": TOPIC})
    assert rc == 1
    assert "aws s3api get-object failed (exit 254, AccessDenied)" in out
    for text in (out, log):
        _assert_no_account_or_bucket(text)
        assert "not authorized" not in text and "i-0123456789abcdef0" not in text
    alert = [c for c in cli.aws_calls if c[1] == "sns"]
    assert len(alert) == 1
    message = alert[0][alert[0].index("--message") + 1]
    assert message == f"Price Intelligence: release install on the instance failed: {pd.REASON_STORE}"
    _assert_no_account_or_bucket(message)


def test_explicit_bucket_is_used_and_redacted(tmp_path, capsys):
    other = "my-other-bucket-name"
    store = FakeStore()
    cli = FakeAwsCli(store.objects)
    rc, _, out, log = _cli_run(tmp_path, cli, capsys, "--bucket", other)
    assert rc == 0
    assert all(c[c.index("--bucket") + 1] == other for c in cli.aws_calls if c[1] == "s3api")
    assert other not in out + log


@pytest.mark.parametrize("answer", ["", "12345", "not-an-account", f"{ACCOUNT}0"])
def test_unexpected_sts_answer_fails_without_printing_it(tmp_path, capsys, answer):
    cli = FakeAwsCli()

    def sts_runner(argv, timeout=None):
        if argv[:3] == ["aws", "sts", "get-caller-identity"]:
            return subprocess.CompletedProcess(argv, 0, answer + "\n", "")
        return cli(argv, timeout)

    paths = make_paths(tmp_path)
    rc = pd.main([], store=None, now=NOW, paths=paths, runner=sts_runner, environ={},
                 sleep=cli.base.sleeps.append)
    out = capsys.readouterr().out
    assert rc == 1
    assert "UnexpectedOutput" in out
    assert (answer not in out) if answer else True


@pytest.mark.parametrize("stderr, code", [
    ("An error occurred (AccessDenied) when calling the GetObject operation: arn:aws:iam::000000000000:x", "AccessDenied"),
    ("An error occurred (NoSuchKey) when calling", "NoSuchKey"),
    ("Could not connect to the endpoint URL", ""),
    ("", ""),
    (None, ""),
    ("An error occurred (Bad Code!) x", ""),
])
def test_aws_error_code(stderr, code):
    assert pd.aws_error_code(stderr) == code


def test_alert_never_contains_the_topic_in_the_output(tmp_path, capsys):
    store = FakeStore()
    publish(store, checksum="0" * 64)
    request(store)
    rc, paths, runner, out = run(tmp_path, store, environ={"PI_ALERTS_TOPIC_ARN": TOPIC}, capsys=capsys)
    assert rc == 1
    sns = runner.named("aws", "sns", "publish")
    assert len(sns) == 1
    argv = sns[0]
    assert argv[argv.index("--topic-arn") + 1] == TOPIC
    assert argv[argv.index("--region") + 1] == "eu-north-1"
    assert pd.REASON_CHECKSUM in argv[argv.index("--message") + 1]
    log = (paths.log_dir / "deploy.log").read_text()
    assert TOPIC not in out + log and ACCOUNT not in out + log


def test_no_alert_on_success(tmp_path):
    store = FakeStore()
    publish(store)
    request(store)
    _, _, runner, _ = run(tmp_path, store, environ={"PI_ALERTS_TOPIC_ARN": TOPIC})
    assert runner.named("aws") == []


# =============================================================================================
# Dry-run, lock, arguments, folders
# =============================================================================================
@pytest.mark.parametrize("with_request", [True, False])
def test_dry_run_reads_only(tmp_path, capsys, with_request):
    store = FakeStore()
    publish(store)
    if with_request:
        request(store)
    paths = make_paths(tmp_path)
    old = install_active(paths, OLD)
    snapshot = sorted(str(p) for p in tmp_path.rglob("*"))
    rc, _, runner, out = run(tmp_path, store, "--dry-run", paths=paths, capsys=capsys)
    assert rc == 0, out
    assert store.puts == [] and not runner.powered_off
    assert [c for c in runner.calls if c[:2] != ["/bin/bash", "-n"]] == []
    assert sorted(str(p) for p in tmp_path.rglob("*")) == snapshot, "nothing created or removed"
    assert target(paths) == old
    assert "[dry-run] would" in out


def test_a_second_concurrent_run_is_refused(tmp_path):
    paths = make_paths(tmp_path)
    paths.releases_dir.mkdir(parents=True)
    store = FakeStore()
    publish(store)
    with open(paths.releases_dir / ".deploy.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, *_ = run(tmp_path, store, paths=paths)
    assert rc == 1
    assert not paths.app_link.exists()
    assert pd.REASON_LOCKED in (paths.log_dir / "deploy.log").read_text()


@pytest.mark.parametrize("argv", [["--sha", "A" * 40], ["--sha", "a" * 39], ["--bucket", "Bad_Bucket"],
                                  ["--bogus"]])
def test_bad_arguments_exit_1_not_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        pd.main(argv, store=FakeStore(), now=NOW, paths=None, runner=FakeRunner(), environ={}, sleep=lambda _: None)
    assert exc.value.code == 1


def test_manual_sha_installs_that_release_and_is_never_deploy_mode(tmp_path):
    store = FakeStore()
    publish(store, sha=OLD, pointer=False)
    publish(store, sha=SHA)
    request(store, sha=SHA)
    rc, paths, runner, _ = run(tmp_path, store, "--sha", OLD)
    assert rc == 0
    assert target(paths) == paths.releases_dir / OLD
    assert not runner.powered_off and store.puts == []


def test_naive_now_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        pd.main([], store=FakeStore(), now=datetime(2026, 11, 10, 15, 0), paths=make_paths(tmp_path),
                runner=FakeRunner(), environ={}, sleep=lambda _: None)


@pytest.mark.parametrize("value", ["relative/logs", "/home/ec2-user/../etc", "logs"])
def test_log_dir_must_be_absolute_without_dotdot(value):
    with pytest.raises(pd.DeployError):
        pd.Paths.from_environ({"PI_LOG_DIR": value})


def test_log_dir_default_and_override():
    assert pd.Paths.from_environ({}).log_dir == Path("/home/ec2-user/logs")
    assert pd.Paths.from_environ({"PI_LOG_DIR": "  "}).log_dir == Path("/home/ec2-user/logs")
    assert pd.Paths.from_environ({"PI_LOG_DIR": "/var/pi/logs"}).log_dir == Path("/var/pi/logs")
    default = pd.Paths()
    assert (str(default.app_link), str(default.releases_dir), str(default.deployer_dir),
            str(default.systemd_dir)) == (config.INSTANCE_APP_DIR, config.INSTANCE_RELEASES_DIR,
                                          config.INSTANCE_DEPLOYER_DIR, "/etc/systemd/system")
    assert str(default.legacy_backup) == config.INSTANCE_APP_DIR + ".pre-f4"


def test_new_folders_are_given_to_ec2_user(tmp_path):
    store = FakeStore()
    rc, paths, runner, _ = run(tmp_path, store)
    assert rc == 0
    owned = [c[-1] for c in runner.named("chown")]
    assert str(paths.releases_dir) in owned and str(paths.log_dir) in owned


def test_a_release_whose_deployer_does_not_compile_fails_after_the_switch(tmp_path):
    """Documented: the system files are synced after the switch; a broken pi_deploy.py fails the
    deploy (release.py then moves the pointer back and the next boot switches back) and the
    deployer in /opt is left as it was."""
    paths = make_paths(tmp_path)
    install_active(paths, OLD)
    paths.deployer_dir.mkdir(parents=True)
    (paths.deployer_dir / "pi_deploy.py").write_bytes(SYSTEM_FILES["infra/instance/pi_deploy.py"])
    store = FakeStore()
    publish(store, files=release_files({"infra/instance/pi_deploy.py": b"def broken(:\n"}))
    request(store)
    rc, _, runner, _ = run(tmp_path, store, paths=paths)
    assert rc == 1
    assert store.result()["reason"] == pd.REASON_SYSTEM_FILES
    assert (paths.deployer_dir / "pi_deploy.py").read_bytes() == SYSTEM_FILES["infra/instance/pi_deploy.py"]
    assert runner.powered_off


# =============================================================================================
# Standalone file: stdlib only, Python 3.9, same constants as infra.config
# =============================================================================================
def test_constants_match_config():
    assert pd.REGION == config.REGION
    assert pd.BUCKET_PREFIX + ACCOUNT == config.bucket_name(ACCOUNT)
    assert pd.RELEASES_PREFIX == config.RELEASES_PREFIX
    assert pd.RELEASES_CURRENT_KEY == config.RELEASES_CURRENT_KEY
    assert pd.DEPLOY_REQUEST_KEY == config.DEPLOY_REQUEST_KEY
    assert pd.DEPLOYS_STATE_PREFIX == config.DEPLOYS_STATE_PREFIX
    assert pd.RELEASES_TO_KEEP == config.RELEASES_TO_KEEP
    assert pd.APP_DIR == config.INSTANCE_APP_DIR
    assert pd.RELEASES_DIR == config.INSTANCE_RELEASES_DIR
    assert pd.DEPLOYER_DIR == config.INSTANCE_DEPLOYER_DIR
    assert pd.DEPLOY_SERVICE == config.DEPLOY_SERVICE
    assert pd.PIPELINE_SERVICE == config.PIPELINE_SERVICE
    assert pd.CONDA_ENV == config.CONDA_ENV
    assert pd.ENV_LOG_DIR == config.ENV_LOG_DIR
    assert pd.ENV_ALERTS_TOPIC_ARN == config.ENV_ALERTS_TOPIC_ARN
    assert pd.RELEASE_REQUIRED_FILES == config.RELEASE_REQUIRED_FILES
    from infra import ec2_instance
    assert pd.APP_DIR == ec2_instance.REPO_DIR and pd.DEFAULT_LOG_DIR == ec2_instance.DEFAULT_LOG_DIR


def test_request_id_rule_matches_release_py():
    from infra import release
    assert pd.REQUEST_ID_RE.pattern == release.REQUEST_ID_PATTERN.pattern
    assert pd.SHA_RE.pattern == release.SHA_PATTERN.pattern


STDLIB = {"__future__", "argparse", "errno", "fcntl", "hashlib", "json", "math", "os", "posixpath", "re",
          "shutil", "stat", "subprocess", "sys", "tarfile", "tempfile", "time", "dataclasses", "datetime", "pathlib"}


def test_deployer_is_stdlib_only_and_python39_syntax():
    source = (INSTANCE_DIR / "pi_deploy.py").read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= STDLIB | {"pi_boot_guard"}, imported - STDLIB
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", source)
    assert "boto" not in source


def test_deployer_runs_standalone_from_its_own_folder(tmp_path):
    """Installed as /opt/price-intelligence/pi_deploy.py next to pi_boot_guard.py, outside the repo."""
    opt = tmp_path / "opt"
    opt.mkdir()
    shutil.copy(INSTANCE_DIR / "pi_deploy.py", opt)
    shutil.copy(INSTANCE_DIR / "pi_boot_guard.py", opt)
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    r = subprocess.run([sys.executable, "-E", "-s", str(opt / "pi_deploy.py"), "--help"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    assert "--allow-env-update" in r.stdout
    r = subprocess.run([sys.executable, "-E", "-s", str(opt / "pi_deploy.py"), "--bogus"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 1


def test_deployer_files_are_valid_for_systemd():
    assert os.access(INSTANCE_DIR / "pi_deploy.py", os.R_OK)
    assert (INSTANCE_DIR / "pi_deploy.py").read_text().startswith("#!/usr/bin/env python3\n")


# =============================================================================================
# Retries (F4 lead finding): STS, the request and the "already handled" result read
# =============================================================================================
def test_retry_constants():
    assert pd.READ_ATTEMPTS == 6
    assert pd.RETRY_DELAYS_SECONDS == (5, 10, 20, 30, 40)
    assert len(pd.RETRY_DELAYS_SECONDS) == pd.READ_ATTEMPTS - 1
    # 105 s of waiting at most, well inside the 30-min request TTL and the 10-min unit timeout.
    assert sum(pd.RETRY_DELAYS_SECONDS) == 105
    assert sum(pd.RETRY_DELAYS_SECONDS) * 2 + pd.READ_ATTEMPTS * 2 * pd.AWS_SMALL_TIMEOUT_SECONDS < 600


def test_flaky_request_read_recovers_into_a_normal_deploy(tmp_path):
    store = FakeStore(flaky={"releases/deploy-request.json": 3})
    publish(store)
    request(store)
    rc, paths, runner, _ = run(tmp_path, store)
    assert rc == 0
    assert runner.sleeps == [5, 10, 20]
    assert store.result()["result"] == "ok" and runner.powered_off
    log = (paths.log_dir / "deploy.log").read_text()
    assert "reading the deploy request: aws s3api get-object failed (exit 254, SlowDown); attempt 1/6, retrying in 5 s" in log
    assert "attempt 3/6, retrying in 20 s" in log and "attempt 4/6" not in log


def test_missing_request_is_not_retried(tmp_path):
    store = FakeStore()
    publish(store)
    rc, _, runner, _ = run(tmp_path, store)
    assert rc == 0 and runner.sleeps == []
    assert store.gets.count("releases/deploy-request.json") == 1


def test_flaky_handled_result_read_recovers(tmp_path):
    store = FakeStore(flaky={f"state/deploys/{RID}.json": 2})
    publish(store)
    request(store)
    rc, _, runner, _ = run(tmp_path, store)
    assert rc == 0
    assert runner.sleeps == [5, 10]
    assert store.result()["result"] == "ok" and runner.powered_off


@pytest.mark.parametrize("now, poweroff", [(NOW, True), (WINDOW_NOW, False)], ids=["outside", "inside-window"])
def test_handled_result_unreadable_is_a_failed_deploy_boot(tmp_path, now, poweroff):
    """A valid open request whose "already handled" check cannot be read after 6 attempts: a
    failed deploy boot (result put attempted, alert, power-off outside the window)."""
    store = FakeStore(fail={f"state/deploys/{RID}.json"})
    publish(store)
    request(store, at=now)
    paths = make_paths(tmp_path)
    old = install_active(paths, OLD)
    rc, _, runner, _ = run(tmp_path, store, now=now, paths=paths, environ={"PI_ALERTS_TOPIC_ARN": TOPIC})
    assert rc == 1
    assert runner.sleeps == [5, 10, 20, 30, 40]
    assert_untouched(paths, old)
    result = store.result()
    assert result["result"] == "failed" and result["reason"] == pd.REASON_STORE and result["sha"] == SHA
    assert runner.powered_off is poweroff
    assert len(runner.named("aws", "sns", "publish")) == 1


def test_handled_result_unreadable_and_unwritable_still_powers_off(tmp_path):
    key = f"state/deploys/{RID}.json"
    store = FakeStore(fail={key}, fail_put={key})
    publish(store)
    request(store)
    rc, _, runner, _ = run(tmp_path, store, "--no-poweroff")
    assert rc == 1 and not runner.powered_off  # --no-poweroff wins
    rc, _, runner, _ = run(tmp_path, store)
    assert rc == 1 and runner.powered_off


@pytest.mark.parametrize("key", ["releases/current", f"releases/{SHA}.tar.gz", f"releases/{SHA}.tar.gz.sha256"])
def test_pointer_and_archive_reads_are_not_retried(tmp_path, key):
    store = FakeStore(fail={key})
    publish(store)
    request(store)
    rc, _, runner, _ = run(tmp_path, store)
    assert rc == 1 and runner.sleeps == []
    assert store.gets.count(key) == 1
    assert store.result()["reason"] == pd.REASON_STORE and runner.powered_off


def test_result_put_is_not_retried(tmp_path):
    key = f"state/deploys/{RID}.json"
    store = FakeStore(fail_put={key})
    publish(store)
    request(store)
    rc, _, runner, _ = run(tmp_path, store)
    assert rc == 1 and runner.sleeps == []


def test_sts_throttling_is_retried_and_never_printed(tmp_path, capsys):
    store = FakeStore()
    publish(store)
    request(store)
    cli = FakeAwsCli(store.objects, sts_failures=2)
    rc, paths, out, log = _cli_run(tmp_path, cli, capsys)
    assert rc == 0, out
    assert cli.base.sleeps == [5, 10]
    assert len([c for c in cli.aws_calls if c[1] == "sts"]) == 3
    assert "sts get-caller-identity: aws sts get-caller-identity failed (exit 255, Throttling); attempt 1/6" in out
    for text in (out, log):
        _assert_no_account_or_bucket(text)
        assert "Rate exceeded" not in text


def test_sts_failing_every_attempt_is_a_failed_normal_boot(tmp_path, capsys):
    store = FakeStore()
    publish(store)
    request(store)
    cli = FakeAwsCli(store.objects, sts_failures=99)
    rc, paths, out, log = _cli_run(tmp_path, cli, capsys, environ={"PI_ALERTS_TOPIC_ARN": TOPIC})
    assert rc == 1
    assert cli.base.sleeps == [5, 10, 20, 30, 40]
    assert [c for c in cli.aws_calls if c[1] == "s3api"] == []
    assert not cli.base.powered_off and not paths.app_link.exists()
    assert len([c for c in cli.aws_calls if c[1] == "sns"]) == 1
    _assert_no_account_or_bucket(out + log)


def test_aws_cli_timeouts_small_objects_and_archives(tmp_path, capsys):
    store = FakeStore()
    publish(store)
    request(store)
    cli = FakeAwsCli(store.objects)
    rc, *_ = _cli_run(tmp_path, cli, capsys)
    assert rc == 0
    for service, op, key, timeout in cli.timeouts:
        if service == "sts" or (key is not None and ".tar.gz" not in key):
            assert timeout == pd.AWS_SMALL_TIMEOUT_SECONDS == 20, (op, key, timeout)
        elif key is not None:
            assert timeout == pd.AWS_TIMEOUT_SECONDS == 300, (op, key, timeout)
    assert {k for _, _, k, t in cli.timeouts if t == 300} == {f"releases/{SHA}.tar.gz", f"releases/{SHA}.tar.gz.sha256"}


def test_with_retries_only_retries_store_errors():
    log, sleeps = [], []

    def other():
        raise pd.DeployError(pd.REASON_COMMAND, "x")

    with pytest.raises(pd.DeployError) as exc:
        pd.with_retries(other, "w", log.append, sleeps.append)
    assert exc.value.reason == pd.REASON_COMMAND and sleeps == [] and log == []

    def oserror():
        raise OSError(5, "io")

    with pytest.raises(OSError):
        pd.with_retries(oserror, "w", log.append, sleeps.append)
    assert sleeps == []
    assert pd.with_retries(lambda: False, "w", log.append, sleeps.append) is False  # "not found" is a value
    assert sleeps == []
