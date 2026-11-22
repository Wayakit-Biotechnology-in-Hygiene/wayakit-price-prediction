"""Static checks of the GitHub Actions workflows (F4): `.github/workflows/*.yml`.

PyYAML is not installed (and the workflows must not need it to be checked), so the files are
read as text with a small indentation-aware scanner: `run:` blocks, `uses:` lines, the `on:`
block, `permissions:` blocks and the job `if:`. What is checked (design: F4 spec, "deploy.yml"):

- Every `uses:` in every workflow is pinned to a full 40-hex commit SHA (a tag or branch can be
  moved by whoever controls the action's repo), with the tag in a comment; the three actions of
  deploy.yml are pinned to the reviewed SHAs.
- deploy.yml only triggers on `workflow_run` of CI (completed, branch main) and on
  `workflow_dispatch` with a required `sha` input: no push, pull_request or pull_request_target.
- The job only runs for dispatch or for a successful CI run of a push to main of this repository
  (not of a fork).
- `permissions` is minimal (`contents: read`, `id-token: write`), deploys never overlap and are
  never cancelled midway, and the job has a timeout.
- No `${{ ... }}` expression inside any `run:` script (script injection): inputs reach bash
  through `env:` only.
- The AWS role comes from a secret (never an ARN with an account ID in the file), the region is
  eu-north-1, the account ID is masked, and the release is built with `git archive | gzip -n`.

Nothing here runs a workflow or reaches the network.
"""
from __future__ import annotations

import re
import subprocess

import pytest

from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.unit

WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
DEPLOY = WORKFLOWS_DIR / "deploy.yml"

# Reviewed pins (F4 spec). Update only together with a review of the new action version.
PINNED = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "actions/setup-python": ("6d4fd22b20465ccc0f7354476588e8df7c9ca0e0", "v7.1.0"),
    "aws-actions/configure-aws-credentials": ("e1253824e5c10ff9df46874f81ed3ec929e19cfd", "v6.3.0"),
}
USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>[^\s#]+)\s*(?:#\s*(?P<comment>.*))?$")
PINNED_REF_RE = re.compile(r"^(?P<action>[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)@(?P<sha>[0-9a-f]{40})$")


# --- Tiny YAML-ish scanner ---------------------------------------------------------------------
def _strip_comment(line):
    """Drop a trailing ` # comment` outside quotes (good enough for workflow files)."""
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _key_indent(line):
    """Indentation of the key on this line (a `- key:` list item counts the dash)."""
    stripped = line.lstrip(" ")
    extra = 2 if stripped.startswith("- ") else 0
    return _indent(line) + extra


def run_blocks(text):
    """Every `run:` script as (line number, script text), block scalars included."""
    lines = text.splitlines()
    blocks = []
    i = 0
    key = re.compile(r"^(\s*)(-\s+)?run:\s*(.*)$")
    while i < len(lines):
        m = key.match(lines[i])
        if not m:
            i += 1
            continue
        own = _key_indent(lines[i])
        value = m.group(3)
        if re.fullmatch(r"[|>][+-]?\d*\s*(#.*)?", value.strip()):
            body = []
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > own):
                body.append(lines[j])
                j += 1
            blocks.append((i + 1, "\n".join(body)))
            i = j
        else:
            blocks.append((i + 1, value))
            i += 1
    return blocks


def block_under(text, key, *, indent=None):
    """Lines of the mapping under the first `key:` (at `indent` if given), as raw lines."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^\s*{re.escape(key)}:\s*(#.*)?$", line) and (indent is None or _indent(line) == indent):
            own = _indent(line)
            body = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and _indent(nxt) <= own:
                    break
                body.append(nxt)
            return body
    return None


def scripts_of(text):
    """All `run:` scripts joined, with backslash line continuations folded into one line."""
    return re.sub(r"\\\n\s*", " ", "\n".join(s for _, s in run_blocks(text)))


def uses_lines(text):
    return [(n + 1, m) for n, line in enumerate(text.splitlines()) if (m := USES_RE.match(line))]


def workflow_files():
    if not WORKFLOWS_DIR.is_dir():
        return []
    return sorted(p for p in WORKFLOWS_DIR.iterdir() if p.suffix in (".yml", ".yaml"))


@pytest.fixture(scope="module")
def deploy_text():
    assert DEPLOY.is_file(), f"{DEPLOY.relative_to(REPO_ROOT)} is missing"
    return DEPLOY.read_text(encoding="utf-8")


# --- Scanner self-tests (so a scanner bug cannot hide a finding) -------------------------------
SAMPLE = """\
on:
  workflow_dispatch:
