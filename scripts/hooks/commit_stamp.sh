#!/usr/bin/env bash
# PreToolUse hook (Bash): early, friendly warning about the "Verified:" stamp of each `git commit`.
#
# The rule is enforced by the git hook scripts/githooks/commit-msg (installed with
# scripts/install_githooks.sh), which reads the FINAL message and compares it with the verify.sh record.
# This hook only brings the warning forward so the agent fixes it before attempting the commit.
#
# Strict detection: the command is split with shlex into segments (; && || | and newlines) and only
# a segment whose program is git (after VAR=x, env, command...) and whose subcommand, skipping the
# global options (-C dir, -c k=v, --git-dir=..., --no-pager), is "commit" counts. `echo "git commit"`,
# grep, comments or text containing those words do not count.
#
# What it checks on each git commit:
#   - Skipping the git hook: --no-verify, -n (also inside -anm...) or -c core.hooksPath=...
#     is ALWAYS blocked (exit 2), in every mode except off: it would bypass the real verification.
#   - The message (from -m/--message, including the heredoc $(cat <<'EOF' ...), from -F/--file or,
#     with --amend and no new message, the last commit's) must have exactly one trailer matching
#       ^Verified: (quick|full|none) \(.+\)$
#     read with "git interpret-trailers --parse".
#   - quick/full: if the command does not change the index (no prior git add, -a or paths), it looks in
#     .claude/state/verifications.jsonl for an OK verify.sh run for the current index tree.
#   - Warns if core.hooksPath does not point to scripts/githooks (the git hook is not installed).
#
# Mode (variable PI_COMMIT_GATE):
#   off      checks nothing.
#   observe  (default) does not block the stamp: warns (systemMessage + stderr) and writes to the log.
#   enforce  blocks (exit 2) if the stamp is missing, does not match the format or has no record. No escape hatch.
# Log: .claude/state/commit_gate.log (date, session, mode and result; never the command or message text).
#
# Manual test:
#   echo '{"session_id":"s1","tool_input":{"command":"git commit -m x"}}' \
#     | PI_COMMIT_GATE=enforce scripts/hooks/commit_stamp.sh; echo $?

set -u

MODE="${PI_COMMIT_GATE:-observe}"
[ "$MODE" = "off" ] && exit 0

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
STATE="$ROOT/.claude/state"
mkdir -p "$STATE"
INPUT="$(cat || true)"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { [ -x /usr/bin/python3 ] && PY=/usr/bin/python3; }
if [ -z "$PY" ]; then
  printf '%s mode=%s no_python3 (not checked; commit-msg checks it)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$MODE" >> "$STATE/commit_gate.log"
  exit 0
fi

printf '%s' "$INPUT" | PI_ROOT="$ROOT" PI_MODE="$MODE" "$PY" -c '
import json, os, re, shlex, subprocess, sys, time

root = os.environ["PI_ROOT"]
mode = os.environ["PI_MODE"]
state = os.path.join(root, ".claude", "state")
REGEX = re.compile(r"^Verified: (quick|full|none) \(.+\)$")

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
cmd = ti.get("command")
if not isinstance(cmd, str) or "commit" not in cmd:
    sys.exit(0)
session = re.sub(r"[^A-Za-z0-9_-]", "_", str(d.get("session_id") or "no_session"))[:60]
cwd = d.get("cwd") if isinstance(d.get("cwd"), str) and os.path.isdir(d.get("cwd")) else root

