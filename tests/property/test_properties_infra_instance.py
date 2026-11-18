"""Property tests of the instance scripts: `infra/ec2_instance.py` and `infra/instance/bootstrap.sh`.

Rules checked over generated inputs:
- `ec2_instance.py`, from any starting state of the instance (tags, metadata options, associated
  profile, shutdown behavior): dry-run makes no mutating call and changes nothing; one apply
  converges (project and Name tags, IMDSv2 required with hop limit 2, PI profile associated) and
  keeps foreign tags; a second apply only reads; rollback then adopt converges again and
  rollback never touches tags or IMDS. No mode ever prints the account or the instance ID.
- an apply (adopt or rollback) whose profile is missing or has no role exits 1 having made no
  mutating call and left tags, IMDS, association and shutdown behavior exactly as they were.
- `metadata_changes` is {} exactly when the desired options are present; `association_plan`
  only replaces when another profile is associated and compares by name.
- `user_data` quotes every value: bash parses the branch, repo URL and bootstrap arguments back
  to exactly the strings given, whatever they contain.
- `bootstrap.sh` `filter_crontab` equals a Python reference model (drop the non-comment lines
  that contain `run_pipeline.sh`), is idempotent, never adds lines and keeps every other line
  byte for byte.
- `render_env_file` and `env_file_value` round-trip every value the validators accept, and the
  validators reject any value with a space, quote, `$`, `;`, backslash or newline.
- `run_pipeline.sh` `redact_secrets` (the failure alert's log excerpt): equals pi-dev's Python
  reference (names key/token/secret/password/authorization in any ASCII case, also inside a longer
  word; the leftmost "rest of the line" form `name: v`, `"name": "v"`, `name = v`,
  `Authorization: Bearer v`... redacts to the end of the line; before it, each plain `name=value`
  shell word, quotes and glued text included, becomes `name=<redacted>`; then the 500-character
  cap). No generated secret value survives it in any of those forms, wherever it is on the line;
  it is idempotent; lines without a secret name come out unchanged apart from the cap; the cap is
  applied after redacting. The function is extracted from the script and run alone (the script
  itself is never sourced: its trap shuts the machine down).

No AWS: the apply loop runs against the stateful `FakeAws` of the unit tests. The bash functions
run with an empty `PATH` (builtins only).
"""
from __future__ import annotations

import copy
import io
import os
import re
import subprocess
from contextlib import redirect_stdout

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from infra import _aws, config, ec2_instance as ec2i
from tests.conftest import REPO_ROOT
from tests.unit.test_infra_instance import (ACCOUNT, IID, MUTATING_OPS, NEW_PROFILE, OLD_PROFILE,
                                            FakeAws, bash_source)

pytestmark = pytest.mark.property

NAME = config.INSTANCE_NAME

# --- ec2_instance.py: apply loop from any starting state ----------------------------------------
tag_keys = st.sampled_from(["Name", "project", "owner", "env", "aws-backup", "cost-center"])
tag_values = st.sampled_from([NAME, "price-intelligence", "other", "", "wayakit-app-server-old"])
tag_lists = st.dictionaries(tag_keys, tag_values, max_size=5).map(_aws.to_tag_list)
metadata = st.fixed_dictionaries({
    "State": st.just("applied"),
    "HttpTokens": st.sampled_from(["optional", "required"]),
    "HttpPutResponseHopLimit": st.integers(1, 64),
    "HttpEndpoint": st.sampled_from(["enabled", "disabled"]),
})
profiles = st.sampled_from([None, OLD_PROFILE, NEW_PROFILE, "some-other-profile"])
shutdowns = st.sampled_from(["stop", "terminate"])


