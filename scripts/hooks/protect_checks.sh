#!/usr/bin/env bash
# PreToolUse hook (Edit|Write|MultiEdit|NotebookEdit and Bash): a change cannot modify its own checks.
#
# What it does:
#   - Edit, Write, MultiEdit and NotebookEdit: if the path (resolved, without "..", without symlinks and
#     case-insensitive, like the macOS disk) falls under scripts/hooks/protected_paths.txt,
#     it blocks the tool (exit 2) and explains why on stderr.
#   - Bash (heuristic, not a complete lock): blocks commands that clearly write to a protected
#     path: redirection (> or >>) to it, tee, sed -i / perl -i, rm, mv, truncate, touch, chmod,
#     cp/ln/install/rsync with a protected destination, git checkout/restore/rm/mv on it
#     and "git config core.hooksPath". It does not see what a script or a "python -c" does inside: that is
#     what the TaskCompleted gate is for, which fails if the same change touches checks and production code.
#
# Exception: PI_EDIT_CHECKS=1 in the Claude Code environment lets everything through and logs it.
# Only the lead sets it (when launching the session), in a change dedicated to the checks, with no production code.
# A teammate cannot turn it on: the hook reads the environment of the Claude Code process, not that of its Bash.
#
# If scripts/hooks/protected_paths.txt does not exist, it uses the list included here (deleting it does not turn off the lock).
# If there is no python3, it blocks Edit/Write (it cannot check the path) and lets Bash through.
# Log: .claude/state/protect_checks.log (date, tool, decision and path; never the content).
#
# Manual test:
#   echo '{"tool_name":"Edit","tool_input":{"file_path":"REVIEW.md"}}' | scripts/hooks/protect_checks.sh; echo $?   # 2
#   echo '{"tool_name":"Edit","tool_input":{"file_path":"ml_model/x.py"}}' | scripts/hooks/protect_checks.sh; echo $? # 0

set -u

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
INPUT="$(cat || true)"
[ "${PI_EDITAR_CHECKS:-0}" = "1" ] && PI_EDIT_CHECKS=1  # TODO(remove-after-translation): legacy alias
PI_EDIT_CHECKS="${PI_EDIT_CHECKS:-0}"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { [ -x /usr/bin/python3 ] && PY=/usr/bin/python3; }

if [ -z "$PY" ]; then
  case "$INPUT" in
    *'"Bash"'*) exit 0 ;;
  esac
  [ "$PI_EDIT_CHECKS" = "1" ] && exit 0
  echo "Blocked: there is no python3 to check whether the path is a protected verification file." >&2
  exit 2
fi

printf '%s' "$INPUT" | PI_ROOT="$ROOT" PI_EDIT_CHECKS="$PI_EDIT_CHECKS" "$PY" -c '
import fnmatch, glob, json, os, shlex, sys, time

root = os.path.realpath(os.environ["PI_ROOT"])
allow = os.environ.get("PI_EDIT_CHECKS", "0") == "1"
list_file = os.path.join(root, "scripts", "hooks", "protected_paths.txt")
FALLBACK = """REVIEW.md
pytest.ini
docs/reliability-checklist.md
.claude/settings.json
.claude/settings.local.json
.claude/agents/**
.claude/skills/**
.claude/state/**
scripts/**
tests/conftest.py
tests/e2e/_helpers.py
tests/e2e/_pipeline_sandbox.py
tests/differential/_harness.py"""

try:
    with open(list_file, encoding="utf-8") as f:
        text = f.read()
except OSError:
    text = FALLBACK
patterns = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
# The list itself is also protected, even if someone removes it from the file.
patterns.append("scripts/hooks/protected_paths.txt")