jobs:
  a:
    steps:
      - run: echo one
      - name: two
        run: |
          echo "${{ inputs.sha }}"
          echo three
        env:
          X: y
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - run: >-
          echo four
"""


def test_scanner_finds_inline_and_block_run_scripts():
    blocks = run_blocks(SAMPLE)
    assert [b[1].strip().splitlines()[0] for b in blocks] == ["echo one", 'echo "${{ inputs.sha }}"', "echo four"]
    assert "X: y" not in blocks[1][1], "the block ends where the step's next key starts"
    assert [m.group("ref") for _, m in uses_lines(SAMPLE)] == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"]
    assert block_under(SAMPLE, "on", indent=0) == ["  workflow_dispatch:"]


# --- Every workflow ----------------------------------------------------------------------------
def test_there_is_a_deploy_workflow():
    assert DEPLOY in workflow_files()


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha_with_the_tag_in_a_comment(path):
    text = path.read_text(encoding="utf-8")
    found = uses_lines(text)
    for lineno, m in found:
        ref = m.group("ref")
        if ref.startswith("./"):
            continue  # a local action lives in this repo, at the same commit
        pinned = PINNED_REF_RE.match(ref)
        assert pinned, f"{path.name}:{lineno}: '{ref}' is not pinned to a 40-hex commit SHA"
        assert m.group("comment") and re.search(r"v\d", m.group("comment")), \
            f"{path.name}:{lineno}: put the tag in a comment after the SHA (e.g. '# v7.0.1')"
        action = pinned.group("action")
        if action in PINNED:
            sha, tag = PINNED[action]
            assert pinned.group("sha") == sha, f"{path.name}:{lineno}: {action} is not the reviewed pin"
            assert tag in m.group("comment"), f"{path.name}:{lineno}: comment should name {tag}"


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_no_expression_is_inlined_in_a_run_script(path):
    """`${{ ... }}` inside `run:` is pasted into the script before bash parses it: a crafted input
    or branch name becomes code. Values reach bash through `env:`."""
    text = path.read_text(encoding="utf-8")
    bad = [(n, s) for n, s in run_blocks(text) if "${{" in s]
    assert not bad, f"{path.name}: expressions inside run: at lines {[n for n, _ in bad]}"


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_no_account_id_arn_or_email_in_workflows(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"(?<![0-9a-f])[0-9]{12}(?![0-9a-f])", text), f"{path.name}: 12-digit number"
    assert "arn:aws:" not in text, f"{path.name}: ARN in the file (use a secret)"
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), f"{path.name}: email in the file"
    assert not re.search(r"\bi-[0-9a-f]{8,17}\b", text), f"{path.name}: instance ID in the file"


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_no_workflow_uses_pull_request_target(path):
    """pull_request_target runs with secrets on code from forks."""
    assert "pull_request_target" not in path.read_text(encoding="utf-8")


# --- deploy.yml --------------------------------------------------------------------------------
def test_deploy_uses_exactly_the_three_reviewed_actions(deploy_text):
    actions = {PINNED_REF_RE.match(m.group("ref")).group("action") for _, m in uses_lines(deploy_text)}
    assert actions == set(PINNED)


def test_deploy_triggers_only_on_ci_of_main_and_manual_dispatch(deploy_text):
    on = block_under(deploy_text, "on", indent=0)
    assert on is not None, "top-level 'on:' block not found"
    on_text = "\n".join(_strip_comment(line) for line in on)
    top = [line.strip().rstrip(":") for line in on if line.strip() and _indent(line) == min(
        _indent(x) for x in on if x.strip())]
    assert sorted(top) == ["workflow_dispatch", "workflow_run"], top
    assert re.search(r"workflows:\s*\[\s*['\"]?CI['\"]?\s*\]", on_text) or \
        re.search(r"workflows:\s*\n\s*-\s*['\"]?CI['\"]?", on_text), on_text
    assert re.search(r"types:\s*\[\s*completed\s*\]", on_text) or \
        re.search(r"types:\s*\n\s*-\s*completed", on_text), on_text
    assert re.search(r"branches:\s*\[\s*['\"]?main['\"]?\s*\]", on_text) or \
        re.search(r"branches:\s*\n\s*-\s*['\"]?main['\"]?\s*$", on_text, re.M), on_text
    for forbidden in ("push", "pull_request", "schedule", "repository_dispatch", "workflow_call"):
        assert not re.search(rf"^\s*{forbidden}:", on_text, re.M), forbidden


def test_deploy_dispatch_requires_a_sha_input(deploy_text):
    dispatch = block_under(deploy_text, "workflow_dispatch")
    assert dispatch, "workflow_dispatch needs inputs"
    text = "\n".join(dispatch)
    assert re.search(r"^\s*sha:\s*$", text, re.M), text
    assert re.search(r"required:\s*true", text), text


def test_deploy_job_condition_only_accepts_a_successful_push_ci_on_main_of_this_repo(deploy_text):
    conds = [_strip_comment(line) for line in deploy_text.splitlines() if re.match(r"^\s{4}if:", line)]
    assert conds, "the deploy job needs an 'if:'"
    # The condition may span several lines (folded scalar): take the whole job-level if block.
    lines = deploy_text.splitlines()
    start = next(i for i, line in enumerate(lines) if re.match(r"^\s{4}if:", line))
    cond = [lines[start]]
    for nxt in lines[start + 1:]:
        if nxt.strip() and _indent(nxt) <= 4:
            break
        cond.append(nxt)
    cond = " ".join(cond)
    for needed in ("workflow_dispatch", "github.event.workflow_run.conclusion == 'success'",
                   "github.event.workflow_run.event == 'push'",
                   "github.event.workflow_run.head_branch == 'main'",
                   "github.event.workflow_run.head_repository.full_name == github.repository"):
        assert needed in re.sub(r"\s+", " ", cond), needed


def _permissions_blocks(text):
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)permissions:\s*(.*)$", line)
        if not m:
            continue
        inline = _strip_comment(m.group(2)).strip()
        if inline:
            out.append(inline)
            continue
        body = {}
        for nxt in lines[i + 1:]:
            if nxt.strip() and _indent(nxt) <= len(m.group(1)):
                break
            if nxt.strip() and not nxt.strip().startswith("#"):
                k, _, v = _strip_comment(nxt).strip().partition(":")
                body[k.strip()] = v.strip()
        out.append(body)
    return out


def test_deploy_permissions_are_minimal(deploy_text):
    blocks = _permissions_blocks(deploy_text)
    assert blocks, "permissions must be set explicitly (the default token is broader)"
    for block in blocks:
        assert block == {"contents": "read", "id-token": "write"}, block


def test_deploy_runs_one_at_a_time_and_is_never_cancelled_midway(deploy_text):
    conc = block_under(deploy_text, "concurrency")
    assert conc is not None
    body = {k.strip(): v.strip() for k, _, v in (_strip_comment(x).strip().partition(":") for x in conc if x.strip())}
    assert body.get("group") == "pi-deploy"
    assert body.get("cancel-in-progress") == "false"
    m = re.search(r"^\s*timeout-minutes:\s*(\d+)\s*$", deploy_text, re.M)
    assert m and int(m.group(1)) == 40


def test_deploy_checks_out_the_full_history(deploy_text):
    """`git merge-base --is-ancestor` needs the history of origin/main."""
    assert re.search(r"fetch-depth:\s*0\b", deploy_text)


def test_deploy_resolves_and_validates_the_sha_in_bash(deploy_text):
    scripts = scripts_of(deploy_text)
    assert "^[0-9a-f]{40}$" in scripts, "dispatch input must be validated as 40 lowercase hex"
    assert "merge-base --is-ancestor" in scripts and "origin/main" in scripts
    assert "refs/heads/main" in scripts, "dispatch must come from main"


def test_deploy_builds_a_reproducible_archive(deploy_text):
    scripts = scripts_of(deploy_text)
    assert re.search(r"git archive --format=tar \"?\$\{?SHA\}?\"? \| gzip -n > release\.tar\.gz", scripts), scripts


def test_deploy_pins_boto_to_the_instance_versions(deploy_text):
    env_yml = (REPO_ROOT / "environment.yml").read_text(encoding="utf-8")
    boto3_v = re.search(r"boto3==([\d.]+)", env_yml).group(1)
    botocore_v = re.search(r"botocore==([\d.]+)", env_yml).group(1)
    scripts = scripts_of(deploy_text)
    assert f"boto3=={boto3_v}" in scripts and f"botocore=={botocore_v}" in scripts


def test_deploy_assumes_the_role_from_a_secret_with_the_account_masked(deploy_text):
    lines = deploy_text.splitlines()
    i = next(n for n, line in enumerate(lines) if "aws-actions/configure-aws-credentials@" in line)
    step = []
    for nxt in lines[i + 1:]:
        if re.match(r"^\s*-\s", nxt) and _indent(nxt) <= _indent(lines[i]):
            break
        step.append(_strip_comment(nxt).strip())
    with_ = dict(x.partition(":")[::2] for x in step if ":" in x)
    with_ = {k.strip(): v.strip().strip("'\"") for k, v in with_.items()}
    assert with_.get("role-to-assume") == "${{ secrets.PI_DEPLOY_ROLE_ARN }}"
    assert with_.get("aws-region") == "eu-north-1"
    assert with_.get("mask-aws-account-id") == "true"


def test_deploy_calls_the_release_script_with_env_credentials(deploy_text):
    scripts = scripts_of(deploy_text)
    m = re.search(r"python -m infra\.release deploy[^\n]*", scripts)
    assert m, scripts
    call = m.group(0)
    for flag in ("--env-credentials", "--sha", "--archive release.tar.gz", "--request-id"):
        assert flag in call, flag
    assert "--dry-run" not in call


# --- deploy.yml: steps and the real "resolve" script ---------------------------------------------
def steps(text):
    """Step blocks of the (single) job, in order, as raw text."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if re.match(r"^\s{6}-\s", line)]
    return ["\n".join(lines[a:b]) for a, b in zip(starts, starts[1:] + [len(lines)])]


