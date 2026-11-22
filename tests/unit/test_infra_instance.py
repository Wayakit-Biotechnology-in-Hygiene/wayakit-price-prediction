"""Unit tests of the instance and run model (F3): `infra/ec2_instance.py` and `infra/instance/`.

No test reaches AWS and none touches this machine's crontab, systemd or packages:

- `ec2_instance.py`: `botocore.stub.Stubber` on real clients built with fake credentials in
  eu-north-1 checks the exact operations, their order and their parameters (and validates every
  request and response against the EC2/IAM/STS service models). `FakeAws` is a small stateful
  double used for the invariants that need several runs (a second apply only reads, dry-run never
  writes, rollback then adopt converges).
- `pi_boot_guard.py`: pure function with an injected clock, `main(argv=[], now=...)`, and the file
  run as a subprocess with the current interpreter in isolated mode.
- `pi-pipeline.service`: parsed statically with configparser.
- `bootstrap.sh`: sourced in `bash --noprofile --norc` (sourcing only defines functions). The pure
  functions run with an EMPTY `PATH` (only builtins exist). The functions with side effects run
  with a `PATH` made only of shims (`crontab`, `install`, `systemctl`, `dnf`, ...) that record
  their calls and act on files inside `tmp_path`; before each run the test checks that every
  protected command resolves to its shim, so the real crontab and systemd are unreachable.

The fake account is `000000000000`; the instance ID is the AWS documentation example.
"""
from __future__ import annotations

import ast
import configparser
import copy
import io
import os
import re
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from infra import _aws, config, ec2_instance as ec2i
from infra.instance import pi_boot_guard as guard
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.unit

ACCOUNT = "000000000000"
IID = "i-0123456789abcdef0"  # AWS documentation example ID
ASSOC_ID = "iip-assoc-0123456789abcdef0"
NAME = config.INSTANCE_NAME
NEW_PROFILE = config.INSTANCE_PROFILE
OLD_PROFILE = config.OLD_INSTANCE_ROLE
NEW_PROFILE_ARN = config.instance_profile_arn(ACCOUNT, NEW_PROFILE)
OLD_PROFILE_ARN = config.instance_profile_arn(ACCOUNT, OLD_PROFILE)
FOREIGN_TAG = {"Key": "owner", "Value": "someone-else"}

CONVERGED_TAGS = [{"Key": "Name", "Value": NAME}, {"Key": "project", "Value": "price-intelligence"}]
CONVERGED_MD = {"State": "applied", "HttpTokens": "required", "HttpPutResponseHopLimit": 2,
                "HttpEndpoint": "enabled"}
LEGACY_MD = {"State": "applied", "HttpTokens": "optional", "HttpPutResponseHopLimit": 1,
             "HttpEndpoint": "enabled"}
DESCRIBE_FILTERS = [{"Name": "tag:Name", "Values": [NAME]},
                    {"Name": "instance-state-name",
                     "Values": ["pending", "running", "stopping", "stopped"]}]
ASSOC_FILTERS = [{"Name": "instance-id", "Values": [IID]},
                 {"Name": "state", "Values": ["associating", "associated"]}]

READ_OPS = {"GetCallerIdentity", "DescribeInstances", "DescribeInstanceAttribute",
            "GetInstanceProfile", "DescribeIamInstanceProfileAssociations"}
MUTATING_OPS = {"CreateTags", "ModifyInstanceMetadataOptions",
                "ReplaceIamInstanceProfileAssociation", "AssociateIamInstanceProfile",
                "RunInstances"}
# What the instance script must never do (it runs against the production instance).
FORBIDDEN_METHODS = ("stop_instances", "start_instances", "terminate_instances", "reboot_instances",
                     "modify_instance_attribute", "disassociate_iam_instance_profile",
                     "delete_tags", "delete_instance_profile", "delete_role", "put_role_policy")


# =============================================================================================
# Stubber session
# =============================================================================================
class StubSession:
    """Session double: real STS/EC2/IAM clients with fake credentials, each behind a Stubber.

    Records every operation (service, name, params) in call order across the three clients, so
    the tests can assert the global order and that no mutating call happened.
    """

    def __init__(self, account=ACCOUNT):
        boto = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                             region_name=config.REGION)
        self.clients = {s: boto.client(s, region_name=config.REGION) for s in ("sts", "ec2", "iam")}
        self.stubs = {s: Stubber(c) for s, c in self.clients.items()}
        self.ops: list[tuple[str, str, dict]] = []
        for service, c in self.clients.items():
            c.meta.events.register("before-parameter-build.*.*", self._recorder(service))
        self.stubs["sts"].add_response(
            "get_caller_identity",
            {"Account": account, "Arn": f"arn:aws:sts::{account}:assumed-role/fake/fake",
             "UserId": "AROAFAKEFAKEFAKE:fake"}, {})
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

    def error(self, service, op, code, params=None, status=400):
        self.stubs[service].add_client_error(op, service_error_code=code, http_status_code=status,
                                             expected_params=params)

    def verify(self):
        for stub in self.stubs.values():
            stub.assert_no_pending_responses()

    @property
    def names(self):
        return [name for _, name, _ in self.ops]


def instance_doc(tags=None, metadata=None, state="stopped", iid=IID):
    return {"InstanceId": iid, "State": {"Code": 80 if state == "stopped" else 16, "Name": state},
            "Tags": copy.deepcopy(CONVERGED_TAGS if tags is None else tags),
            "MetadataOptions": copy.deepcopy(CONVERGED_MD if metadata is None else metadata)}


def profile_doc(name, roles=True):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return {"InstanceProfile": {
        "Path": "/", "InstanceProfileName": name, "InstanceProfileId": "AIPAFAKEFAKEFAKEFAKE1",
        "Arn": config.instance_profile_arn(ACCOUNT, name), "CreateDate": created,
        "Roles": [{"Path": "/", "RoleName": name, "RoleId": "AROAFAKEFAKEFAKEFAKE1",
                   "Arn": config.role_arn(ACCOUNT, name), "CreateDate": created}] if roles else []}}


def assoc_doc(profile_arn, state="associated"):
    return {"AssociationId": ASSOC_ID, "InstanceId": IID, "State": state,
            "IamInstanceProfile": {"Arn": profile_arn, "Id": "AIPAFAKEFAKEFAKEFAKE2"}}


def stub_describe(s, instances):
    s.add("ec2", "describe_instances", {"Reservations": [{"Instances": instances}]},
          {"Filters": DESCRIBE_FILTERS})


def stub_shutdown_attr(s, value="stop"):
    s.add("ec2", "describe_instance_attribute",
          {"InstanceId": IID, "InstanceInitiatedShutdownBehavior": {"Value": value}},
          {"InstanceId": IID, "Attribute": "instanceInitiatedShutdownBehavior"})


def stub_profile(s, name=NEW_PROFILE, roles=True):
    s.add("iam", "get_instance_profile", profile_doc(name, roles), {"InstanceProfileName": name})


def stub_assocs(s, assocs):
    s.add("ec2", "describe_iam_instance_profile_associations",
          {"IamInstanceProfileAssociations": assocs}, {"Filters": ASSOC_FILTERS})


def assoc_response(profile_arn):
    return {"IamInstanceProfileAssociation": assoc_doc(profile_arn, "associating")}


def run_main(s, argv, capsys):
    rc = ec2i.main(argv, session=s)
    out = capsys.readouterr()
    return rc, out.out + out.err


def assert_clean_output(text):
    """No account ID and no instance ID ever reach the output (it may be pasted into chat)."""
    assert ACCOUNT not in text, text
    assert IID not in text and not re.search(r"\bi-[0-9a-f]{8,17}\b", text), text
    assert ASSOC_ID not in text, text


# =============================================================================================
# ec2_instance.py: pure builders
# =============================================================================================
def test_desired_tags_and_metadata_options():
    assert ec2i.desired_tags() == {"project": "price-intelligence", "Name": "wayakit-app-server"}
    assert ec2i.desired_metadata_options() == {"HttpTokens": "required", "HttpPutResponseHopLimit": 2,
                                               "HttpEndpoint": "enabled"}


@pytest.mark.parametrize("current, changes", [
    (None, True),
    ({}, True),
    (CONVERGED_MD, False),
    ({**CONVERGED_MD, "HttpProtocolIpv6": "disabled", "InstanceMetadataTags": "enabled"}, False),
    ({**CONVERGED_MD, "HttpTokens": "optional"}, True),
    ({**CONVERGED_MD, "HttpPutResponseHopLimit": 1}, True),
    ({**CONVERGED_MD, "HttpEndpoint": "disabled"}, True),
    (LEGACY_MD, True),
], ids=["none", "empty", "converged", "converged-extra-keys", "imdsv1", "hop1", "endpoint-off",
        "legacy"])
def test_metadata_changes(current, changes):
    result = ec2i.metadata_changes(current)
    assert result == (ec2i.desired_metadata_options() if changes else {})


def test_instances_from_reservations():
    assert ec2i.instances_from_reservations(None) == []
    assert ec2i.instances_from_reservations([{"Instances": [{"a": 1}]}, {}, {"Instances": [{"b": 2}]}]) \
        == [{"a": 1}, {"b": 2}]


def test_select_instance_requires_exactly_one():
    assert ec2i.select_instance([{"InstanceId": IID}]) == {"InstanceId": IID}
    with pytest.raises(SystemExit, match="No instance"):
        ec2i.select_instance([])
    with pytest.raises(SystemExit, match="2 instances"):
        ec2i.select_instance([{"InstanceId": IID}, {"InstanceId": "i-0123456789abcdef1"}])


@pytest.mark.parametrize("arn, name", [
    (NEW_PROFILE_ARN, NEW_PROFILE),
    (f"arn:aws:iam::{ACCOUNT}:instance-profile/some/path/{NEW_PROFILE}", NEW_PROFILE),
    (NEW_PROFILE, NEW_PROFILE),
])
def test_profile_name_from_arn(arn, name):
    assert ec2i.profile_name_from_arn(arn) == name