def _quiet(fn, *args, **kwargs):
    out = io.StringIO()
    with redirect_stdout(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


def _assert_clean(text):
    assert ACCOUNT not in text and IID not in text, text


@given(tag_lists, metadata, profiles, shutdowns)
@example([], {"State": "applied", "HttpTokens": "optional", "HttpPutResponseHopLimit": 1,
              "HttpEndpoint": "enabled"}, OLD_PROFILE, "stop")          # today's instance (assumed)
@example([{"Key": "project", "Value": "other"}], {"State": "applied", "HttpTokens": "required",
         "HttpPutResponseHopLimit": 2, "HttpEndpoint": "enabled"}, NEW_PROFILE, "terminate")
def test_dry_run_then_apply_then_apply_again(tags, md, profile, shutdown):
    aws = FakeAws(tags=tags, metadata=md, profile=profile, shutdown=shutdown,
                  profiles=(NEW_PROFILE, OLD_PROFILE, "some-other-profile"))
    foreign = {k: v for k, v in _aws.tags_to_dict(tags).items() if k not in ("Name", "project")}
    before = aws.snapshot()

    rc, out = _quiet(ec2i.main, ["--dry-run"], session=aws)
    assert rc == 0 and aws.mutations() == [] and aws.snapshot() == before
    _assert_clean(out)

    aws.calls.clear()
    rc, out = _quiet(ec2i.main, [], session=aws)
    assert rc == 0 and aws.converged()
    have = _aws.tags_to_dict(aws.instance["Tags"])
    assert {k: have[k] for k in foreign} == foreign, "foreign tags kept"
    assert aws.shutdown == shutdown, "shutdown behavior is never changed"
    assert set(aws.mutations()) <= MUTATING_OPS - {"RunInstances"}
    assert len(aws.mutations()) == len(set(aws.mutations())), "each fix at most once"
    if profile not in (None, NEW_PROFILE):
        assert f"--rollback-profile {profile}" in out
    _assert_clean(out)

    aws.calls.clear()
    rc, out = _quiet(ec2i.main, [], session=aws)
    assert rc == 0 and aws.mutations() == [], aws.calls
    _assert_clean(out)


@given(tag_lists, metadata, profiles)
def test_rollback_only_moves_the_profile(tags, md, profile):
    aws = FakeAws(tags=tags, metadata=md, profile=profile)
    _quiet(ec2i.main, [], session=aws)
    tags_md = copy.deepcopy((aws.instance["Tags"], aws.instance["MetadataOptions"]))
    aws.calls.clear()
    _quiet(ec2i.main, ["--rollback-profile", OLD_PROFILE], session=aws)
    assert aws.associated_profile() == OLD_PROFILE
    assert set(aws.mutations()) == {"ReplaceIamInstanceProfileAssociation"}
    assert (aws.instance["Tags"], aws.instance["MetadataOptions"]) == tags_md
    _quiet(ec2i.main, [], session=aws)
    assert aws.converged()


@given(tag_lists, metadata, profiles, shutdowns, st.sampled_from(["missing", "no-role"]),
       st.booleans())
@example([], {"State": "applied", "HttpTokens": "optional", "HttpPutResponseHopLimit": 1,
              "HttpEndpoint": "enabled"}, OLD_PROFILE, "stop", "missing", False)  # finding 4
def test_apply_with_a_bad_profile_changes_nothing(tags, md, profile, shutdown, problem, rollback):
    target = OLD_PROFILE if rollback else NEW_PROFILE
    others = {NEW_PROFILE, OLD_PROFILE, "some-other-profile"} - {target}
    aws = FakeAws(tags=tags, metadata=md, profile=profile, shutdown=shutdown,
                  profiles=others | ({target} if problem == "no-role" else set()),
                  roleless={target} if problem == "no-role" else ())
    before = aws.snapshot()
    argv = ["--rollback-profile", target] if rollback else []
    with pytest.raises(SystemExit) as exc:
        _quiet(ec2i.main, argv, session=aws)
    assert exc.value.code not in (0, None)
    assert aws.mutations() == [], aws.calls
    assert aws.snapshot() == before


@given(st.one_of(st.none(), metadata, st.dictionaries(
    st.sampled_from(["HttpTokens", "HttpPutResponseHopLimit", "HttpEndpoint", "State",
                     "HttpProtocolIpv6"]),
    st.sampled_from(["required", "optional", 2, 1, "enabled", "disabled", "applied"]))))
def test_metadata_changes_is_empty_exactly_when_converged(current):
    desired = ec2i.desired_metadata_options()
    converged = all((current or {}).get(k) == v for k, v in desired.items())
    assert (ec2i.metadata_changes(current) == {}) is converged
    if not converged:
        assert ec2i.metadata_changes(current) == desired


profile_names = st.from_regex(r"[A-Za-z0-9+=,.@_-]{1,20}", fullmatch=True)
paths = st.sampled_from(["", "pi/", "a/b/"])


@given(profile_names, profile_names, paths, st.sampled_from(["associating", "associated",
                                                             "disassociating", "disassociated"]))
def test_association_plan(current, desired, path, state):
    arn = f"arn:aws:iam::{ACCOUNT}:instance-profile/{path}{current}"
    plan = ec2i.association_plan([{"AssociationId": "a-1", "State": state,
                                   "IamInstanceProfile": {"Arn": arn}}],
                                 config.instance_profile_arn(ACCOUNT, desired))
    if state not in ("associating", "associated"):
        assert plan == {"action": "associate", "association_id": None, "previous_profile": None}
    elif current == desired:
        assert plan["action"] == "none"
    else:
        assert plan == {"action": "replace", "association_id": "a-1", "previous_profile": current}


# --- user_data quoting --------------------------------------------------------------------------
shell_text = st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
                     min_size=1, max_size=30)