def test_every_step_after_resolve_only_runs_when_there_is_something_to_deploy(deploy_text):
    """A superseded CI run (main moved on) exits 0 with deploy=false: nothing after it may run."""
    blocks = steps(deploy_text)
    i = next(n for n, b in enumerate(blocks) if re.search(r"^\s*id:\s*resolve\s*$", b, re.M))
    assert i == 1, "resolve runs right after checkout"
    for block in blocks[i + 1:]:
        assert re.search(r"^\s*if:\s*steps\.resolve\.outputs\.deploy == 'true'\s*$", block, re.M), block


def _resolve_script(deploy_text):
    block = next(b for b in steps(deploy_text) if re.search(r"^\s*id:\s*resolve\s*$", b, re.M))
    return next(s for _, s in run_blocks(block))


def _git(repo, *args):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(repo.parent), "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True,
                          check=True, timeout=30).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Local repo: main = A -> B (origin/main = B), plus a side commit C not on main."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    shas = {}
    for name in ("A", "B"):
        (r / "f").write_text(name)
        _git(r, "add", "f")
        _git(r, "commit", "-q", "-m", name)
        shas[name] = _git(r, "rev-parse", "HEAD")
    _git(r, "update-ref", "refs/remotes/origin/main", shas["B"])
    _git(r, "checkout", "-q", "-b", "side", shas["A"])
    (r / "f").write_text("C")
    _git(r, "commit", "-q", "-am", "C")
    shas["C"] = _git(r, "rev-parse", "HEAD")
    return r, shas