def test_association_plan():
    assert ec2i.association_plan([], NEW_PROFILE_ARN) == {
        "action": "associate", "association_id": None, "previous_profile": None}
    assert ec2i.association_plan([assoc_doc(OLD_PROFILE_ARN)], NEW_PROFILE_ARN) == {
        "action": "replace", "association_id": ASSOC_ID, "previous_profile": OLD_PROFILE}
    assert ec2i.association_plan([assoc_doc(NEW_PROFILE_ARN)], NEW_PROFILE_ARN)["action"] == "none"
    # Same profile reported with a path: still "none" (compared by name).
    with_path = f"arn:aws:iam::{ACCOUNT}:instance-profile/pi/{NEW_PROFILE}"
    assert ec2i.association_plan([assoc_doc(with_path)], NEW_PROFILE_ARN)["action"] == "none"
    # Disassociating/disassociated associations are not "current".
    for state in ("disassociating", "disassociated"):
        assert ec2i.association_plan([assoc_doc(OLD_PROFILE_ARN, state)], NEW_PROFILE_ARN)["action"] \
            == "associate"


def test_bootstrap_args_only_carry_given_values():
    assert ec2i.bootstrap_args() == ["--log-dir", "/home/ec2-user/logs"]
    assert ec2i.bootstrap_args("/x", "arn:t", "p", "f") == [
        "--log-dir", "/x", "--alerts-topic-arn", "arn:t", "--primary-model", "p",
        "--fallback-model", "f"]
    assert ec2i.bootstrap_args("/x", None, "", "f") == ["--log-dir", "/x", "--fallback-model", "f"]


def _clone_and_bootstrap_argv(text):
    """Runs the clone and bootstrap lines of a userData through bash with `printf` in place of the
    commands, so the test sees the exact argv bash would build. Nothing is executed."""
    lines = text.splitlines()
    clone = next(line for line in lines if "git clone" in line).strip()
    boot = next(line for line in lines if "bootstrap.sh" in line and line.startswith("bash ")).strip()
    script = (f"set -- {clone}\nprintf '%s\\0' \"$@\"\nprintf '\\n'\n"
              f"set -- {boot}\nprintf '%s\\0' \"$@\"\n")
    r = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", script], env={"PATH": ""},
                       capture_output=True, timeout=10)
    assert r.returncode == 0, r.stderr
    first, second = r.stdout.split(b"\n", 1)
    return ([a.decode() for a in first.split(b"\0")[:-1]],
            [a.decode() for a in second.split(b"\0")[:-1]])


def test_user_data_clones_the_branch_and_runs_bootstrap_with_quoted_values():
    hostile = "main; touch /tmp/pwned $(id)"
    text = ec2i.user_data("https://example.invalid/repo.git", hostile,
                          ["--log-dir", "/home/ec2-user/logs", "--primary-model", "a b'c"])
    assert text.startswith("#!/bin/bash\n") and "set -euo pipefail" in text
    subprocess.run(["/bin/bash", "-n"], input=text, text=True, check=True, timeout=10)
    clone, boot = _clone_and_bootstrap_argv(text)
    assert clone == ["runuser", "-u", "ec2-user", "--", "git", "clone", "--branch", hostile,
                     "--depth", "1", "https://example.invalid/repo.git", ec2i.REPO_DIR]
    assert boot == ["bash", f"{ec2i.REPO_DIR}/infra/instance/bootstrap.sh",
                    "--log-dir", "/home/ec2-user/logs", "--primary-model", "a b'c"]


def test_user_data_defaults():
    text = ec2i.user_data()
    clone, boot = _clone_and_bootstrap_argv(text)
    assert clone[clone.index("--branch") + 1] == "main" == config.DEPLOY_BRANCH
    assert ec2i.REPO_URL == ("https://github.com/Wayakit-Biotechnology-in-Hygiene/"
                             "wayakit-price-prediction.git")
    assert boot[2:] == ec2i.bootstrap_args()
    assert ec2i.REPO_DIR == "/home/ec2-user/wayakit-price-prediction"


def test_run_instances_params_shape_and_safety():
    params = ec2i.run_instances_params("#!/bin/bash\n", subnet_id="subnet-0fake",
                                       security_group_ids=["sg-0fake"])
    tags = [{"Key": "Name", "Value": NAME}, {"Key": "project", "Value": "price-intelligence"}]
    assert params == {
        "ImageId": "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
        "InstanceType": "t3.medium", "MinCount": 1, "MaxCount": 1,
        "IamInstanceProfile": {"Name": "price-intelligence-instance"},
        "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 2,
                            "HttpEndpoint": "enabled"},
        "InstanceInitiatedShutdownBehavior": "stop",
        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {
            "VolumeSize": 30, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}],
        "TagSpecifications": [{"ResourceType": "instance", "Tags": tags},
                              {"ResourceType": "volume", "Tags": tags}],
        "UserData": "#!/bin/bash\n",
        "SubnetId": "subnet-0fake", "SecurityGroupIds": ["sg-0fake"],
    }
    minimal = ec2i.run_instances_params("x")
    assert "SubnetId" not in minimal and "SecurityGroupIds" not in minimal
    assert "KeyName" not in minimal, "access is through SSM only: no key pair"


def test_run_instances_params_validate_against_the_ec2_model():
    s = StubSession()
    params = ec2i.run_instances_params(ec2i.user_data(), "subnet-0fake", ["sg-0fake"])
    s.add("ec2", "run_instances", {"Instances": []}, params)
    s.clients["ec2"].run_instances(**params)
    s.stubs["ec2"].assert_no_pending_responses()


# =============================================================================================
# ec2_instance.py: adopt mode with Stubber (exact operations, order and parameters)
# =============================================================================================
def _stub_adopt_reads(s, instance, shutdown="stop", assocs=None, profile_roles=True):
    stub_describe(s, [instance])
    stub_shutdown_attr(s, shutdown)
    stub_profile(s, roles=profile_roles)
    stub_assocs(s, [assoc_doc(OLD_PROFILE_ARN)] if assocs is None else assocs)


def test_adopt_dry_run_makes_only_read_calls(capsys):
    s = StubSession()
    _stub_adopt_reads(s, instance_doc(tags=[{"Key": "Name", "Value": NAME}], metadata=LEGACY_MD))
    rc, out = run_main(s, ["--dry-run"], capsys)
    assert rc == 0
    s.verify()
    assert s.names == ["GetCallerIdentity", "DescribeInstances", "DescribeInstanceAttribute",
                       "GetInstanceProfile", "DescribeIamInstanceProfileAssociations"]
    assert not MUTATING_OPS & set(s.names)
    assert "[dry-run] would add tags ['project']" in out
    assert "[dry-run] would set metadata options" in out
    assert "[dry-run] would replace instance profile price-intelligence-instance" in out
    assert f"--rollback-profile {OLD_PROFILE}" in out
    assert_clean_output(out)


def test_adopt_apply_replaces_the_profile_and_fixes_tags_and_imds(capsys):
    s = StubSession()
    stub_describe(s, [instance_doc(tags=[{"Key": "Name", "Value": NAME}, FOREIGN_TAG],
                                   metadata=LEGACY_MD, state="running")])
    # The profile is checked before any change (a missing profile must leave nothing half-applied).
    stub_shutdown_attr(s)
    stub_profile(s)
    stub_assocs(s, [assoc_doc(OLD_PROFILE_ARN)])
    s.add("ec2", "create_tags", {}, {"Resources": [IID],
                                     "Tags": [{"Key": "project", "Value": "price-intelligence"}]})
    s.add("ec2", "modify_instance_metadata_options",
          {"InstanceId": IID, "InstanceMetadataOptions": CONVERGED_MD},
          {"InstanceId": IID, "HttpTokens": "required", "HttpPutResponseHopLimit": 2,
           "HttpEndpoint": "enabled"})
    s.add("ec2", "replace_iam_instance_profile_association", assoc_response(NEW_PROFILE_ARN),
          {"IamInstanceProfile": {"Arn": NEW_PROFILE_ARN}, "AssociationId": ASSOC_ID})
    rc, out = run_main(s, [], capsys)
    assert rc == 0
    s.verify()
    assert s.names == ["GetCallerIdentity", "DescribeInstances", "DescribeInstanceAttribute",
                       "GetInstanceProfile", "DescribeIamInstanceProfileAssociations",
                       "CreateTags", "ModifyInstanceMetadataOptions",
                       "ReplaceIamInstanceProfileAssociation"]
    # The rollback is printed before the switch, with the previous profile's name.
    assert f"previous instance profile: {OLD_PROFILE}" in out
    assert f"python -m infra.ec2_instance --rollback-profile {OLD_PROFILE}" in out
    assert "running" in out
    assert_clean_output(out)


def test_adopt_apply_associates_when_the_instance_has_no_profile(capsys):
    s = StubSession()
    stub_describe(s, [instance_doc()])
    stub_shutdown_attr(s)
    stub_profile(s)
    stub_assocs(s, [])
    s.add("ec2", "associate_iam_instance_profile", assoc_response(NEW_PROFILE_ARN),
          {"IamInstanceProfile": {"Arn": NEW_PROFILE_ARN}, "InstanceId": IID})
    rc, out = run_main(s, [], capsys)
    assert rc == 0
    s.verify()
    assert "ReplaceIamInstanceProfileAssociation" not in s.names
    assert "CreateTags" not in s.names and "ModifyInstanceMetadataOptions" not in s.names
    assert "previous instance profile" not in out
    assert_clean_output(out)


def test_adopt_apply_on_a_converged_instance_only_reads(capsys):
    s = StubSession()
    _stub_adopt_reads(s, instance_doc(tags=CONVERGED_TAGS + [FOREIGN_TAG]),
                      assocs=[assoc_doc(NEW_PROFILE_ARN)])
    rc, out = run_main(s, [], capsys)
    assert rc == 0
    s.verify()
    assert set(s.names) <= READ_OPS, s.names
    assert "tags: OK" in out and "IMDSv2: OK" in out and "instance profile: OK" in out
    assert_clean_output(out)