_FAKE_COMMANDS = (
    'dnf() { :; }\n'
    'runuser() { printf "%s\\0" "$@" > "$OUT/clone.bin"; }\n'
    'bash() { printf "%s\\0" "$@" > "$OUT/boot.bin"; }\n'
)


def _run_user_data(text, out):
    """Runs the whole userData in bash with functions in place of dnf/runuser/bash (functions win
    over the PATH, which is empty): what each command would receive is written to `out`."""
    script = _FAKE_COMMANDS + text.split("\n", 1)[1]  # drop the shebang line
    r = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", script],
                       env={"PATH": "", "LC_ALL": "C", "OUT": str(out)}, capture_output=True, timeout=10)
    assert r.returncode == 0, r.stderr

    def argv(name):
        data = (out / name).read_bytes()
        return [a.decode("utf-8", "surrogateescape") for a in data.split(b"\0")[:-1]]
    return argv("clone.bin"), argv("boot.bin")


@given(shell_text, shell_text, st.lists(shell_text, max_size=4))
@example("main; rm -rf / #", "https://x/$(id)`id`.git", ["--primary-model", "a'b\"c\nd"])
@example("x\nfi\ny", "u", ["\nfi"])
def test_user_data_values_reach_bash_verbatim(tmp_path_factory, branch, repo_url, extra):
    assert not os.path.isdir(ec2i.REPO_DIR + "/.git"), "not on a PI instance"
    bootstrap = ["--log-dir", "/home/ec2-user/logs", *extra]
    clone, boot = _run_user_data(ec2i.user_data(repo_url, branch, bootstrap),
                                 tmp_path_factory.mktemp("userdata"))
    assert clone == ["-u", "ec2-user", "--", "git", "clone", "--branch", branch, "--depth", "1",
                     repo_url, ec2i.REPO_DIR]
    assert boot == [f"{ec2i.REPO_DIR}/infra/instance/bootstrap.sh", *bootstrap]


# --- bootstrap.sh: filter_crontab ---------------------------------------------------------------
CRON_PIECES = ["run_pipeline.sh", "#", " ", "\t", "0 4 * * 5 ", "/home/ec2-user/wayakit-price-prediction/",
               "--stage 2", ">> /home/ec2-user/logs/cron.log 2>&1", "run_pipeline", ".sh", "\\", "*",
               "MAILTO=", "é", "\r", "other-job"]
cron_lines = st.one_of(
    st.lists(st.sampled_from(CRON_PIECES), max_size=6).map("".join),
    st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\n\x00"), max_size=40),
)
crontabs = st.tuples(st.lists(cron_lines, max_size=12), st.booleans())


def _crontab_bytes(lines_and_eol):
    lines, final_newline = lines_and_eol
    text = "\n".join(lines) + ("\n" if final_newline and lines else "")
    return text.encode("utf-8")


def _reference_filter(data: bytes) -> bytes:
    """Drop lines containing run_pipeline.sh unless they start with optional blanks and '#'.
    Every kept line ends with a newline (bash `read` + `printf '%s\\n'`)."""
    if not data:
        return b""
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines = lines[:-1]
    kept = [line for line in lines
            if b"run_pipeline.sh" not in line or re.match(rb"[ \t\n\v\f\r]*#", line)]
    return b"".join(line + b"\n" for line in kept)


def _filter(data: bytes) -> bytes:
    r = bash_source("filter_crontab", stdin=data)
    assert r.returncode == 0, r.stderr
    return r.stdout


@given(crontabs)
@example((["0 4 * * 5 /home/ec2-user/wayakit-price-prediction/run_pipeline.sh",
           "  # run_pipeline.sh disabled", "*/5 * * * * other-job"], False))