def _resolve(deploy_text, repo_dir, **env):
    out = repo_dir.parent / "github_output"
    out.write_text("")
    full = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(repo_dir.parent), "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GITHUB_OUTPUT": str(out), "EVENT_NAME": "", "GIT_REF": "",
            "INPUT_SHA": "", "RUN_HEAD_SHA": "", **env}
    r = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-c", _resolve_script(deploy_text)], cwd=repo_dir,
                       env=full, capture_output=True, text=True, timeout=30)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return r.returncode, outputs, r.stdout + r.stderr


def test_resolve_dispatch_of_an_older_main_commit_is_a_rollback(deploy_text, repo):
    r, shas = repo
    rc, outputs, log = _resolve(deploy_text, r, EVENT_NAME="workflow_dispatch", GIT_REF="refs/heads/main",
                                INPUT_SHA=shas["A"])
    assert rc == 0, log
    assert outputs == {"sha": shas["A"], "deploy": "true"}


@pytest.mark.parametrize("case", ["side-commit", "other-ref", "uppercase", "short", "injection", "empty"])
def test_resolve_dispatch_refusals(deploy_text, repo, case):
    r, shas = repo
    env = {"EVENT_NAME": "workflow_dispatch", "GIT_REF": "refs/heads/main", "INPUT_SHA": shas["A"]}
    marker = r.parent / "pwned"
    env.update({
        "side-commit": {"INPUT_SHA": shas["C"]},
        "other-ref": {"GIT_REF": "refs/heads/side"},
        "uppercase": {"INPUT_SHA": shas["A"].upper()},
        "short": {"INPUT_SHA": shas["A"][:12]},
        "injection": {"INPUT_SHA": f'$(touch {marker})'},
        "empty": {"INPUT_SHA": ""},
    }[case])
    rc, outputs, log = _resolve(deploy_text, r, **env)
    assert rc != 0, log
    assert outputs.get("deploy") != "true" and "sha" not in outputs
    assert not marker.exists()


