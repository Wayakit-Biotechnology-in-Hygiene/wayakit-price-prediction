#!/usr/bin/env bash
# Price Intelligence (PI) verification. No network: does not call AWS, Odoo, Apify or Bedrock.
#
# Usage (always AFTER "git add": the record applies to what is in the index):
#   scripts/verify.sh            # same as "quick"
#   scripts/verify.sh quick      # unit and property tests (Hypothesis)
#   scripts/verify.sh full       # quick + E2E + differential (base vs. new version)
#
# Variables:
#   PI_PYTHON    interpreter to use. If unset, the conda env "wayakit-price-prediction" is searched for;
#                if not found, exits with 2 (never falls back to another python silently).
#   PI_BASE_REF  base version for the differential tests. Defaults to origin/server-improvement
#                (what runs in production).
#
# Exit codes: 0 OK; 1 a test failed, none were collected or, in "full", an E2E or differential test
# was skipped; 2 usage or environment error (no interpreter, no base version).
#
# "full" requires the E2E and differential tests to run: if data/f1_before_2026-11-15/ is missing or pytest
# reports "skipped" in that block, the result is FAIL. In that case the honest stamp is
#   Verified: quick (e2e and differential did not run: <reason>)
#
# Record for the commit-msg hook (scripts/githooks/commit-msg): at the end it writes
# .claude/state/last_verification.json with {mode, result, tree, date, head, index_matches_tree, summary}
# and appends the same line to .claude/state/verifications.jsonl (history).
#   tree                = "git write-tree" of the index when verify ran (what is about to be committed).
#   index_matches_tree  = false if there were changes without "git add" or untracked files: then what was
#                         tested (the working tree) is not what gets committed and the hook does not accept the stamp.
# The hook only accepts "Verified: quick|full (...)" if there is an OK record with the same tree.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO_ROOT="$(pwd)"

MODE="${1:-quick}"
case "$MODE" in
  quick|full) ;;
  *) echo "Usage: scripts/verify.sh [quick|full]" >&2; exit 2 ;;
esac

# Python: PI_PYTHON if set; otherwise the conda env "wayakit-price-prediction" in known paths
# (hooks run without an interactive shell, so conda may not be on the PATH). Never another one.
ENV_NAME="wayakit-price-prediction"
PY="${PI_PYTHON:-}"
if [[ -n "$PY" ]]; then
  if ! command -v "$PY" >/dev/null 2>&1; then
    echo "ERROR: PI_PYTHON='$PY' does not exist or is not executable." >&2
    exit 2
  fi
else
  for base in "${CONDA_PREFIX:+${CONDA_PREFIX%/envs/*}}" "${CONDA_EXE:+$(dirname "$(dirname "$CONDA_EXE")")}" \
              "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" "$HOME/mambaforge" \
              "/opt/miniconda3" "/opt/anaconda3" "/opt/homebrew/Caskroom/miniconda/base"; do
    if [[ -n "$base" && -x "$base/envs/$ENV_NAME/bin/python" ]]; then
      PY="$base/envs/$ENV_NAME/bin/python"; break
    fi
  done
  if [[ -z "$PY" ]]; then
    {
      echo "ERROR: conda env '$ENV_NAME' not found (searched miniconda3, anaconda3, miniforge3, mambaforge and /opt)."
      echo "Create it with 'conda env create -f environment.yml' or set PI_PYTHON=/path/to/python."
      echo "No other python is used: the differential comparison can change with other versions."
    } >&2
    exit 2
  fi
fi

export PI_BASE_REF="${PI_BASE_REF:-origin/server-improvement}"
export PYTHONDONTWRITEBYTECODE=1

# Lock: tests must not reach AWS even if someone calls boto3 by mistake.
unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true

TMP_OUT="$(mktemp -d)"
trap 'rm -rf "$TMP_OUT"' EXIT

STATE="$REPO_ROOT/.claude/state"
mkdir -p "$STATE"

# Index tree at start (what would be committed). If it changes during the run, OK is not recorded.
TREE_START="$(git write-tree 2>/dev/null || echo '')"
HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo '')"
if git diff --quiet 2>/dev/null && [[ -z "$(git ls-files --others --exclude-standard 2>/dev/null | head -n 1)" ]]; then
  INDEX_MATCHES_TREE=true
else
  INDEX_MATCHES_TREE=false
fi

SUMMARY=()
FAILED=0
SKIP_REASON=""

run_block() {
  # $1 = block name, $2 = pytest marker expression, $3 = "no_skips" to require 0 skipped
  local name="$1" markers="$2" require="${3:-}" output="$TMP_OUT/$1.txt" rc=0
  echo "== $name: pytest -m \"$markers\" =="
  # -rfEs: the short summary lists FAILED, ERROR and SKIPPED with their names (the gate reads it).
  "$PY" -m pytest -m "$markers" -q -rfEs 2>&1 | tee "$output" || rc=$?
  cp "$output" "$STATE/last_verification_${name}.txt" 2>/dev/null || true
  local line
  line="$(grep -E '(passed|failed|error|skipped|no tests ran)' "$output" | tail -n 1 | sed -E 's/=+//g; s/^ +| +$//g' || true)"
  if [[ "$rc" -eq 5 ]]; then
    SUMMARY+=("$name: FAIL (no tests were collected)")
    FAILED=1
  elif [[ "$rc" -ne 0 ]]; then
    SUMMARY+=("$name: FAIL (${line:-exit code $rc})")
    FAILED=1
  elif [[ "$require" == "no_skips" ]] && grep -Eq '[0-9]+ skipped' <<<"$line"; then
    SUMMARY+=("$name: FAIL (tests were skipped: $line)")
    SKIP_REASON="${SKIP_REASON:-pytest reported skipped tests in $name}"
    FAILED=1
  else
    SUMMARY+=("$name: OK (${line:-no summary})")
  fi
}