@example((["\r# run_pipeline.sh"], True))
def test_filter_crontab_matches_reference_and_is_idempotent(crontab):
    data = _crontab_bytes(crontab)
    once = _filter(data)
    assert once == _reference_filter(data)
    assert _filter(once) == once
    in_lines = [] if not data else (data[:-1] if data.endswith(b"\n") else data).split(b"\n")
    out_lines = once[:-1].split(b"\n") if once else []
    assert len(out_lines) <= len(in_lines), "never adds lines"
    # Every line without run_pipeline.sh survives, byte for byte and in order.
    assert [x for x in out_lines if b"run_pipeline.sh" not in x] == \
        [x for x in in_lines if b"run_pipeline.sh" not in x]
    # No active (non-comment) line with run_pipeline.sh is left.
    assert not [x for x in out_lines if b"run_pipeline.sh" in x and not re.match(rb"\s*#", x)]


# --- bootstrap.sh: env file round trip and validators -------------------------------------------
log_dirs = st.from_regex(r"/[A-Za-z0-9._/-]{1,40}", fullmatch=True)
topic_arns = st.one_of(st.just(""), st.builds(
    lambda region, account, name: f"arn:aws:sns:{region}:{account}:{name}",
    st.from_regex(r"[a-z0-9-]{1,15}", fullmatch=True), st.from_regex(r"[0-9]{12}", fullmatch=True),
    st.from_regex(r"[A-Za-z0-9_-]{1,30}", fullmatch=True)))
models = st.one_of(st.just(""), st.from_regex(r"[A-Za-z0-9._:/+-]{1,60}", fullmatch=True))
KEYS = ("PI_LOG_DIR", "PI_ALERTS_TOPIC_ARN", "PI_RELEVANCE_PRIMARY_MODEL", "PI_RELEVANCE_FALLBACK_MODEL")


@given(log_dirs, topic_arns, models, models)
@example("/home/ec2-user/logs", "arn:aws:sns:eu-north-1:000000000000:price-intelligence-alerts",
         "arn:aws:bedrock:eu-north-1:000000000000:application-inference-profile/abc", "deepseek.v3.2")
@example("/a", "", "", "")
def test_valid_values_round_trip_through_the_env_file(tmp_path_factory, log_dir, topic, primary,
                                                      fallback):
    f = tmp_path_factory.mktemp("env") / "price-intelligence.env"
    r = bash_source('valid_log_dir "$V1" && valid_topic_arn "$V2" && valid_model "$V3" '
                    '&& valid_model "$V4" || exit 3\n'
                    'render_env_file "$V1" "$V2" "$V3" "$V4" > "$F"\n'
                    + "".join(f'env_file_value "$F" {k}; printf "\\0"\n' for k in KEYS),
                    env={"F": str(f), "V1": log_dir, "V2": topic, "V3": primary, "V4": fallback})
    assert r.returncode == 0, ("validators rejected a valid value", r.returncode, r.stderr)
    assert [p.decode() for p in r.stdout.split(b"\0")[:4]] == [log_dir, topic, primary, fallback]
    # Empty values are not written at all (the code then uses its defaults).
    written = [line.split("=", 1)[0] for line in f.read_text().splitlines() if not line.startswith("#")]
    assert written == [k for k, v in zip(KEYS, (log_dir, topic, primary, fallback)) if v]


FORBIDDEN = [" ", "'", '"', "$", ";", "\\", "\n", "`", "\t"]


@given(st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=30),
       st.sampled_from(FORBIDDEN), st.integers(0, 30))
@example("arn:aws:bedrock:eu-north-1:000000000000:x", "\n", 41)
def test_validators_reject_shell_and_env_file_metacharacters(text, bad, position):
    value = text[:position] + bad + text[position:]
    for fn, v in (("valid_log_dir", "/" + value), ("valid_model", value),
                  ("valid_topic_arn", f"arn:aws:sns:eu-north-1:000000000000:{value}")):
        r = bash_source(f'{fn} "$V"', env={"V": v})
        assert r.returncode != 0, (fn, v)


@given(st.text(st.characters(min_codepoint=32, max_codepoint=126), max_size=40))
def test_valid_model_equals_its_python_regex(value):
    r = bash_source('valid_model "$V"', env={"V": value})
    assert (r.returncode == 0) is (value == "" or re.fullmatch(r"[A-Za-z0-9._:/+-]+", value) is not None)