def test_adopt_warns_on_terminate_shutdown_behavior_and_never_changes_it(capsys):
    s = StubSession()
    _stub_adopt_reads(s, instance_doc(), shutdown="terminate", assocs=[assoc_doc(NEW_PROFILE_ARN)])
    rc, out = run_main(s, [], capsys)
    assert rc == 0
    s.verify()
    assert "WARNING: shutdown behavior is 'terminate'" in out
    assert "ModifyInstanceAttribute" not in s.names


def test_adopt_dry_run_with_missing_profile_warns_and_exits_0(capsys):
    s = StubSession()
    stub_describe(s, [instance_doc()])
    stub_shutdown_attr(s)
    s.error("iam", "get_instance_profile", "NoSuchEntity", {"InstanceProfileName": NEW_PROFILE}, 404)
    rc, out = run_main(s, ["--dry-run"], capsys)
    assert rc == 0
    s.verify()
    assert "WARNING: instance profile 'price-intelligence-instance' does not exist" in out
    assert "infra/instance_role.py" in out
    assert not MUTATING_OPS & set(s.names)


def test_adopt_apply_with_missing_profile_exits_1_without_touching_the_association(capsys):
    s = StubSession()
    stub_describe(s, [instance_doc()])
    stub_shutdown_attr(s)
    s.error("iam", "get_instance_profile", "NoSuchEntity", {"InstanceProfileName": NEW_PROFILE}, 404)
    with pytest.raises(SystemExit, match="does not exist"):
        ec2i.main([], session=s)
    s.verify()
    assert not {"ReplaceIamInstanceProfileAssociation", "AssociateIamInstanceProfile",
                "DescribeIamInstanceProfileAssociations"} & set(s.names)


@pytest.mark.parametrize("problem", ["missing", "no-role"])
def test_adopt_apply_with_a_bad_profile_changes_nothing_even_when_tags_and_imds_are_off(problem):
    """Finding 4: the profile is checked before the tags and IMDS fixes, so an apply that exits 1
    for the profile leaves the instance exactly as it was (no half-applied adopt)."""
    s = StubSession()
    stub_describe(s, [instance_doc(tags=[FOREIGN_TAG], metadata=LEGACY_MD, state="running")])
    stub_shutdown_attr(s)
    if problem == "missing":
        s.error("iam", "get_instance_profile", "NoSuchEntity", {"InstanceProfileName": NEW_PROFILE},
                404)
    else:
        stub_profile(s, roles=False)
    with pytest.raises(SystemExit, match="does not exist" if problem == "missing" else "has no role"):
        ec2i.main([], session=s)
    s.verify()
    assert s.names == ["GetCallerIdentity", "DescribeInstances", "DescribeInstanceAttribute",
                       "GetInstanceProfile"]
    assert not MUTATING_OPS & set(s.names)


def test_rollback_to_a_role_less_profile_exits_1_without_changes():
    s = StubSession()
    stub_describe(s, [instance_doc(tags=[], metadata=LEGACY_MD)])
    stub_profile(s, name=OLD_PROFILE, roles=False)
    with pytest.raises(SystemExit, match="has no role"):
        ec2i.main(["--rollback-profile", OLD_PROFILE], session=s)
    s.verify()
    assert not MUTATING_OPS & set(s.names)


def test_profile_without_role_is_refused(capsys):
    s = StubSession()
    stub_describe(s, [instance_doc()])
    stub_shutdown_attr(s)
    stub_profile(s, roles=False)
    with pytest.raises(SystemExit, match="has no role"):
        ec2i.main([], session=s)
    s.verify()
    assert not MUTATING_OPS & set(s.names)


def test_access_denied_propagates_instead_of_meaning_missing():
    s = StubSession()
    stub_describe(s, [instance_doc()])
    stub_shutdown_attr(s)
    s.error("iam", "get_instance_profile", "AccessDenied", {"InstanceProfileName": NEW_PROFILE}, 403)
    with pytest.raises(ClientError, match="AccessDenied"):
        ec2i.main(["--dry-run"], session=s)
    assert not MUTATING_OPS & set(s.names)


@pytest.mark.parametrize("instances, message", [
    ([], "No instance"),
    ([instance_doc(), instance_doc(iid="i-0123456789abcdef1")], "must be unique"),
], ids=["none", "two"])
def test_adopt_needs_exactly_one_instance_and_stops_before_any_other_call(instances, message):
    s = StubSession()
    stub_describe(s, instances)
    with pytest.raises(SystemExit, match=message):
        ec2i.main([], session=s)
    s.verify()
    assert s.names == ["GetCallerIdentity", "DescribeInstances"]


def test_describe_instances_follows_next_token():
    s = StubSession()
    s.add("ec2", "describe_instances", {"Reservations": [], "NextToken": "page-2"},
          {"Filters": DESCRIBE_FILTERS})
    s.add("ec2", "describe_instances", {"Reservations": [{"Instances": [instance_doc()]}]},
          {"Filters": DESCRIBE_FILTERS, "NextToken": "page-2"})
    found = ec2i.find_instances(s.clients["ec2"])
    s.stubs["ec2"].assert_no_pending_responses()
    assert [i["InstanceId"] for i in found] == [IID]


def test_terminated_instances_are_not_searched():
    assert set(ec2i.STATES_ALIVE) == {"pending", "running", "stopping", "stopped"}


# --- rollback ------------------------------------------------------------------------------------
def test_rollback_reassociates_the_old_profile_and_nothing_else(capsys):
    s = StubSession()
    stub_describe(s, [instance_doc(tags=[], metadata=LEGACY_MD, state="running")])
    stub_profile(s, name=OLD_PROFILE)
    stub_assocs(s, [assoc_doc(NEW_PROFILE_ARN)])
    s.add("ec2", "replace_iam_instance_profile_association", assoc_response(OLD_PROFILE_ARN),
          {"IamInstanceProfile": {"Arn": OLD_PROFILE_ARN}, "AssociationId": ASSOC_ID})
    rc, out = run_main(s, ["--rollback-profile", OLD_PROFILE], capsys)
    assert rc == 0
    s.verify()
    assert s.names == ["GetCallerIdentity", "DescribeInstances", "GetInstanceProfile",
                       "DescribeIamInstanceProfileAssociations",
                       "ReplaceIamInstanceProfileAssociation"]
    assert_clean_output(out)


def test_rollback_dry_run_only_reads(capsys):
    s = StubSession()
    stub_describe(s, [instance_doc()])
    stub_profile(s, name=OLD_PROFILE)
    stub_assocs(s, [assoc_doc(NEW_PROFILE_ARN)])
    rc, out = run_main(s, ["--rollback-profile", OLD_PROFILE, "--dry-run"], capsys)
    assert rc == 0
    s.verify()
    assert not MUTATING_OPS & set(s.names)
    assert f"[dry-run] would replace instance profile {OLD_PROFILE}" in out
    assert "[WARN] the apply would stop here: the instance is stopped" in out


def test_rollback_of_a_missing_profile_exits_1():
    s = StubSession()
    stub_describe(s, [instance_doc()])
    s.error("iam", "get_instance_profile", "NoSuchEntity", {"InstanceProfileName": "nope"}, 404)
    with pytest.raises(SystemExit, match="'nope' does not exist"):
        ec2i.main(["--rollback-profile", "nope"], session=s)
    assert not MUTATING_OPS & set(s.names)


def test_create_and_rollback_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        ec2i.parse_args(["--create", "--rollback-profile", OLD_PROFILE])
    assert exc.value.code == 2


# --- create --------------------------------------------------------------------------------------
def _create_argv(*extra):
    return ["--create", "--alerts-topic", "--primary-model", "primary-fake",
            "--fallback-model", "fallback-fake", *extra]


def test_create_dry_run_never_launches(capsys):
    s = StubSession()
    stub_describe(s, [])
    stub_profile(s)
    rc, out = run_main(s, _create_argv("--dry-run"), capsys)
    assert rc == 0
    s.verify()
    assert s.names == ["GetCallerIdentity", "DescribeInstances", "GetInstanceProfile"]
    assert "[dry-run] would launch a t3.medium 'wayakit-app-server'" in out
    assert "branch main" in out
    assert_clean_output(out)


def test_create_apply_launches_one_instance_with_exact_params(capsys):
    s = StubSession()
    stub_describe(s, [])
    stub_profile(s)
    expected_ud = ec2i.user_data(ec2i.REPO_URL, "main", [
        "--log-dir", "/home/ec2-user/logs", "--alerts-topic-arn", config.topic_arn(ACCOUNT),
        "--primary-model", "primary-fake", "--fallback-model", "fallback-fake"])
    params = ec2i.run_instances_params(expected_ud, "subnet-0fake", ["sg-0fake"])
    s.add("ec2", "run_instances", {"Instances": [instance_doc(state="pending")]}, params)
    rc, out = run_main(s, _create_argv("--subnet-id", "subnet-0fake", "--security-group-id",
                                       "sg-0fake"), capsys)
    assert rc == 0
    s.verify()
    assert s.names == ["GetCallerIdentity", "DescribeInstances", "GetInstanceProfile", "RunInstances"]
    assert_clean_output(out)


def test_create_refuses_when_the_instance_already_exists():
    s = StubSession()
    stub_describe(s, [instance_doc()])
    with pytest.raises(SystemExit, match="already exists"):
        ec2i.main(_create_argv(), session=s)
    assert "RunInstances" not in s.names


def test_create_apply_without_the_profile_exits_1():
    s = StubSession()
    stub_describe(s, [])
    s.error("iam", "get_instance_profile", "NoSuchEntity", {"InstanceProfileName": NEW_PROFILE}, 404)
    with pytest.raises(SystemExit, match="does not exist"):
        ec2i.main(_create_argv(), session=s)
    assert "RunInstances" not in s.names


