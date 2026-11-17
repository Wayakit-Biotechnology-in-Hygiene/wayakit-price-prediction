#!/usr/bin/env bash
# TaskCompleted hook (Agent Teams): a task only closes if it passes the quick verification.
#
# What it does:
#   1. Reads the JSON from stdin without assuming its shape (the TaskCompleted schema is not
#      documented). The first time it stores only the key NAMES, never the
#      values, in .claude/state/hook_inputs.log to learn the schema.
#   1b. Checks / code separation: compares the working tree (including untracked files)
#      against merge-base(PI_CHECKS_BASE, HEAD). If that same diff changes
#      verification files (scripts/hooks/protected_paths.txt) AND production code
#      (scraper/, ml_model/, run_pipeline.sh), it fails with exit 2: a change cannot modify
#      the checks that judge it. They go in separate changes. If the base version does not exist, it fails.
#   2. Runs `scripts/verify.sh quick`. If the script does not exist, it fails (does not let it through).
#   3. If it fails, adds 1 to the counter .claude/state/failures_<task>.count (the task comes from the id
#      or the subject if they are in the JSON; otherwise 'global') and exits with 2: the task stays
#      open and stderr reaches the agent as feedback.
#      From the third failed round on it asks the agent to stop and escalate to Mafer, and logs it
#      in .claude/state/escalations.log.
#   4. If it passes, deletes the counter and exits with 0.
#   The failure message includes the failed tests (pytest FAILED/ERROR lines), the verify.sh
#   summary and `git status --short`: the tree is shared and the failure may belong to another teammate.
#
# Variables:
#   PI_CHECKS_BASE         version against which "the change" is measured in step 1b. Defaults to
#                          PI_BASE_REF or origin/server-improvement. While the checks setup is not
#                          in the base, the lead can point it to a commit that already has it.
#   PI_GATE_FORCE_FAIL=1   simulates a failure without running the tests (to test the hook).
#   CLAUDE_PROJECT_DIR     repo root (set by Claude Code); if missing, git is used.
#
# Manual test:
#   echo '{"task_id":"7","task_subject":"x"}' | PI_GATE_FORCE_FAIL=1 scripts/hooks/gate_task_completed.sh; echo $?

set -u

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
STATE="$ROOT/.claude/state"
mkdir -p "$STATE"
LIMIT=3

INPUT="$(cat || true)"

# Reads the JSON with jq if it exists; otherwise with python3. Never fails: returns empty.
json_eval() {
  local jq_filter="$1" py_code="$2"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$INPUT" | jq -r "$jq_filter" 2>/dev/null || true
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s' "$INPUT" | python3 -c "$py_code" 2>/dev/null || true
  fi
}

# 1. Schema: key names only, once.
if ! grep -q 'TaskCompleted' "$STATE/hook_inputs.log" 2>/dev/null; then
  KEYS="$(json_eval \
    '[paths(scalars) | map(tostring) | join(".")] | unique | join(", ")' \
    'import json,sys
d=json.load(sys.stdin)
def p(o,pre=""):
    if isinstance(o,dict):
        for k,v in o.items(): yield from p(v,pre+str(k)+".")
    elif isinstance(o,list):
        for i,v in enumerate(o): yield from p(v,pre+str(i)+".")
    else: yield pre[:-1]
print(", ".join(sorted(set(p(d)))))')"
  printf '%s TaskCompleted keys: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${KEYS:-<could not read the JSON>}" \
    >> "$STATE/hook_inputs.log"
fi