def note(res):
    try:
        with open(os.path.join(state, "commit_gate.log"), "a", encoding="utf-8") as f:
            f.write("%s session=%s mode=%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), session, mode, res))
    except OSError:
        pass

def git(*args, stdin=None, dir_=None):
    try:
        p = subprocess.run(["git", "-C", dir_ or cwd] + list(args), input=stdin, capture_output=True, text=True, timeout=10)
        return p.returncode, p.stdout
    except Exception:
        return 1, ""

# ---------- Split into segments ----------
try:
    lx = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()\n")
    lx.whitespace = " \t\r"
    lx.whitespace_split = True
    tokens = list(lx)
except ValueError:
    note("unparsable_command (commit-msg checks it)")
    sys.exit(0)
SEP = set(";&|()\n")
segments, current = [], []
for t in tokens:
    if t and set(t) <= SEP:
        if current: segments.append(current)
        current = []
    else:
        current.append(t)
if current: segments.append(current)

def parse_git(seg):
    """(dir, global_options, subcommand, args) if the segment is a git call; otherwise None."""
    i = 0
    while i < len(seg):
        t = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) or t in ("env", "command", "nohup", "time", "exec", "sudo"):
            i += 1
        else:
            break
    if i >= len(seg) or os.path.basename(seg[i]) != "git":
        return None
    i += 1
    dir_, global_opts = cwd, []
    while i < len(seg) and seg[i].startswith("-"):
        t = seg[i]
        if t in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(seg):
            if t == "-C":
                dir_ = seg[i + 1] if os.path.isabs(seg[i + 1]) else os.path.join(dir_, seg[i + 1])
            global_opts += [t, seg[i + 1]]
            i += 2
        else:
            global_opts.append(t)
            i += 1
    if i >= len(seg):
        return None
    return dir_, global_opts, seg[i], seg[i + 1:]

had_add = False
problems, warnings, blocks = [], [], []
found = False
WITH_ARG = set("mFcCt")  # short git commit options that take an argument

def heredoc_body(v):
    m = re.match(r"^\$\(\s*cat\s+<<-?\s*[\x27\"]?(\w+)[\x27\"]?\s*\n(.*)\n\s*\1\s*\n?\s*\)\s*$", v, re.S)
    return m.group(2) if m else v

