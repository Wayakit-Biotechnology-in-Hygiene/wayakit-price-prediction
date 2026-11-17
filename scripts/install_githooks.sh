#!/usr/bin/env bash
# Installs PI's versioned git hooks: sets "git config core.hooksPath scripts/githooks" in
# the .git/config of THIS clone (not global, does not touch other repos).
#
# What it leaves configured:
#   - scripts/githooks/commit-msg: every commit carries the stamp "Verified: <mode> (<detail>)" and, if it says
#     quick or full, it must match an OK run of scripts/verify.sh for the same index.
#   - With core.hooksPath, git stops reading .git/hooks/: if there were custom hooks there (not *.sample), it warns.
#
# Usage (from any folder in the repo; can be run more than once):
#   scripts/install_githooks.sh             # installs
#   scripts/install_githooks.sh --dry-run   # shows what it would do, without changing anything
#   scripts/install_githooks.sh --status    # shows the current configuration
# To uninstall: git config --unset core.hooksPath (done by the lead, by hand).
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
DESIRED="scripts/githooks"
ACTION="${1:-install}"
CURRENT="$(git config --local --get core.hooksPath || true)"

case "$ACTION" in
  --status)
    echo "core.hooksPath (local): ${CURRENT:-<unset: git uses .git/hooks>}"
    [ "$CURRENT" = "$DESIRED" ] && echo "Installed." || echo "Not installed: run scripts/install_githooks.sh"
    exit 0 ;;
  --dry-run|install) ;;
  *) echo "Usage: scripts/install_githooks.sh [--dry-run|--status]" >&2; exit 2 ;;
esac

[ -f "$DESIRED/commit-msg" ] || { echo "ERROR: $DESIRED/commit-msg is missing." >&2; exit 1; }

CUSTOM="$(find "$(git rev-parse --git-dir)/hooks" -maxdepth 1 -type f ! -name '*.sample' 2>/dev/null || true)"
if [ -n "$CUSTOM" ]; then
  echo "WARNING: with core.hooksPath git stops using these hooks in .git/hooks/:"
  printf '  %s\n' $CUSTOM
fi
if [ -n "$CURRENT" ] && [ "$CURRENT" != "$DESIRED" ]; then
  echo "WARNING: core.hooksPath was already '$CURRENT'; changing it to '$DESIRED'."
fi

if [ "$ACTION" = "--dry-run" ]; then
  echo "[dry-run] chmod +x $DESIRED/*"
  echo "[dry-run] git config --local core.hooksPath $DESIRED"
  exit 0
fi

chmod +x "$DESIRED"/*
git config --local core.hooksPath "$DESIRED"
echo "Done: core.hooksPath = $(git config --local --get core.hooksPath)"
echo "From now on every commit needs its \"Verified:\" trailer (see scripts/githooks/commit-msg)."