# Task key: id or subject if present; otherwise 'global'. Only [A-Za-z0-9_-], max. 60.
TASK="$(json_eval \
  '[.task_id, .taskId, (.task|objects|.id), .task_subject, (.task|objects|.subject), .subject]
   | map(select(. != null and . != "")) | (.[0] // "") | tostring' \
  'import json,sys
d=json.load(sys.stdin); t=d.get("task") if isinstance(d.get("task"),dict) else {}
for v in (d.get("task_id"),d.get("taskId"),t.get("id"),d.get("task_subject"),t.get("subject"),d.get("subject")):
    if v not in (None,""): print(v); break')"
TASK="$(printf '%s' "$TASK" | tr -c 'A-Za-z0-9_-' '_' | cut -c1-60)"
[ -n "$TASK" ] || TASK="global"
COUNTER="$STATE/failures_${TASK}.count"

# Counts a failed round; on the third one asks to escalate. Does not exit.
count_round() {
  N=$(( $(cat "$COUNTER" 2>/dev/null || echo 0) + 1 ))
  echo "$N" > "$COUNTER"
}
escalate_if_due() {
  if [ "$N" -ge "$LIMIT" ]; then
    echo "Third failed round: stop and escalate to Mafer with what you tried." >&2
    printf '%s task=%s rounds=%s reason=%s\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" "$TASK" "$N" "$1" >> "$STATE/escalations.log"
  fi
}
tree_status() {
  echo "State of the shared tree (git status --short):"
  git -C "$ROOT" status --short 2>/dev/null | head -n 40
  echo "Note: the tree is shared. If the failure is in files that are not part of your task, it may be"
  echo "another teammate's work in progress: do not touch it; report it and close the task at a sync"
  echo "point (when pi-dev announces \"stable tree\")."
}

# 1b. Checks and production code do not go in the same change.
CHECKS_BASE="${PI_CHECKS_BASE:-${PI_BASE_REF:-origin/server-improvement}}"
if ! BASE_SHA="$(git -C "$ROOT" rev-parse --verify --quiet "${CHECKS_BASE}^{commit}")"; then
  count_round
  {
    echo "Base version '$CHECKS_BASE' does not exist, so the separation between checks and code cannot be checked."
    echo "Run 'git fetch origin' or ask the lead to set PI_CHECKS_BASE. The task stays open (round $N)."
  } >&2
  escalate_if_due "no_base"
  exit 2
fi
MB="$(git -C "$ROOT" merge-base "$BASE_SHA" HEAD 2>/dev/null || echo "$BASE_SHA")"
CHANGED="$( { git -C "$ROOT" diff --name-only "$MB" -- 2>/dev/null; git -C "$ROOT" ls-files --others --exclude-standard 2>/dev/null; } | sort -u)"
PROT_LIST="$ROOT/scripts/hooks/protected_paths.txt"
PROT_PATTERNS="$( { grep -Ev '^[[:space:]]*(#|$)' "$PROT_LIST" 2>/dev/null; echo 'scripts/hooks/protected_paths.txt'; } | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')"
[ -f "$PROT_LIST" ] || PROT_PATTERNS="$(printf '%s\n' REVIEW.md pytest.ini docs/reliability-checklist.md \
  .claude/settings.json '.claude/agents/**' '.claude/skills/**' 'scripts/**' \
  tests/conftest.py tests/e2e/_helpers.py tests/e2e/_pipeline_sandbox.py tests/differential/_harness.py)"
CHECKS_TOUCHED=""
PROD_TOUCHED=""
while IFS= read -r f; do
  [ -n "$f" ] || continue
  case "$f" in scraper/*|ml_model/*|run_pipeline.sh) PROD_TOUCHED="${PROD_TOUCHED}  $f\n"; continue ;; esac
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    case "$p" in
      */\*\*) [ "${f#"${p%/\*\*}"/}" != "$f" ] && { CHECKS_TOUCHED="${CHECKS_TOUCHED}  $f\n"; break; } ;;
      *) [ "$f" = "$p" ] && { CHECKS_TOUCHED="${CHECKS_TOUCHED}  $f\n"; break; } ;;
    esac
  done <<< "$PROT_PATTERNS"
done <<< "$CHANGED"
if [ -n "$CHECKS_TOUCHED" ] && [ -n "$PROD_TOUCHED" ]; then
  count_round
  {
    echo "Gate: this change touches both verification files and production code (base: $CHECKS_BASE @ ${MB:0:7})."
    echo "A change cannot modify the checks that judge it. Split them: first the checks, in a"
    echo "separate change made by the lead with PI_EDIT_CHECKS=1; then the code. The task stays open (round $N)."
    echo "Modified checks:"; printf '%b' "$CHECKS_TOUCHED" | head -n 20
    echo "Modified production code:"; printf '%b' "$PROD_TOUCHED" | head -n 20
    tree_status
  } >&2
  escalate_if_due "checks_and_code"
  exit 2
fi

# 2. Quick verification.
OUTPUT="$STATE/last_verification_${TASK}.txt"
if [ "${PI_GATE_FORCE_FAIL:-0}" = "1" ]; then
  echo "Simulated failure (PI_GATE_FORCE_FAIL=1)." > "$OUTPUT"
  RC=1
elif [ ! -f "$ROOT/scripts/verify.sh" ]; then
  # It used to let it through; now deleting verify.sh cannot open the gate.
  echo "scripts/verify.sh does not exist: it cannot be verified and the task does not close." > "$OUTPUT"
  RC=2
else
  (cd "$ROOT" && bash scripts/verify.sh quick) > "$OUTPUT" 2>&1
  RC=$?
fi

# 4. Success: the counter is reset.
if [ "$RC" -eq 0 ]; then
  rm -f "$COUNTER"
  exit 0
fi

# 3. Failure: the round is counted.
count_round

{
  echo "The quick verification (scripts/verify.sh quick) failed: round $N (limit: $LIMIT) for task '$TASK'."
  echo "The task stays open."
  FAILED_TESTS="$(grep -E '^(FAILED|ERROR) ' "$OUTPUT" 2>/dev/null | head -n 20)"
  if [ -n "$FAILED_TESTS" ]; then
    echo "Failed tests:"
    printf '%s\n' "$FAILED_TESTS" | sed 's/^/  /'
  fi
  echo "verify.sh summary:"
  sed -n '/===== Summary/,$p' "$OUTPUT" | head -n 15
  [ "$RC" -eq 2 ] && { echo "Environment or usage error (exit 2):"; tail -n 5 "$OUTPUT"; }
  tree_status
  echo "Full output: .claude/state/$(basename "$OUTPUT")"
  echo "It is not fixed with sleeps, retries or skips: find the root cause."
} >&2

escalate_if_due "verify_quick output=.claude/state/$(basename "$OUTPUT")"
exit 2