record() {
  # $1 = result (OK | FAIL), $2 = mode to record (defaults to $MODE), $3.. = summary lines
  # (defaults to all of SUMMARY). Writes the JSON atomically; never stores test output.
  local result="$1" rec_mode="${2:-$MODE}" tree_end tmp
  shift; [[ $# -gt 0 ]] && shift
  local lines
  if [[ $# -gt 0 ]]; then lines="$(printf '%s\n' "$@")"; else lines="$(printf '%s\n' "${SUMMARY[@]}")"; fi
  tree_end="$(git write-tree 2>/dev/null || echo '')"
  if [[ "$result" == "OK" && "$tree_end" != "$TREE_START" ]]; then
    echo "WARNING: the index changed during verification (another 'git add'); recorded as FAIL." >&2
    result="FAIL"
    lines="${lines}"$'\n'"index: changed during the run"
  fi
  tmp="$(mktemp "$STATE/.last_verification.XXXXXX")"
  PI_R_MODE="$rec_mode" PI_R_RES="$result" PI_R_TREE="$TREE_START" PI_R_HEAD="$HEAD_SHA" \
  PI_R_CLEAN="$INDEX_MATCHES_TREE" PI_R_SUMMARY="$lines" \
    "$PY" -c '
import json, os, sys, time
e = os.environ
json.dump({
    "mode": e["PI_R_MODE"],
    "result": e["PI_R_RES"],
    "tree": e["PI_R_TREE"],
    "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "head": e["PI_R_HEAD"],
    "index_matches_tree": e["PI_R_CLEAN"] == "true",
    "summary": [l for l in e["PI_R_SUMMARY"].splitlines() if l],
}, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
' "$tmp"
  mv -f "$tmp" "$STATE/last_verification.json"
  # History (one line per run): the gate runs "quick" often and must not erase the record
  # of a "full" run for the same index.
  "$PY" -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), ensure_ascii=False))' \
    "$STATE/last_verification.json" >> "$STATE/verifications.jsonl"
  # Rotation: the last 500 runs are kept so the history does not grow without limit.
  if [ "$(wc -l < "$STATE/verifications.jsonl")" -gt 500 ]; then
    tail -n 500 "$STATE/verifications.jsonl" > "$STATE/verifications.jsonl.tmp" \
      && mv -f "$STATE/verifications.jsonl.tmp" "$STATE/verifications.jsonl"
  fi
}

echo "PI verify — mode: $MODE"
echo "Python: $PY ($("$PY" --version 2>&1))"
echo "Repo: $REPO_ROOT ($(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?') @ $(git rev-parse --short HEAD 2>/dev/null || echo '?'))"
echo "Index: tree ${TREE_START:0:12}"
if [[ "$INDEX_MATCHES_TREE" != "true" ]]; then
  echo "WARNING: there are changes without 'git add' or untracked files. The tests run on the working"
  echo "         tree, but the record is for the index: the commit-msg hook will not accept this result as"
  echo "         a quick/full stamp. 'git add' everything that goes in the commit and run again."
fi

if [[ "$MODE" == "full" ]]; then
  if ! BASE_SHA="$(git rev-parse --verify --quiet "${PI_BASE_REF}^{commit}")"; then
    echo "ERROR: base version '$PI_BASE_REF' does not exist. Run 'git fetch origin' or set PI_BASE_REF." >&2
    exit 2
  fi
  echo "Base: $PI_BASE_REF @ ${BASE_SHA:0:7}"
  if [[ ! -d data/f1_before_2026-11-15 ]]; then
    SKIP_REASON="data/f1_before_2026-11-15/ is missing"
    echo "WARNING: $SKIP_REASON; the E2E and differential tests cannot run and full mode will end in FAIL."
  fi
fi
echo

run_block quick "unit or property"
if [[ "$MODE" == "full" ]]; then
  echo
  # "and not unit": the differential harness self-tests (unit + differential) already ran in quick.
  run_block e2e-differential "(e2e or differential) and not unit" no_skips
  if [[ -n "$SKIP_REASON" && "$FAILED" -eq 0 ]]; then
    SUMMARY+=("e2e-differential: FAIL ($SKIP_REASON)")
    FAILED=1
  fi
fi

echo
echo "===== Summary ($MODE) ====="
for l in "${SUMMARY[@]}"; do echo "  $l"; done
if [[ "$FAILED" -ne 0 ]]; then
  # In "full", if the quick block passed, that result is also true: it is recorded separately
  # as "quick" (before the full one, which stays as the last verification).
  QUICK_OK=false
  if [[ "$MODE" == "full" && "${SUMMARY[0]}" == "quick: OK"* ]]; then
    record OK quick "${SUMMARY[0]}"
    QUICK_OK=true
  fi
  record FAIL
  echo "Result: FAIL"
  if [[ -n "$SKIP_REASON" ]]; then
    echo "The E2E or differential tests did not run completely ($SKIP_REASON): this is NOT a full verification."
    if [[ "$QUICK_OK" == "true" && "$INDEX_MATCHES_TREE" == "true" ]]; then
      echo "The quick block passed and was recorded as \"quick\". The honest stamp is:"
      echo "  Verified: quick (e2e and differential did not run: $SKIP_REASON)"
    fi
  fi
  exit 1
fi
record OK
echo "Result: OK"
JOINED="$(printf '%s; ' "${SUMMARY[@]}")"
if [[ "$INDEX_MATCHES_TREE" == "true" ]]; then
  echo "Suggested trailer: Verified: $MODE (verify.sh $MODE — ${JOINED%; })"
else
  echo "No suggested trailer: the index does not match the working tree (see WARNING above)."
fi