for seg in segments:
    if seg[0] == "cd" and len(seg) > 1:
        target = os.path.expanduser(seg[1])
        target = target if os.path.isabs(target) else os.path.join(cwd, target)
        if os.path.isdir(target):
            cwd = target
        continue
    g = parse_git(seg)
    if g is None:
        continue
    dir_, global_opts, sub, args = g
    if sub in ("add", "rm", "mv", "stash", "reset", "restore", "checkout"):
        had_add = True
        continue
    if sub != "commit":
        continue
    found = True
    for k in range(0, len(global_opts) - 1):
        if global_opts[k] == "-c" and global_opts[k + 1].lower().startswith("core.hookspath"):
            blocks.append("git -c core.hooksPath=... bypasses the commit-msg hook")
    msgs, msg_file, amend, changes_index = [], None, False, had_add
    k = 0
    while k < len(args):
        a = args[k]
        if a == "--":
            changes_index = changes_index or k + 1 < len(args)
            break
        if a == "--no-verify":
            blocks.append("--no-verify skips the commit-msg hook")
        elif a in ("-m", "--message") and k + 1 < len(args):
            msgs.append(args[k + 1]); k += 1
        elif a.startswith("--message="):
            msgs.append(a.split("=", 1)[1])
        elif a in ("-F", "--file") and k + 1 < len(args):
            msg_file = args[k + 1]; k += 1
        elif a.startswith("--file="):
            msg_file = a.split("=", 1)[1]
        elif a == "--amend":
            amend = True
        elif a in ("--all", "--include", "--only", "--interactive", "--patch"):
            changes_index = True
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            # group of short options: -anm "x", -mx, -Ffile
            for j, c in enumerate(a[1:], start=1):
                if c == "n":
                    blocks.append("-n (--no-verify) skips the commit-msg hook")
                if c in ("a", "i", "o", "p"):
                    changes_index = True
                if c in ("u", "S"):
                    break  # -uno, -S<key>: the rest is its value, no more options
                if c in WITH_ARG:
                    rest = a[j + 1:]
                    value = rest if rest else (args[k + 1] if k + 1 < len(args) else "")
                    if not rest: k += 1
                    if c == "m": msgs.append(value)
                    elif c == "F": msg_file = value
                    break
        elif not a.startswith("-"):
            changes_index = True  # paths: git commit <file>
        k += 1

    # Message to check
    message = None
    if msgs:
        message = "\n\n".join(heredoc_body(m) for m in msgs)
    elif msg_file and msg_file != "-":
        path = msg_file if os.path.isabs(msg_file) else os.path.join(dir_, msg_file)
        try:
            message = open(path, encoding="utf-8").read()
        except OSError:
            warnings.append("could not read the -F file; commit-msg will check it")
    elif amend:
        rc, out = git("log", "-1", "--format=%B", dir_=dir_)
        message = out if rc == 0 else None
    else:
        warnings.append("the message is written in the editor; commit-msg will check it")

    if message is not None:
        rc, clean = git("stripspace", "--strip-comments", stdin=message, dir_=dir_)
        rc, trailers = git("interpret-trailers", "--parse", stdin=clean if rc == 0 else message, dir_=dir_)
        stamps = [l for l in trailers.splitlines() if l.lower().startswith("verified:")]
        if not stamps:
            if "verified:" in message.lower():
                problems.append("the \"Verified:\" is not a trailer: it goes on its own line, in the last paragraph, after a blank line")
            else:
                problems.append("the line \"Verified: <quick|full|none> (<detail>)\" is missing")
        elif len(stamps) > 1:
            problems.append("there are %d \"Verified:\" trailers; there must be one" % len(stamps))
        elif not REGEX.match(stamps[0]):
            problems.append("the stamp does not match ^Verified: (quick|full|none) \\(.+\\)$")
        else:
            m = stamps[0].split()[1]
            if m in ("quick", "full"):
                if changes_index:
                    warnings.append("the command changes the index (git add, -a or paths): the verify.sh record will not match; run git add, run verify and then commit")
                else:
                    rc, tree = git("write-tree", dir_=dir_)
                    tree = tree.strip()
                    ok = False
                    try:
                        with open(os.path.join(state, "verifications.jsonl"), encoding="utf-8") as f:
                            recs = [json.loads(l) for l in f if l.strip()]
                    except (OSError, ValueError):
                        recs = []
                    for rec_mode in (["full"] if m == "full" else ["quick", "full"]):
                        c = [r for r in recs if r.get("tree") == tree and r.get("mode") == rec_mode]
                        if c and c[-1].get("result") == "OK" and c[-1].get("index_matches_tree") is True:
                            ok = True
                    if not ok:
                        problems.append("the stamp says \"%s\" but there is no OK run of scripts/verify.sh %s for the current index (run verify after git add)" % (m, m))

if not found:
    sys.exit(0)

rc, hp = git("config", "--get", "core.hooksPath")
if hp.strip() != "scripts/githooks":
    warnings.append("the git commit-msg hook is not installed (core.hooksPath); the lead must run scripts/install_githooks.sh")

if blocks:
    note("blocked_hook_skip")
    sys.stderr.write("Commit blocked: " + "; ".join(sorted(set(blocks))) + ".\n"
                     "In this repo every commit goes through scripts/githooks/commit-msg. It is not skipped: if the stamp cannot\n"
                     "be met, use \"Verified: none (reason)\".\n")
    sys.exit(2)

if problems and mode == "enforce":
    note("blocked_stamp: problems=%d" % len(problems))
    sys.stderr.write("Commit blocked (PI_COMMIT_GATE=enforce):\n" + "".join("  - %s\n" % p for p in problems)
                     + "Format: Verified: <quick|full|none> (<detail or reason>), REVIEW.md rule 10.\n"
                     "If it was not verified, write \"Verified: none (reason)\"; do not invent a verification.\n")
    sys.exit(2)

if problems or warnings:
    note("warning: problems=%d warnings=%d" % (len(problems), len(warnings)))
    text = "Verified stamp warning (the commit-msg hook may reject this commit):\n" + \
           "".join("  - %s\n" % p for p in problems + warnings)
    sys.stderr.write(text)
    print(json.dumps({"systemMessage": text.strip()}, ensure_ascii=False))
else:
    note("stamp_ok")
sys.exit(0)
'