def test_create_without_alerts_topic_leaves_it_out_of_the_user_data():
    s = StubSession()
    stub_describe(s, [])
    stub_profile(s)
    s.add("ec2", "run_instances", {"Instances": []}, None)
    ec2i.main(["--create"], session=s)
    params = next(p for _, n, p in s.ops if n == "RunInstances")
    assert "--alerts-topic-arn" not in params["UserData"]
    assert ACCOUNT not in params["UserData"]


# --- least privilege (static) ---------------------------------------------------------------------
def test_the_script_never_stops_starts_or_terminates_the_instance():
    source = (REPO_ROOT / "infra" / "ec2_instance.py").read_text(encoding="utf-8")
    for method in FORBIDDEN_METHODS:
        assert method not in source, method


def test_docstring_lists_exactly_the_dry_run_read_calls(capsys):
    """The read calls the module docstring promises for --dry-run are the ones it makes."""
    doc = ec2i.__doc__
    listed = set(re.findall(r"(?:sts|ec2|iam):([A-Za-z]+)", doc.split("`--dry-run`")[1].split("\n\n")[0]))
    s = StubSession()
    _stub_adopt_reads(s, instance_doc(tags=[], metadata=LEGACY_MD))
    ec2i.main(["--dry-run"], session=s)
    assert listed == set(s.names) == READ_OPS


# =============================================================================================
# ec2_instance.py: stateful fake (idempotence, dry-run, rollback round trip)
# =============================================================================================
def _client_error(code, op):
    return ClientError({"Error": {"Code": code, "Message": "fake"}}, op)


class FakeAws:
    """Stateful STS + EC2 + IAM double for one instance. Records every call by API name."""

    def __init__(self, tags=None, metadata=None, profile=OLD_PROFILE, shutdown="stop",
                 profiles=(NEW_PROFILE, OLD_PROFILE), instances=1, roleless=(), state="running"):
        self.instance = instance_doc(tags=tags, metadata=metadata, state=state)
        self.instances = instances
        self.association = None if profile is None else {
            "AssociationId": "iip-assoc-fake-1", "InstanceId": IID, "State": "associated",
            "IamInstanceProfile": {"Arn": config.instance_profile_arn(ACCOUNT, profile)}}
        self.shutdown = shutdown
        self.profiles = set(profiles)
        self.roleless = set(roleless)  # profiles that exist but have no role
        self.calls: list[str] = []
        self._assoc_seq = 1

    # session
    def client(self, service, **kwargs):
        assert kwargs.get("region_name") == config.REGION
        return self

    # STS
    def get_caller_identity(self):
        self.calls.append("GetCallerIdentity")
        return {"Account": ACCOUNT}

    # EC2 reads
    def describe_instances(self, **kwargs):
        self.calls.append("DescribeInstances")
        assert kwargs["Filters"] == DESCRIBE_FILTERS
        return {"Reservations": [{"Instances": [copy.deepcopy(self.instance)
                                                for _ in range(self.instances)]}]}

    def describe_instance_attribute(self, InstanceId, Attribute):
        self.calls.append("DescribeInstanceAttribute")
        assert (InstanceId, Attribute) == (IID, "instanceInitiatedShutdownBehavior")
        return {"InstanceInitiatedShutdownBehavior": {"Value": self.shutdown}}

    def describe_iam_instance_profile_associations(self, Filters):
        self.calls.append("DescribeIamInstanceProfileAssociations")
        return {"IamInstanceProfileAssociations": [copy.deepcopy(self.association)]
                if self.association else []}

    # EC2 writes
    def create_tags(self, Resources, Tags):
        self.calls.append("CreateTags")
        assert Resources == [IID]
        current = _aws.tags_to_dict(self.instance["Tags"])
        current.update(_aws.tags_to_dict(Tags))
        self.instance["Tags"] = _aws.to_tag_list(current)
        return {}

    def modify_instance_metadata_options(self, InstanceId, **kwargs):
        self.calls.append("ModifyInstanceMetadataOptions")
        assert InstanceId == IID
        self.instance["MetadataOptions"].update(kwargs)
        return {}

    def _associate(self, arn):
        self._assoc_seq += 1
        self.association = {"AssociationId": f"iip-assoc-fake-{self._assoc_seq}", "InstanceId": IID,
                            "State": "associated", "IamInstanceProfile": {"Arn": arn}}

    def replace_iam_instance_profile_association(self, IamInstanceProfile, AssociationId):
        self.calls.append("ReplaceIamInstanceProfileAssociation")
        assert self.association and AssociationId == self.association["AssociationId"]
        if self.instance["State"]["Name"] != "running":  # real AWS, seen 2026-11-22
            raise _client_error("IncorrectState", "ReplaceIamInstanceProfileAssociation")
        self._associate(IamInstanceProfile["Arn"])
        return {}

    def associate_iam_instance_profile(self, IamInstanceProfile, InstanceId):
        self.calls.append("AssociateIamInstanceProfile")
        assert self.association is None, "AWS refuses a second association"
        self._associate(IamInstanceProfile["Arn"])
        return {}

    def run_instances(self, **kwargs):
        self.calls.append("RunInstances")
        return {"Instances": []}

    # IAM
    def get_instance_profile(self, InstanceProfileName):
        self.calls.append("GetInstanceProfile")
        if InstanceProfileName not in self.profiles:
            raise _client_error("NoSuchEntity", "GetInstanceProfile")
        return profile_doc(InstanceProfileName, roles=InstanceProfileName not in self.roleless)

    # helpers
    def snapshot(self):
        return copy.deepcopy((self.instance, self.association, self.shutdown))

    def associated_profile(self):
        return ec2i.profile_name_from_arn(self.association["IamInstanceProfile"]["Arn"]) \
            if self.association else None

    def mutations(self):
        return [c for c in self.calls if c in MUTATING_OPS]

    def converged(self):
        tags = _aws.tags_to_dict(self.instance["Tags"])
        return (all(tags.get(k) == v for k, v in ec2i.desired_tags().items())
                and ec2i.metadata_changes(self.instance["MetadataOptions"]) == {}
                and self.associated_profile() == NEW_PROFILE)


def test_fake_second_apply_only_reads_and_dry_run_changes_nothing(capsys):
    aws = FakeAws(tags=[FOREIGN_TAG], metadata=dict(LEGACY_MD), profile=OLD_PROFILE)
    before = aws.snapshot()
    assert ec2i.main(["--dry-run"], session=aws) == 0
    assert aws.mutations() == [] and aws.snapshot() == before
    aws.calls.clear()
    assert ec2i.main([], session=aws) == 0
    assert aws.converged()
    assert _aws.tags_to_dict(aws.instance["Tags"])["owner"] == "someone-else", "foreign tags kept"
    aws.calls.clear()
    assert ec2i.main([], session=aws) == 0
    assert aws.mutations() == [], aws.calls
    assert_clean_output(capsys.readouterr().out)


def test_fake_rollback_then_adopt_round_trip():
    aws = FakeAws(profile=OLD_PROFILE)
    ec2i.main([], session=aws)
    assert aws.associated_profile() == NEW_PROFILE
    ec2i.main(["--rollback-profile", OLD_PROFILE], session=aws)
    assert aws.associated_profile() == OLD_PROFILE
    tags_md = copy.deepcopy((aws.instance["Tags"], aws.instance["MetadataOptions"]))
    ec2i.main([], session=aws)
    assert aws.associated_profile() == NEW_PROFILE
    assert (aws.instance["Tags"], aws.instance["MetadataOptions"]) == tags_md


# =============================================================================================
# pi_boot_guard.py
# =============================================================================================
UTC = timezone.utc
MX = ZoneInfo("America/Mexico_City")


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def test_guard_time_zone_matches_the_schedule():
    assert guard.DEFAULT_TZ == config.SCHEDULE_TIMEZONE == "America/Mexico_City"


def test_schedule_cron_falls_inside_the_guard_window_on_a_thursday():
    m = re.fullmatch(r"cron\((\d+) (\d+) \? \* (\d)L \*\)", config.SCHEDULE_CRON)
    assert m, config.SCHEDULE_CRON
    minute, hour, dow = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert guard.WINDOW_START <= time(hour, minute) < guard.WINDOW_END
    # EventBridge day-of-week: 1=SUN ... 7=SAT, so 5 = Thursday = Python weekday 3.
    assert ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"][dow - 1] == "THU"
    assert guard.THURSDAY == 3 == date(2026, 11, 26).weekday()
    # At least 5 minutes of margin for a slow boot before the window closes and for clock skew.
    start = datetime.combine(date(2026, 1, 1), guard.WINDOW_START)
    end = datetime.combine(date(2026, 1, 1), guard.WINDOW_END)
    fire = datetime.combine(date(2026, 1, 1), time(hour, minute))
    assert fire - start >= timedelta(minutes=5) and end - fire >= timedelta(minutes=5)


@pytest.mark.parametrize("day, expected", [
    (date(2026, 11, 26), True), (date(2026, 11, 19), False), (date(2026, 11, 27), False),
    (date(2026, 4, 30), True), (date(2026, 4, 23), False),          # month ends on Thursday
    (date(2026, 12, 31), True),                                     # year ends on Thursday
    (date(2024, 2, 29), True), (date(2024, 2, 22), False),          # leap February
    (date(2029, 2, 22), True),                                      # earliest possible last Thursday
    (date(2026, 2, 26), True),
], ids=str)
def test_is_last_thursday(day, expected):
    assert guard.is_last_thursday(day) is expected