def test_resolve_ci_run_of_the_main_tip_deploys_it(deploy_text, repo):
    r, shas = repo
    rc, outputs, log = _resolve(deploy_text, r, EVENT_NAME="workflow_run", RUN_HEAD_SHA=shas["B"])
    assert rc == 0, log
    assert outputs == {"sha": shas["B"], "deploy": "true"}


def test_resolve_superseded_ci_run_skips_without_failing(deploy_text, repo):
    r, shas = repo
    rc, outputs, log = _resolve(deploy_text, r, EVENT_NAME="workflow_run", RUN_HEAD_SHA=shas["A"])
    assert rc == 0, log
    assert outputs == {"deploy": "false"}
    assert "superseded" in log


@pytest.mark.parametrize("head", ["", "x" * 40, "main"])
def test_resolve_ci_run_with_a_bad_head_sha_fails(deploy_text, repo, head):
    r, _ = repo
    rc, outputs, _ = _resolve(deploy_text, r, EVENT_NAME="workflow_run", RUN_HEAD_SHA=head)
    assert rc != 0 and outputs.get("deploy") != "true"


def test_deploy_request_id_fits_release_py(deploy_text):
    """`<run_id>-<run_attempt>` must pass release.py's --request-id rule."""
    from infra import release
    assert re.search(r"REQUEST_ID:\s*\$\{\{ github\.run_id \}\}-\$\{\{ github\.run_attempt \}\}", deploy_text)
    assert release.REQUEST_ID_PATTERN.fullmatch("12345678901-1")


def test_checkout_does_not_persist_the_token(deploy_text):
    assert re.search(r"persist-credentials:\s*false", deploy_text)