# --- run_pipeline.sh: redact_secrets (failure alert) --------------------------------------------
RUN_PIPELINE = REPO_ROOT / "run_pipeline.sh"


def _redact_source():
    """REDACT_RE ... redact_secrets() exactly as written in run_pipeline.sh (the block also holds
    REDACT_REST_RE), plus the line cap."""
    text = RUN_PIPELINE.read_text(encoding="utf-8")
    cap = re.search(r"^ALERT_LINE_MAX_CHARS=(\d+)$", text, re.M)
    block = re.search(r"^REDACT_RE=.*?^redact_secrets\(\) \{\n.*?^\}\n", text, re.M | re.S)
    assert cap and block, "REDACT_RE / redact_secrets() / ALERT_LINE_MAX_CHARS not found"
    assert "REDACT_REST_RE=" in block.group(0), "REDACT_REST_RE must be in the extracted block"
    return int(cap.group(1)), block.group(0)


LINE_CAP, REDACT_BLOCK = _redact_source()
NO_CAP = 100_000  # longer than any generated line: the uncapped redaction

# Reference (pi-dev's contract, F3 follow-up B). re.ASCII is required: without it the Kelvin sign
# and the long s match 'k' and 's' case-insensitively, which bash in the C locale does not do.
# [:space:] in the C locale is exactly these six characters.
_F = re.IGNORECASE | re.ASCII
_WS = r"[ \t\n\v\f\r]"
_NAME = r"(key|token|secret|password|authorization)"
REFERENCE_REST_RE = re.compile(_NAME + rf"""(["'\\]*{_WS}*:|["'\\]+{_WS}*=|{_WS}+=|={_WS}){_WS}*""", _F)
REFERENCE_PLAIN_RE = re.compile(_NAME + r"""=("[^"]*("|\Z)|'[^']*('|\Z)|[^&\t\n\v\f\r "'])+""", _F)
SECRET_NAME_RE = re.compile(_NAME, _F)


def reference_redact(line: str, cap: int = LINE_CAP) -> str:
    m = REFERENCE_REST_RE.search(line)
    head, tail = (line[:m.start()], m.group(0) + "<redacted>") if m else (line, "")
    return (REFERENCE_PLAIN_RE.sub(lambda x: x.group(1) + "=<redacted>", head) + tail)[:cap]


def run_redact(lines, passes=1, cap=None):
    """Run the extracted function over `lines` (builtins only, C locale); returns the output lines."""
    pipe = " | ".join(["redact_secrets"] * passes)
    src = f"ALERT_LINE_MAX_CHARS={cap or LINE_CAP}\n{REDACT_BLOCK}"
    r = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", f"{src}\n{pipe}"],
                       input="".join(f"{line}\n" for line in lines).encode(),
                       env={"PATH": "", "LC_ALL": "C"}, capture_output=True, timeout=30)
    assert r.returncode == 0 and r.stderr == b"", r.stderr
    out = r.stdout.decode()
    assert out.endswith("\n") or not lines
    return out.split("\n")[:-1]


KEY_NAMES = st.sampled_from([
    "key", "KEY", "Key", "api_key", "API_KEY", "apiKey", "x-api-key", "monkey", "keytoken",
    "token", "TOKEN", "Token", "access_token", "refreshToken",
    "secret", "SECRET", "client_secret", "clientSecret",
    "password", "Password", "PASSWORD", "db_password",
    "authorization", "Authorization", "AUTHORIZATION", "proxy-authorization"])
# Real secrets (API keys, tokens, base64) never contain quotes or newlines; bare ones do not
# contain & or blanks either.
SECRET_BARE = st.text(st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                                      "0123456789+/=._~:-%@;,?\\"), min_size=1, max_size=40)
SECRET_QUOTED = st.text(st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                                        "0123456789+/=._~:-%@;,?\\ &\t"), min_size=1, max_size=40)
# Any ASCII except newline (one log line), NUL (not representable in bash) and DEL.
LINE_CHARS = st.characters(min_codepoint=1, max_codepoint=126, blacklist_characters="\n")
# Filler never spells a generated secret by itself (every secret starts with "FAKE").
FILLER = st.text(LINE_CHARS, max_size=60).filter(lambda s: "FAKE" not in s)
TERMINATORS = st.sampled_from(["&", " ", "\t", "\r", "&alt=json", " refused", "\" end", "' end", ""])
# Separators that may glue a secret to the text before it (a previous value included).
SEPARATORS = st.sampled_from([" ", "&", "?", "\t", ",", ";", "{", "(", "\\", ""])