@pytest.mark.parametrize("now, expected", [
    (utc(2026, 11, 27, 3, 50), True),                    # 21:50 CDMX on Thu 26 Nov (the schedule)
    (utc(2026, 11, 27, 3, 39, 59), False),               # 21:39:59: before the window
    (utc(2026, 11, 27, 3, 40), True),                    # 21:40:00: start inclusive
    (utc(2026, 11, 27, 4, 59, 59, 999999), True),        # 22:59:59.999999
    (utc(2026, 11, 27, 5, 0), False),                    # 23:00: end exclusive
    (utc(2026, 11, 26, 21, 50), False),                  # 21:50 UTC is 15:50 local: not UTC wall time
    (utc(2026, 11, 20, 3, 50), False),                   # 21:50 on Thu 19: not the last Thursday
    (utc(2026, 11, 28, 3, 50), False),                   # 21:50 on Fri 27
    (utc(2027, 1, 1, 3, 50), True),                      # Thu 31 Dec 2026 local, already 2027 in UTC
    (utc(2026, 5, 1, 3, 50), True),                      # Thu 30 Apr (month ends on Thursday)
    (utc(2026, 5, 1, 5, 0), False),
    (utc(2024, 3, 1, 3, 50), True),                      # Thu 29 Feb 2024
    # Mexico City had DST until 2022 (CDT = UTC-5 from 4 Apr to 31 Oct 2021): the window follows
    # local time. With a fixed UTC-6 these two would be the other way round.
    (utc(2021, 4, 30, 2, 45), True),                     # 21:45 CDT on Thu 29 Apr 2021
    (utc(2021, 4, 30, 4, 30), False),                    # 23:30 CDT
], ids=lambda v: v.isoformat() if isinstance(v, datetime) else str(v))
def test_is_scheduled_window(now, expected):
    assert guard.is_scheduled_window(now) is expected


def test_is_scheduled_window_accepts_any_offset_for_the_same_instant():
    instant = datetime(2026, 11, 26, 21, 50, tzinfo=MX)
    for tz in (UTC, MX, ZoneInfo("Asia/Riyadh"), timezone(timedelta(hours=14)),
               timezone(timedelta(hours=-11, minutes=-30))):
        assert guard.is_scheduled_window(instant.astimezone(tz)) is True


def test_is_scheduled_window_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="timezone-aware"):
        guard.is_scheduled_window(datetime(2026, 11, 26, 21, 50))


def test_is_scheduled_window_uses_the_tz_argument():
    # 21:50 in New York on its last Thursday is outside the window in Mexico City and vice versa.
    ny = datetime(2026, 11, 26, 21, 50, tzinfo=ZoneInfo("America/New_York"))
    assert guard.is_scheduled_window(ny, "America/New_York") is True
    assert guard.is_scheduled_window(ny) is False


def _call_main(argv, now=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = guard.main(argv, now=now)
        except SystemExit as exc:
            rc = ("SystemExit", exc.code)
    return rc, out.getvalue() + err.getvalue()


def test_main_exit_codes_with_an_injected_clock():
    rc, out = _call_main([], now=utc(2026, 11, 27, 3, 50))
    assert rc == guard.EXIT_INSIDE == 0
    assert "2026-11-26 21:50 America/Mexico_City is inside" in out
    rc, out = _call_main([], now=utc(2026, 11, 20, 3, 50))
    assert rc == guard.EXIT_OUTSIDE == 1
    assert "outside the scheduled window" in out and "not running the pipeline" in out


@pytest.mark.parametrize("argv, expected", [
    (["--now", "2026-11-27T03:50:00+00:00"], 0),
    (["--now", "2026-11-26T21:50:00-06:00"], 0),
    (["--now", "2026-11-20T03:50:00+00:00"], 1),
    (["--now", "2026-11-27T03:50:00Z"], 0),              # "Z" works on Python 3.9/3.10 too
    (["--now", "2026-11-27T03:50:00z"], 0),
    (["--now", "2026-11-20T03:50:00Z"], 1),
    (["--now", "2026-11-27T03:50:00"], 255),              # no offset: an error, not a guess
    (["--now", "Z"], 255),
    (["--now", "2026-11-27T03:50:00+00:00Z"], 255),       # "Z" after an offset is not an offset
    (["--now", "yesterday"], 255),
    (["--now", "2026-11-27T03:50:00+00:00", "--tz", "Mars/Olympus_Mons"], 255),
    (["--tz", ""], 255),
], ids=lambda v: " ".join(v) if isinstance(v, list) else str(v))
def test_main_with_now_argument(argv, expected):
    rc, out = _call_main(argv)
    assert rc == expected, out
    if expected == 255:
        assert "ERROR" in out


@pytest.mark.parametrize("argv", [["--bogus"], ["--now"], ["extra"]], ids=" ".join)
def test_main_argument_errors_are_255_not_a_silent_skip(argv):
    rc, out = _call_main(argv)
    assert rc == ("SystemExit", guard.EXIT_ERROR), out


@pytest.mark.parametrize("argv, expected", [
    (["--help"], 1),                                       # argparse exit 0 must not mean "run"
    (["--bogus"], 255),
    (["--now", "2026-11-27T03:50:00+00:00"], 0),
    (["--now", "2026-11-27T03:50:00"], 255),
], ids=lambda v: " ".join(v) if isinstance(v, list) else str(v))
def test_cli_exit_codes(monkeypatch, argv, expected):
    monkeypatch.setattr(sys, "argv", ["pi_boot_guard.py", *argv])
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        assert guard._cli() == expected


@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyboardInterrupt(), SystemExit("text")],
                         ids=["runtime", "interrupt", "systemexit-text"])