def log(tool, decision, path):
    try:
        state_dir = os.path.join(root, ".claude", "state")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "protect_checks.log"), "a", encoding="utf-8") as f:
            f.write("%s tool=%s decision=%s path=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), tool, decision, path))
    except OSError:
        pass

def relative(path, cwd):
    """Path relative to the repo root, resolved; None if it falls outside the repo."""
    if not path:
        return None
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    real = os.path.realpath(path)
    rel = os.path.relpath(real, root)
    if rel == ".." or rel.startswith(".." + os.sep):
        return None
    return rel.replace(os.sep, "/")

def is_protected(rel):
    if rel is None:
        return False
    r = rel.casefold()
    for p in patterns:
        q = p.casefold()
        if q.endswith("/**"):
            base = q[:-3]
            if r == base or r.startswith(base + "/"):
                return True
        elif r == q:
            return True
    return False

def block(tool, rel, detail=""):
    log(tool, "blocked", rel)
    sys.stderr.write(
        "Blocked: \"%s\" is a protected verification file (see scripts/hooks/protected_paths.txt).%s\n"
        "A change cannot modify the tests, rules or hooks that judge it.\n"
        "If another check is really needed, do not work around it: tell the lead. She does it in a separate change,\n"
        "with no production code (scraper/, ml_model/, run_pipeline.sh), with PI_EDIT_CHECKS=1.\n"
        % (rel, (" " + detail) if detail else ""))
    sys.exit(2)

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
tool = d.get("tool_name") or ""
ti = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
cwd = d.get("cwd") if isinstance(d.get("cwd"), str) and os.path.isdir(d.get("cwd")) else root

# ---------- Edit / Write / MultiEdit / NotebookEdit ----------
if tool != "Bash":
    paths = [ti.get(k) for k in ("file_path", "notebook_path", "path") if isinstance(ti.get(k), str)]
    for path in paths:
        rel = relative(path, cwd)
        if is_protected(rel):
            if allow:
                log(tool, "allowed_PI_EDIT_CHECKS", rel)
                continue
            block(tool, rel)
    sys.exit(0)

# ---------- Bash (heuristic) ----------
cmd = ti.get("command")
if not isinstance(cmd, str) or not cmd.strip() or allow:
    sys.exit(0)

try:
    lx = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>\n")
    lx.whitespace = " \t\r"
    lx.whitespace_split = True
    tokens = list(lx)
except ValueError:
    sys.exit(0)  # unclosed quotes: do not guess

SEP = set(";&|()\n")
segments, current = [], []
for t in tokens:
    if t and set(t) <= SEP:
        if current:
            segments.append(current)
        current = []
    else:
        current.append(t)
if current:
    segments.append(current)

def protected_rels(tok):
    """Protected paths a token points to (expands wildcards)."""
    if any(c in tok for c in "*?["):
        base = tok if os.path.isabs(tok) else os.path.join(cwd, tok)
        matches = glob.glob(base, recursive=True) or [base.split("*")[0].split("?")[0].split("[")[0]]
        out = [relative(h, cwd) for h in matches]
    else:
        out = [relative(tok, cwd)]
    return [r for r in out if is_protected(r)]

def check(tool_detail, toks):
    for t in toks:
        for r in protected_rels(t):
            block("Bash", r, "(" + tool_detail + ")")

for seg in segments:
    # Redirections: the token after > or >> is the destination.
    for i, t in enumerate(seg):
        if t in (">", ">>", ">|", "&>", "&>>", "<>") and i + 1 < len(seg):
            check("redirection " + t, [seg[i + 1]])
    # Effective command: VAR=x assignments and common wrappers are skipped.
    i = 0
    while i < len(seg):
        t = seg[i]
        if "=" in t and not t.startswith("-") and t.split("=", 1)[0].replace("_", "").isalnum():
            i += 1
        elif t in ("sudo", "command", "env", "nohup", "time", "exec"):
            i += 1
        else:
            break
    if i >= len(seg):
        continue
    prog = os.path.basename(seg[i])
    args = [a for a in seg[i + 1:] if a not in (">", ">>", "<", ">|", "&>", "&>>", "<>")]
    non_opts = [a for a in args if not a.startswith("-")]
    if prog in ("rm", "unlink", "truncate", "touch", "chmod", "chown", "mv", "tee", "shred"):
        check(prog, non_opts)
    elif prog in ("cp", "ln", "install", "rsync", "ditto") and non_opts:
        check(prog + " (destination)", non_opts[-1:])
    elif prog in ("sed", "gsed", "perl") and any(
            a == "--in-place" or a.startswith("--in-place=") or (a.startswith("-") and not a.startswith("--") and "i" in a)
            for a in args):
        check(prog + " in place", non_opts)
    elif prog == "git":
        j = 0
        while j < len(args) and args[j].startswith("-"):
            j += 2 if args[j] in ("-C", "-c") else 1
        sub = args[j] if j < len(args) else ""
        rest = args[j + 1:]
        keys = [k for k, a in enumerate(rest) if a.casefold() == "core.hookspath"]
        writes = bool(keys) and (keys[0] + 1 < len(rest)
                                 or any(a.startswith("--unset") or a in ("--add", "--replace-all") for a in rest))
        if sub == "config" and writes and "--get" not in rest:
            log("Bash", "blocked", "git config core.hooksPath")
            sys.stderr.write("Blocked: core.hooksPath is set only by scripts/install_githooks.sh (run by the lead).\n")
            sys.exit(2)
        if sub in ("checkout", "restore", "rm", "mv"):
            check("git " + sub, [a for a in rest if not a.startswith("-")])
sys.exit(0)
'