PLAIN_FORMS = ("bare", "double", "single", "double-open", "single-open", "glued-after-quote",
               "glued-before-quote")
REST_FORMS = {
    "colon": "{n}: {v}", "colon-tight": "{n}:{v}", "json": '"{n}": "{v}"', "json-tight": '"{n}":"{v}"',
    "json-single": "'{n}': '{v}'", "json-escaped": '\\"{n}\\": \\"{v}\\"', "spaced-equals": "{n} = {v}",
    "equals-space": "{n}= {v}", "space-equals": "{n} ={v}", "bearer": "{n}: Bearer {v}",
    "yaml-quoted": '{n}: "{v}"',
}


def _fake(draw, alphabet):
    return "FAKE" + draw(alphabet)


@st.composite
def secret_lines(draw, forms=PLAIN_FORMS + tuple(REST_FORMS)):
    """A log line with 1-3 secrets in any supported form among arbitrary filler.
    Returns (line, secrets)."""
    parts, secrets = [draw(FILLER)], []
    for _ in range(draw(st.integers(1, 3))):
        form = draw(st.sampled_from(forms))
        name, sep = draw(KEY_NAMES), draw(SEPARATORS)
        if form in REST_FORMS:
            secret = _fake(draw, SECRET_QUOTED)
            parts.append(sep + REST_FORMS[form].format(n=name, v=secret) + draw(FILLER))
            secrets.append(secret)
            break  # everything after a rest-of-line form is redacted
        if form.startswith("glued"):
            a, b = _fake(draw, SECRET_BARE), _fake(draw, SECRET_BARE)
            q = draw(st.sampled_from(['"', "'"]))
            value = f"{q}{a}{q}{b}" if form == "glued-after-quote" else f"{a}{q}{b}{q}"
            secrets += [a, b]
        else:
            secret = _fake(draw, SECRET_BARE if form == "bare" else SECRET_QUOTED)
            value = {"bare": secret, "double": f'"{secret}"', "single": f"'{secret}'",
                     "double-open": f'"{secret}', "single-open": f"'{secret}"}[form]
            secrets.append(secret)
        parts.append(f"{sep}{name}={value}")
        if form.endswith("-open"):
            break  # an unterminated quote runs to the end of the line
        parts.append(draw(TERMINATORS) + draw(FILLER))
    return "".join(parts), secrets


# Lines built from the pieces the two regexes care about: stresses the regex edges far more than
# random text, where a secret name almost never appears.
REGEX_PIECES = st.sampled_from(["key", "KEY", "Token", "secret", "PassWord", "authorization", "api_",
                                "=", ":", " ", "\t", "\r", '"', "'", "\\", "&", "?", ",", "a", "FAKE",
                                "<redacted>", "Bearer "])
PIECE_LINES = st.lists(REGEX_PIECES, max_size=25).map("".join)
ANY_LINES = st.one_of(st.text(LINE_CHARS, max_size=700), PIECE_LINES,
                      secret_lines().map(lambda c: c[0]))


@given(st.lists(ANY_LINES, min_size=1, max_size=8))
@example(['GET /v1?key=AIzaFAKE&alt=json', 'KEY="FAKE QUOTED', "token='FAKE'", 'key=key=FAKE',
          'key=""', "x" * 498 + "key=FAKE", 'API_KEY=FAKE', 'Token=FAKE;expires=1', ''])
@example(['{"api_key": "FAKE0"}', "x-api-key: FAKE1", "Authorization: Bearer FAKE2",
          "password = FAKE3", 'key="a"b"c', "key='a'b'c", 'key=x?key="FAKE0"', ' key="FAKE0"0',
          'body={\\"client_secret\\": \\"FAKE4\\"}', "key=a token: b", "key: a token=b"])
@example(["\u212a" + "ey=FAKE", "\u017f" + "ecret=FAKE"])  # Kelvin sign / long s: not names in C
def test_redact_equals_the_reference(lines):
    assert run_redact(lines) == [reference_redact(line) for line in lines]