def test_cli_turns_any_crash_into_255(monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(guard, "main", boom)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        assert guard._cli() == guard.EXIT_ERROR


GUARD = REPO_ROOT / "infra" / "instance" / "pi_boot_guard.py"


@pytest.mark.parametrize("args, expected", [
    (["--now", "2026-11-27T03:50:00+00:00"], 0),
    (["--now", "2026-11-27T03:50:00Z"], 0),
    (["--now", "2026-11-27T05:00:00+00:00"], 1),
    (["--help"], 1),
    (["--bogus"], 255),
    (["--now", "2026-11-27T03:50:00"], 255),
], ids=lambda v: " ".join(v) if isinstance(v, list) else str(v))
def test_guard_file_runs_standalone_with_systemd_exit_codes(tmp_path, args, expected):
    """As systemd runs it: by path, isolated (no repo on sys.path), another cwd and another TZ."""
    env = {"PATH": "/usr/bin:/bin", "TZ": "Asia/Tokyo", "LC_ALL": "C"}
    r = subprocess.run([sys.executable, "-I", str(GUARD), *args], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == expected, r.stdout + r.stderr
    assert "Traceback" not in r.stderr


def test_guard_is_stdlib_only_and_python39_syntax():
    source = GUARD.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "argparse", "sys", "datetime", "zoneinfo"}, imported
    assert source.startswith("#!/usr/bin/env python3\n")


# =============================================================================================
# pi-pipeline.service
# =============================================================================================
SERVICE = REPO_ROOT / "infra" / "instance" / "pi-pipeline.service"


def _unit():
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read_string(SERVICE.read_text(encoding="utf-8"))
    return parser


def test_service_unit_keys():
    unit = _unit()
    assert unit.sections() == ["Unit", "Service", "Install"]
    assert dict(unit["Unit"]) == {
        "Description": "Price Intelligence monthly pipeline (only on the scheduled boot)",
        "Wants": "network-online.target", "After": "network-online.target"}
    service = dict(unit["Service"])
    repo = ec2i.REPO_DIR
    assert service == {
        # exec, not oneshot (F3 review): the boot finishes instead of staying "starting" for the
        # whole run; ExecCondition (the boot guard) still decides whether ExecStart runs.
        "Type": "exec",
        "User": "ec2-user",
        "Group": "ec2-user",
        "WorkingDirectory": repo,
        "EnvironmentFile": config.INSTANCE_ENV_FILE,
        "ExecCondition": f"/usr/bin/python3 {repo}/infra/instance/pi_boot_guard.py",
        # One run, no arguments (single run since 2026-11-18; run_pipeline.sh exits 1 on any).
        "ExecStart": f"/bin/bash {repo}/run_pipeline.sh",
        "TimeoutStartSec": "infinity",
        "KillMode": "control-group",
    }
    assert dict(unit["Install"]) == {"WantedBy": "multi-user.target"}


def test_service_never_restarts_and_runs_nothing_but_the_guard_and_the_pipeline():
    text = SERVICE.read_text(encoding="utf-8")
    active = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert not any(line.startswith(("Restart", "ExecStartPre", "ExecStartPost", "ExecStop",
                                    "Environment=", "User=root")) for line in active), active
    assert config.PIPELINE_SERVICE == SERVICE.name


def test_service_paths_exist_in_the_repo():
    unit = _unit()["Service"]
    for key in ("ExecCondition", "ExecStart"):
        path = unit[key].split()[1]
        rel = Path(path).relative_to(ec2i.REPO_DIR)
        assert (REPO_ROOT / rel).is_file(), path


# =============================================================================================
# bootstrap.sh
# =============================================================================================
BOOTSTRAP = REPO_ROOT / "infra" / "instance" / "bootstrap.sh"
BASH = "/bin/bash"


def bash_source(snippet, *, stdin=b"", path="", env=None, cwd=None, timeout=20):
    """`source bootstrap.sh; <snippet>` in a clean bash. `path=""` leaves only builtins."""
    full_env = {"PATH": path, "LC_ALL": "C", "HOME": str(cwd or "/nonexistent")}
    full_env.update(env or {})
    return subprocess.run([BASH, "--noprofile", "--norc", "-c", f'source "$1" || exit 99\n{snippet}',
                           "bash", str(BOOTSTRAP)],
                          input=stdin, env=full_env, cwd=cwd, capture_output=True, timeout=timeout)


def test_sourcing_only_defines_functions():
    r = bash_source("declare -F | while read -r _ _ f; do echo $f; done")
    assert r.returncode == 0, r.stderr
    functions = set(r.stdout.decode().split())
    assert {"filter_crontab", "env_file_value", "render_env_file", "valid_log_dir",
            "valid_topic_arn", "valid_model", "main", "remove_old_cron_lines"} <= functions
    r = bash_source("true")
    assert r.stdout == b"" and r.stderr == b"", "sourcing must not print or run anything"


def test_bootstrap_constants_match_config_and_run_pipeline():
    r = bash_source('printf "%s\\n" "$CONDA_ENV" "$ENV_FILE" "$SERVICE_NAME" "$REPO_DIR_EXPECTED" '
                    '"$DEFAULT_LOG_DIR" "$ENV_KEYS" "$PI_USER" "$CONDA_DIR"')
    values = r.stdout.decode().splitlines()
    assert values == [config.CONDA_ENV, config.INSTANCE_ENV_FILE, config.PIPELINE_SERVICE,
                      ec2i.REPO_DIR, ec2i.DEFAULT_LOG_DIR,
                      " ".join([config.ENV_LOG_DIR, config.ENV_ALERTS_TOPIC_ARN,
                                config.ENV_PRIMARY_MODEL, config.ENV_FALLBACK_MODEL]),
                      "ec2-user", "/home/ec2-user/miniconda"]
    pipeline = (REPO_ROOT / "run_pipeline.sh").read_text(encoding="utf-8")
    assert f'CONDA_ENV_NAME="{config.CONDA_ENV}"' in pipeline
    assert 'echo "/home/ec2-user/miniconda"' in pipeline, "conda fallback path of run_pipeline.sh"


OLD_CRONTAB = (
    "SHELL=/bin/bash\n"
    "MAILTO=\"\"\n"
    "# Price Intelligence (old): stage 1 on Friday, stage 2 on Saturday\n"
    "0 4 * * 5 /home/ec2-user/wayakit-price-prediction/run_pipeline.sh >> /home/ec2-user/logs/cron.log 2>&1\n"
    "0 4 * * 6 /home/ec2-user/wayakit-price-prediction/run_pipeline.sh --stage 2 >> /home/ec2-user/logs/cron.log 2>&1\n"
    "  # 0 3 * * 1 bash run_pipeline.sh --stage 2   (disabled, kept)\n"
    "*/30 * * * * /usr/bin/other-job --flag\n"
)
OLD_CRONTAB_FILTERED = (
    "SHELL=/bin/bash\n"
    "MAILTO=\"\"\n"
    "# Price Intelligence (old): stage 1 on Friday, stage 2 on Saturday\n"
    "  # 0 3 * * 1 bash run_pipeline.sh --stage 2   (disabled, kept)\n"
    "*/30 * * * * /usr/bin/other-job --flag\n"
)


@pytest.mark.parametrize("given, expected", [
    (OLD_CRONTAB, OLD_CRONTAB_FILTERED),
    ("", ""),
    ("0 4 * * 5 x/run_pipeline.sh", ""),                                 # last line without \n
    ("a\n\n0 4 * * 5 run_pipeline.sh\nb", "a\n\nb\n"),                   # blank lines kept
    ("\t0 4 * * 5 run_pipeline.sh\n", ""),                               # indented active line
    ("\t# run_pipeline.sh\n", "\t# run_pipeline.sh\n"),                  # indented comment kept
    ("0 4 * * 5 run_pipeline.shx\n", ""),                                # contains the name
    ("0 4 * * 5 run_pipeline_sh\n", "0 4 * * 5 run_pipeline_sh\n"),      # '.' is literal
    ("0 4 * * 5 RUN_PIPELINE.SH\n", "0 4 * * 5 RUN_PIPELINE.SH\n"),      # case-sensitive
    ("0 4 * * 5 a # run_pipeline.sh\n", ""),                             # trailing comment: active
    ("x\\ny * run_pipeline.sh\n", ""),                                   # backslashes kept literal
])
def test_filter_crontab(given, expected):
    r = bash_source("filter_crontab", stdin=given.encode())
    assert r.returncode == 0, r.stderr
    assert r.stdout.decode() == expected


@pytest.mark.parametrize("content, key, expected", [
    ("PI_LOG_DIR=/a\nPI_ALERTS_TOPIC_ARN=arn:x\n", "PI_ALERTS_TOPIC_ARN", "arn:x"),
    ("PI_LOG_DIR=/a\n", "PI_ALERTS_TOPIC_ARN", ""),
    ("PI_LOG_DIR_OLD=/b\nPI_LOG_DIR=/a\n", "PI_LOG_DIR", "/a"),
    ("# PI_LOG_DIR=/c\nPI_LOG_DIR=/a", "PI_LOG_DIR", "/a"),
    ("PI_LOG_DIR=/a=b\n", "PI_LOG_DIR", "/a=b"),
    ("PI_LOG_DIR=\n", "PI_LOG_DIR", ""),
])
def test_env_file_value(tmp_path, content, key, expected):
    f = tmp_path / "env"
    f.write_text(content)
    r = bash_source(f'env_file_value "$F" "$K"; printf "[rc=%s]" "$?"', env={"F": str(f), "K": key})
    assert r.stdout.decode() == f"{expected}[rc=0]"


def test_env_file_value_of_a_missing_file_is_empty_and_exit_0(tmp_path):
    r = bash_source(f'env_file_value "$F" PI_LOG_DIR; printf "[rc=%s]" "$?"',
                    env={"F": str(tmp_path / "missing")})
    assert r.stdout.decode() == "[rc=0]"


def test_render_env_file():
    topic = "arn:aws:sns:eu-north-1:000000000000:price-intelligence-alerts"
    r = bash_source('render_env_file /home/ec2-user/logs "$T" "" fallback-model', env={"T": topic})
    lines = r.stdout.decode().splitlines()
    assert lines[0].startswith("#") and "No secrets" in lines[0]
    assert lines[1:] == ["PI_LOG_DIR=/home/ec2-user/logs", f"PI_ALERTS_TOPIC_ARN={topic}",
                         "PI_RELEVANCE_FALLBACK_MODEL=fallback-model"]
    r = bash_source('render_env_file /x "" "" ""')
    assert r.stdout.decode().splitlines()[1:] == ["PI_LOG_DIR=/x"]


@pytest.mark.parametrize("fn, value, ok", [
    ("valid_log_dir", "/home/ec2-user/logs", True),
    ("valid_log_dir", "relative/logs", False),
    ("valid_log_dir", "/", False),
    ("valid_log_dir", "/a b", False),
    ("valid_log_dir", "/a;b", False),
    ("valid_log_dir", "/a$b", False),
    ("valid_log_dir", "", False),
    ("valid_topic_arn", "", True),
    ("valid_topic_arn", "arn:aws:sns:eu-north-1:000000000000:price-intelligence-alerts", True),
    ("valid_topic_arn", "arn:aws:sns:eu-north-1:00000000000:price-intelligence-alerts", False),
    ("valid_topic_arn", "arn:aws:sqs:eu-north-1:000000000000:q", False),
    ("valid_topic_arn", "arn:aws:sns:eu-north-1:000000000000:a b", False),
    ("valid_topic_arn", "arn:aws:sns:eu-north-1:000000000000:t\nPI_LOG_DIR=/evil", False),
    ("valid_model", "", True),
    ("valid_model", "deepseek.v3.2", True),
    ("valid_model", "eu.anthropic.claude-haiku-4-5-20251001-v1:0", True),
    ("valid_model", "arn:aws:bedrock:eu-north-1:000000000000:application-inference-profile/abc123", True),
    ("valid_model", "x y", False),
    ("valid_model", "x\"y", False),
    ("valid_model", "x'y", False),
    ("valid_model", "x$y", False),
    ("valid_model", "x;y", False),
    ("valid_model", "x\ny", False),
])
def test_validators(fn, value, ok):
    r = bash_source(f'{fn} "$V"', env={"V": value})
    assert (r.returncode == 0) is ok, (fn, value, r.stderr)


# --- bootstrap.sh run as a script (never as root here) ----------------------------------------
def _script_bin(tmp_path, uid="1000"):
    """PATH dir with a fake `id` (never root) and `sed` (for --help); nothing else exists."""
    d = tmp_path / "bin"
    d.mkdir()
    (d / "id").write_text(f"#!/bin/sh\necho {uid}\n")
    (d / "id").chmod(0o755)
    (d / "sed").symlink_to("/usr/bin/sed")
    return d


@pytest.mark.parametrize("args, message", [
    (["--bogus"], "unknown argument '--bogus'"),
    (["--log-dir"], "--log-dir needs a value"),
    (["--alerts-topic-arn"], "--alerts-topic-arn needs a value"),
    ([], "run it as root"),
    (["--dry-run"], "run it as root"),
], ids=lambda v: " ".join(v) if isinstance(v, list) else None)
def test_bootstrap_script_refuses_bad_arguments_and_non_root(tmp_path, args, message):
    bin_dir = _script_bin(tmp_path)
    r = subprocess.run([BASH, "--noprofile", "--norc", str(BOOTSTRAP), *args],
                       env={"PATH": str(bin_dir), "LC_ALL": "C"}, cwd=tmp_path,
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 1, r.stdout + r.stderr
    assert message in r.stderr
    assert "command not found" not in r.stderr


def test_bootstrap_help_prints_usage_and_exits_0(tmp_path):
    bin_dir = _script_bin(tmp_path)
    r = subprocess.run([BASH, "--noprofile", "--norc", str(BOOTSTRAP), "--help"],
                       env={"PATH": str(bin_dir), "LC_ALL": "C"}, cwd=tmp_path,
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert "Bootstrap of the Price Intelligence instance" in r.stdout
    assert "--alerts-topic-arn" in r.stdout


def test_bootstrap_script_is_executable_and_valid_bash():
    assert os.access(BOOTSTRAP, os.X_OK)
    assert os.access(GUARD, os.X_OK)
    subprocess.run([BASH, "-n", str(BOOTSTRAP)], check=True, timeout=10)


# --- bootstrap.sh side-effect functions with shims (fake crontab, systemctl, install, ...) ------
SHIMMED = ("crontab", "install", "chown", "chmod", "systemctl", "dnf", "rpm", "curl", "runuser",
           "google-chrome", "aws", "sha256sum")
REAL_UTILS = ("wc", "date", "mkdir", "cat", "rm", "cp", "mktemp", "cmp", "cut")


class ShimBox:
    """Shims for every command with effects outside `tmp_path`, plus links to harmless utilities.

    Each shim appends `name<TAB>args` to `calls.tsv`. `crontab` keeps the fake crontab of
    ec2-user in `crontab.txt` (absent = "no crontab"). `install` copies or creates directories,
    but only inside `tmp_path` (anything else is recorded and skipped). `systemctl is-active` /
    `is-enabled` answer from files in `state/`; `rpm -q` always says "not installed".
    `aws`, `google-chrome`, `curl`, `dnf`, `runuser` only record.
    """

    def __init__(self, root: Path):
        self.root = root
        self.shims = root / "shims"
        self.utils = root / "utils"
        self.state = root / "state"
        self.calls_file = root / "calls.tsv"
        self.crontab = root / "crontab.txt"
        for d in (self.shims, self.utils, self.state, root / "tmp"):
            d.mkdir(parents=True, exist_ok=True)
        record = 'printf "%s\\t%s\\n" "${0##*/}" "$*" >> "$SHIM_CALLS"\n'
        bodies = {
            "crontab": (
                'case "$*" in\n'
                '  "-u ec2-user -l") [ -f "$SHIM_CRONTAB" ] || { echo "no crontab for ec2-user" >&2; exit 1; }; '
                'cat "$SHIM_CRONTAB" ;;\n'
                '  "-u ec2-user -r") rm -f "$SHIM_CRONTAB" ;;\n'
                '  "-u ec2-user -") cat > "$SHIM_CRONTAB" ;;\n'
                '  *) echo "fake crontab: unexpected $*" >&2; exit 2 ;;\n'
                'esac\n'),
            "install": (
                'dir=0; mode=""\n'
                'while [ $# -gt 0 ]; do case "$1" in -d) dir=1; shift ;; -o|-g|-m) shift 2 ;; *) break ;; esac; done\n'
                'case "$*" in *"$SHIM_ROOT"*) ;; *) exit 0 ;; esac\n'
                'if [ "$dir" = 1 ]; then mkdir -p "$1"; else cp "$1" "$2"; fi\n'),
            "systemctl": (
                'case "$1" in\n'
                '  is-active) [ -f "$SHIM_STATE/active-${3:-$2}" ] ;;\n'
                '  is-enabled) [ -f "$SHIM_STATE/enabled-${3:-$2}" ] ;;\n'
                '  enable) for u in "$@"; do : > "$SHIM_STATE/enabled-$u"; done ;;\n'
                '  *) exit 0 ;;\n'
                'esac\n'),
            "rpm": "exit 1\n",
        }
        for name in SHIMMED:
            p = self.shims / name
            p.write_text("#!/bin/sh\n" + record + bodies.get(name, "exit 0\n"))
            p.chmod(0o755)
        for name in REAL_UTILS:
            real = next(Path(d) / name for d in ("/usr/bin", "/bin") if (Path(d) / name).exists())
            (self.utils / name).symlink_to(real)
        self.path = f"{self.shims}:{self.utils}"
        self.env = {"SHIM_CALLS": str(self.calls_file), "SHIM_CRONTAB": str(self.crontab),
                    "SHIM_STATE": str(self.state), "SHIM_ROOT": str(root),
                    "TMPDIR": str(root / "tmp")}
        self._check()

    def _check(self):
        probe = 'for c in "$@"; do printf "%s\\t%s\\n" "$c" "$(type -P "$c")"; done'
        r = subprocess.run([BASH, "--noprofile", "--norc", "-c", probe, "probe", *SHIMMED],
                           env={"PATH": self.path}, capture_output=True, text=True, timeout=10)
        resolved = dict(line.split("\t") for line in r.stdout.splitlines())
        bad = {c: w for c, w in resolved.items() if Path(w).parent != self.shims}
        assert not bad and len(resolved) == len(SHIMMED), f"commands not shimmed: {bad}"

    def run(self, snippet):
        r = bash_source(snippet, path=self.path, env=self.env, cwd=self.root)
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def calls(self, name=None):
        if not self.calls_file.exists():
            return []
        rows = [tuple((line.split("\t", 1) + [""])[:2])
                for line in self.calls_file.read_text().splitlines()]
        return [args for n, args in rows if n == name] if name else rows

    def reset_calls(self):
        self.calls_file.unlink(missing_ok=True)


@pytest.fixture
def box(tmp_path):
    return ShimBox(tmp_path / "box")


def _cron_snippet(box, dry_run=0):
    return f'LOG_DIR="{box.root}/logs"; DRY_RUN={dry_run}; remove_old_cron_lines'


def test_remove_old_cron_lines_keeps_other_lines_and_backs_up(box):
    box.crontab.write_text(OLD_CRONTAB)
    rc, out, err = box.run(_cron_snippet(box))
    assert rc == 0, err
    assert box.crontab.read_text() == OLD_CRONTAB_FILTERED
    assert "removing 2 run_pipeline.sh line(s)" in out
    backups = list((box.root / "logs").glob("crontab-ec2-user.*.bak"))
    assert len(backups) == 1 and re.fullmatch(r"crontab-ec2-user\.\d{8}T\d{6}Z\.bak", backups[0].name)
    assert backups[0].read_text() == OLD_CRONTAB
    assert box.calls("crontab") == ["-u ec2-user -l", "-u ec2-user -"]
    assert ["ec2-user:ec2-user " + str(backups[0])] == box.calls("chown")
    assert ["0600 " + str(backups[0])] == box.calls("chmod")
    # Idempotent: a second run reads and writes nothing.
    box.reset_calls()
    rc, out, err = box.run(_cron_snippet(box))
    assert rc == 0 and "no run_pipeline.sh lines" in out
    assert box.calls("crontab") == ["-u ec2-user -l"]
    assert len(list((box.root / "logs").glob("*.bak"))) == 1


def test_remove_old_cron_lines_removes_a_crontab_left_empty(box):
    box.crontab.write_text("0 4 * * 5 /home/ec2-user/wayakit-price-prediction/run_pipeline.sh\n"
                           "0 4 * * 6 /home/ec2-user/wayakit-price-prediction/run_pipeline.sh --stage 2\n")
    rc, out, err = box.run(_cron_snippet(box))
    assert rc == 0, err
    assert not box.crontab.exists()
    assert box.calls("crontab") == ["-u ec2-user -l", "-u ec2-user -r"]


def test_remove_old_cron_lines_dry_run_writes_nothing(box):
    box.crontab.write_text(OLD_CRONTAB)
    rc, out, err = box.run(_cron_snippet(box, dry_run=1))
    assert rc == 0, err
    assert box.crontab.read_text() == OLD_CRONTAB
    assert "[dry-run] would save the current crontab" in out
    assert box.calls("crontab") == ["-u ec2-user -l"]
    assert not (box.root / "logs").exists()


@pytest.mark.parametrize("content", [None, "*/5 * * * * /usr/bin/other\n"], ids=["no-crontab", "other"])
def test_remove_old_cron_lines_without_pipeline_lines_changes_nothing(box, content):
    if content is not None:
        box.crontab.write_text(content)
    rc, out, err = box.run(_cron_snippet(box))
    assert rc == 0, err
    assert "no run_pipeline.sh lines" in out
    assert box.calls("crontab") == ["-u ec2-user -l"]
    assert (box.crontab.read_text() if box.crontab.exists() else None) == content


def _env_snippet(box, topic="", primary="", fallback="", dry_run=0):
    return (f'ENV_FILE="{box.root}/price-intelligence.env"; LOG_DIR="{box.root}/logs"; '
            f'TOPIC_ARN="{topic}"; PRIMARY_MODEL="{primary}"; FALLBACK_MODEL="{fallback}"; '
            f'DRY_RUN={dry_run}; ensure_env_file')


TOPIC = "arn:aws:sns:eu-north-1:000000000000:price-intelligence-alerts"
PRIMARY = "arn:aws:bedrock:eu-north-1:000000000000:application-inference-profile/fakeprimary1"
FALLBACK = "arn:aws:bedrock:eu-north-1:000000000000:application-inference-profile/fakefallbk1"


def test_ensure_env_file_writes_root_0644_and_never_prints_values(box):
    rc, out, err = box.run(_env_snippet(box, TOPIC, PRIMARY, FALLBACK))
    assert rc == 0, err
    env_file = box.root / "price-intelligence.env"
    assert env_file.read_text().splitlines()[1:] == [
        f"PI_LOG_DIR={box.root}/logs", f"PI_ALERTS_TOPIC_ARN={TOPIC}",
        f"PI_RELEVANCE_PRIMARY_MODEL={PRIMARY}", f"PI_RELEVANCE_FALLBACK_MODEL={FALLBACK}"]
    installs = box.calls("install")
    assert any(a.startswith("-o root -g root -m 0644 ") and a.endswith(str(env_file)) for a in installs)
    for key in ("PI_LOG_DIR", "PI_ALERTS_TOPIC_ARN", "PI_RELEVANCE_PRIMARY_MODEL",
                "PI_RELEVANCE_FALLBACK_MODEL"):
        assert f"  {key}: set" in out
    assert ACCOUNT not in out + err and "arn:" not in out + err
    assert "WARNING" not in out
    # Second run: same content, nothing rewritten.
    box.reset_calls()
    rc, out, err = box.run(_env_snippet(box, TOPIC, PRIMARY, FALLBACK))
    assert rc == 0 and "up to date" in out
    assert not any(a.endswith(str(env_file)) for a in box.calls("install"))
    assert not list((box.root / "tmp").iterdir()), "the temporary file is removed"


def test_ensure_env_file_warns_without_topic_and_dry_run_writes_nothing(box):
    rc, out, err = box.run(_env_snippet(box, dry_run=1))
    assert rc == 0, err
    assert "WARNING: PI_ALERTS_TOPIC_ARN is not set" in out
    assert "  PI_ALERTS_TOPIC_ARN: not set" in out and "  PI_LOG_DIR: set" in out
    assert not (box.root / "price-intelligence.env").exists()
    assert box.calls("install") == []


def test_ensure_service_installs_and_enables_but_never_starts(box):
    dst = box.root / "systemd" / "pi-pipeline.service"
    dst.parent.mkdir()
    snippet = f'REPO_DIR="{REPO_ROOT}"; SERVICE_DST="{dst}"; DRY_RUN=0; ensure_service'
    rc, out, err = box.run(snippet)
    assert rc == 0, err
    assert dst.read_text() == SERVICE.read_text()
    assert box.calls("install") == [f"-o root -g root -m 0644 {SERVICE} {dst}"]
    assert box.calls("systemctl") == ["daemon-reload", "is-enabled --quiet pi-pipeline.service",
                                      "enable pi-pipeline.service"]
    box.reset_calls()
    rc, out, err = box.run(snippet)
    assert rc == 0, err
    assert "pi-pipeline.service: up to date." in out and "pi-pipeline.service: enabled." in out
    assert box.calls("install") == []
    assert box.calls("systemctl") == ["is-enabled --quiet pi-pipeline.service"]
    every = " ".join(box.calls("systemctl"))
    assert "start" not in every and "--now" not in every


# --- bootstrap.sh main(): the old crontab lines go first (F3 review) ---------------------------
def _main_box(box, *, failing=None):
    """Lets `main` run in the box: a fake `id` (root), `dirname` for the repo-path check, and the
    SSM agent already active. `failing` = "chrome" makes `dnf install <chrome rpm>` fail; "conda"
    makes the Miniforge download (curl) fail."""
    (box.shims / "id").write_text("#!/bin/sh\necho 0\n")
    (box.shims / "id").chmod(0o755)
    (box.utils / "dirname").symlink_to(next(Path(d) / "dirname" for d in ("/usr/bin", "/bin")
                                            if (Path(d) / "dirname").exists()))
    (box.state / "active-amazon-ssm-agent").write_text("")
    record = 'printf "%s\\t%s\\n" "${0##*/}" "$*" >> "$SHIM_CALLS"\n'
    if failing == "chrome":
        (box.shims / "google-chrome").unlink()
        (box.shims / "dnf").write_text('#!/bin/sh\n' + record
                                       + 'case "$*" in *google-chrome*) exit 1 ;; esac\nexit 0\n')
    elif failing == "conda":
        (box.shims / "curl").write_text("#!/bin/sh\n" + record + "exit 22\n")


def _main_snippet(box, *args):
    """`main` with the box's paths: the repo is expected where this test file's repo is."""
    quoted = " ".join(f"'{a}'" for a in args)
    return (f'REPO_DIR_EXPECTED="{REPO_ROOT}"; ENV_FILE="{box.root}/price-intelligence.env"; '
            f'SERVICE_DST="{box.root}/systemd/pi-pipeline.service"; CONDA_DIR="{box.root}/miniconda"; '
            f'main --log-dir "{box.root}/logs" {quoted}')


def _assert_crontab_filtered_first(box):
    assert box.crontab.read_text() == OLD_CRONTAB_FILTERED
    backups = list((box.root / "logs").glob("crontab-ec2-user.*.bak"))
    assert len(backups) == 1 and backups[0].read_text() == OLD_CRONTAB
    names = [name for name, _ in box.calls()]
    last_crontab = max(i for i, n in enumerate(names) if n == "crontab")
    later = {"dnf", "curl", "runuser", "systemctl", "google-chrome", "sha256sum"}
    assert not later & set(names[:last_crontab]), names


@pytest.mark.parametrize("failing, args", [("chrome", ["--skip-conda"]), ("conda", ["--skip-chrome"])])
def test_main_failing_chrome_or_conda_still_leaves_the_crontab_filtered(box, failing, args):
    """The old crontab lines are removed before any step that can fail: a failed Chrome or conda
    install aborts `main` (set -e) but the old Friday and `--stage 2` lines are already gone."""
    box.crontab.write_text(OLD_CRONTAB)
    _main_box(box, failing=failing)
    rc, out, err = box.run(_main_snippet(box, *args))
    assert rc != 0, out + err
    assert "bootstrap: done." not in out
    _assert_crontab_filtered_first(box)
    assert not (box.root / "systemd" / "pi-pipeline.service").exists()  # ensure_service never ran
    assert "command not found" not in err, err


def test_main_success_removes_the_crontab_lines_before_everything_else(box):
    box.crontab.write_text(OLD_CRONTAB)
    _main_box(box)
    (box.root / "systemd").mkdir()
    rc, out, err = box.run(_main_snippet(box, "--skip-chrome", "--skip-conda"))
    assert rc == 0, out + err
    assert out.rstrip().endswith("bootstrap: done. The pipeline runs on the next scheduled boot "
                                 "(last Thursday of the month, 21:50 America/Mexico_City).")
    _assert_crontab_filtered_first(box)
    lines = out.splitlines()
    first_cron = next(i for i, line in enumerate(lines) if "crontab of ec2-user" in line)
    assert first_cron < next(i for i, line in enumerate(lines) if line.startswith("bootstrap: SSM agent"))
    assert all("crontab" not in line for line in lines[first_cron + 2:-1]), "cron step runs once"


def test_main_dry_run_reports_the_crontab_step_first_and_changes_nothing(box):
    box.crontab.write_text(OLD_CRONTAB)
    _main_box(box)
    rc, out, err = box.run(_main_snippet(box, "--dry-run", "--skip-chrome", "--skip-conda"))
    assert rc == 0, out + err
    lines = out.splitlines()
    assert lines[0] == "bootstrap: DRY RUN: nothing will be changed."
    assert lines[1].startswith("bootstrap: crontab of ec2-user: removing 2 run_pipeline.sh line(s)")
    assert box.crontab.read_text() == OLD_CRONTAB
    assert box.calls("crontab") == ["-u ec2-user -l"]


def test_every_step_in_dry_run_changes_nothing(box):
    """All the ensure_* steps on a bare machine in dry-run: only checks run, nothing installs."""
    box.crontab.write_text(OLD_CRONTAB)
    dst = box.root / "systemd" / "pi-pipeline.service"
    snippet = (f'REPO_DIR="{REPO_ROOT}"; SERVICE_DST="{dst}"; CONDA_DIR="{box.root}/miniconda"; '
               + _env_snippet(box, TOPIC, dry_run=1).replace("ensure_env_file", "")
               + 'ensure_ssm_agent && ensure_packages && ensure_chrome && ensure_conda && '
               'ensure_env_file && ensure_service && remove_old_cron_lines')
    # google-chrome and aws are shims here; hide them so the "missing" branches run.
    (box.shims / "google-chrome").unlink()
    (box.shims / "aws").unlink()
    rc, out, err = box.run(snippet)
    assert rc == 0, out + err
    for name in ("dnf", "curl", "runuser", "chown", "chmod", "install", "google-chrome", "aws"):
        assert box.calls(name) == [], (name, box.calls(name))
    assert set(box.calls("systemctl")) <= {"is-active --quiet amazon-ssm-agent",
                                           "is-enabled --quiet pi-pipeline.service"}
    assert box.calls("crontab") == ["-u ec2-user -l"]
    assert box.crontab.read_text() == OLD_CRONTAB and not dst.exists()
    assert "[dry-run] would run: dnf install -y" in out
    assert "[dry-run] would run: systemctl enable pi-pipeline.service" in out
    assert ACCOUNT not in out + err


# =============================================================================================
# ec2_instance.py: a profile REPLACE needs the instance running (IncorrectState, 2026-11-22)
# =============================================================================================
@pytest.mark.parametrize("state", ["stopped", "stopping", "pending"])
@pytest.mark.parametrize("argv", [[], ["--rollback-profile", OLD_PROFILE]])
def test_replace_on_an_instance_not_running_exits_1_before_any_change(state, argv, capsys):
    current = NEW_PROFILE if argv else OLD_PROFILE
    aws = FakeAws(tags=[FOREIGN_TAG], metadata=dict(LEGACY_MD), profile=current, state=state)
    before = aws.snapshot()
    with pytest.raises(SystemExit) as exc:
        ec2i.main(argv, session=aws)
    assert "Cannot switch the instance profile: the instance is " + state in str(exc.value.code)
    assert "Nothing was changed." in str(exc.value.code)
    assert aws.mutations() == [], aws.calls
    assert aws.snapshot() == before
    assert_clean_output(capsys.readouterr().out + str(exc.value.code))


@pytest.mark.parametrize("argv", [[], ["--rollback-profile", OLD_PROFILE]])
def test_replace_dry_run_on_a_stopped_instance_warns_and_exits_0(argv, capsys):
    current = NEW_PROFILE if argv else OLD_PROFILE
    aws = FakeAws(profile=current, state="stopped")
    before = aws.snapshot()
    assert ec2i.main(argv + ["--dry-run"], session=aws) == 0
    out = capsys.readouterr().out
    assert aws.mutations() == [] and aws.snapshot() == before
    assert "[WARN] the apply would stop here: the instance is stopped" in out
    assert "Start it" in out and "Nothing was changed" not in out
    assert_clean_output(out)


def test_associate_and_converged_are_not_blocked_on_a_stopped_instance():
    aws = FakeAws(tags=[FOREIGN_TAG], metadata=dict(LEGACY_MD), profile=None, state="stopped")
    assert ec2i.main([], session=aws) == 0
    assert aws.converged() and "AssociateIamInstanceProfile" in aws.calls
    aws.calls.clear()
    assert ec2i.main([], session=aws) == 0 and aws.mutations() == []


@pytest.mark.parametrize("state,action,blocked", [
    ("running", "replace", False), ("stopped", "replace", True), ("stopping", "replace", True),
    ("pending", "replace", True), ("stopped", "associate", False), ("stopped", "none", False),
    (None, "replace", True)])
def test_replace_blocked_reason(state, action, blocked):
    instance = {"State": {"Name": state}} if state else {}
    reason = ec2i.replace_blocked_reason(instance, {"action": action})
    assert (reason is not None) == blocked
    assert ec2i.replace_blocked_reason({"State": {"Name": "stopped"}}, None) is None