@given(st.lists(secret_lines(), min_size=1, max_size=5))
@example([('url?key="FAKE with spaces" next', ["FAKE with spaces"])])
@example([("auth token='FAKEunterminated and more", ["FAKEunterminated and more"])])
@example([('k=1 key=FAKE1&token="FAKE2" API_KEY=\'FAKE3\'', ["FAKE1", "FAKE2", "FAKE3"])])
@example([('key=x?key="FAKE0"', ["FAKE0"]), (' key="FAKE0"FAKE1', ["FAKE0", "FAKE1"]),
          ('key=FAKE0"FAKE1"', ["FAKE0", "FAKE1"])])
@example([('{"api_key": "FAKEJSON", "model": "x"}', ["FAKEJSON"]),
          ("Authorization: Bearer FAKEBEARER", ["FAKEBEARER"]),
          ("x-api-key: FAKEHEADER", ["FAKEHEADER"]), ("password = FAKEPWD", ["FAKEPWD"]),
          ("{'token': 'FAKESQ'}", ["FAKESQ"]), ('a\\"secret\\": \\"FAKEESC\\"', ["FAKEESC"])])
def test_no_secret_survives_redaction(cases):
    out = run_redact([line for line, _ in cases])
    for (line, secrets), redacted in zip(cases, out):
        for secret in secrets:
            assert secret not in redacted, (line, redacted)
        assert len(redacted) <= LINE_CAP


@given(st.sampled_from(sorted(REST_FORMS)), KEY_NAMES, SECRET_QUOTED, FILLER, FILLER)
def test_rest_of_line_forms_redact_everything_after_the_separator(form, name, value, before, after):
    """The name and its separator stay; everything after them, to the end of the line, goes."""
    assume(not SECRET_NAME_RE.search(before))  # an earlier name would win (more redaction)
    line = before + " " + REST_FORMS[form].format(n=name, v="FAKE" + value) + after
    [out] = run_redact([line], cap=NO_CAP)
    assert out.endswith("<redacted>") and out.count("<redacted>") == 1, (line, out)
    kept = out[: -len("<redacted>")]
    assert line.startswith(kept) and name in kept, (line, out)
    assert "FAKE" not in out


@given(st.lists(ANY_LINES, min_size=1, max_size=8))
@example(["x" * 495 + "key=FAKEFAKE"])     # the cap cuts "<redacted>": the second pass must agree
@example([' key="FAKE0"0', 'key=x?key="FAKE0"', 'key=FAKE"LEAK"', "x" * 490 + "token: FAKE"])
def test_redact_is_idempotent(lines):
    assert run_redact(lines, passes=2) == run_redact(lines)


@given(st.lists(st.text(LINE_CHARS, max_size=700).filter(lambda s: not SECRET_NAME_RE.search(s)),
                min_size=1, max_size=8))
@example(["x" * 600, "no names here: a=b c: d \"e\" 'f'", "monke=y tok=en", ""])
def test_lines_without_a_secret_name_are_unchanged_apart_from_the_cap(lines):
    assert run_redact(lines) == [line[:LINE_CAP] for line in lines]


@given(st.lists(ANY_LINES, min_size=1, max_size=8))
@example(["x" * 495 + "key=FAKEFAKE", "y" * 600, "z" * 498 + "token: FAKE"])
def test_cap_is_applied_after_redaction(lines):
    capped, uncapped = run_redact(lines), run_redact(lines, cap=NO_CAP)
    assert capped == [u[:LINE_CAP] for u in uncapped]
    assert all(len(c) <= LINE_CAP for c in capped)


@given(st.sampled_from(['"', "'"]), SECRET_BARE, SECRET_BARE, st.booleans())
@example('"', "FAKE0", "0", True)      # ' key="FAKE0"0' (was a strict xfail before the F3 follow-up)
@example('"', "x?key=", "FAKE0", False)  # ' key=x?key="FAKE0"': a whole quoted secret used to leak
def test_text_glued_to_a_quoted_value_is_part_of_the_value(quote, head, tail, quoted_first):
    """Shell-word rule: text glued to a quote belongs to the value, so none of it is left."""
    value = f"{quote}{head}{quote}{tail}" if quoted_first else f"{head}{quote}{tail}{quote}"
    line = f" key={value}"
    once = run_redact([line])
    assert once == [" key=<redacted>"] and run_redact([line], passes=2) == once
