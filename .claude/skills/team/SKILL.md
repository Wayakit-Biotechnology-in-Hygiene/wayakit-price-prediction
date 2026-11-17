---
name: team
description: The lead's (main session's) manual for assembling and directing a Price Intelligence agent team with Agent Teams (pi-dev, pi-tester, pi-reviewer). Use it when a ticket or phase of docs/plan.md has several parts (code + tests + review) and a team is worth the cost; for a small change, work alone.
---

# PI team: the lead's manual

You are the lead: the main session. You organize, decide, talk to Mafer and are **the only one that commits**. Teammates do not commit, push or switch branches.

## First step of this setup: the checks to the base, on their own

The rules are read from the base branch (`${PI_BASE_REF:-origin/server-improvement}`), so while `REVIEW.md`, the `verify` skill, `docs/reliability-checklist.md` and the hooks are not in the base, "the rules come from the base" does not work and the gate rejects any production change (it sees it mixed with the checks).

That is why, **before the first team ticket**:

1. Open a separate PR against the base with **only** the verification setup: `REVIEW.md`, `.claude/` (agents, skills, `settings.json`), `scripts/`, `pytest.ini`, `requirements-dev.txt`, `tests/`, `docs/reliability-checklist.md` and the docs that describe them. **Nothing** from `scraper/`, `ml_model/` or `run_pipeline.sh`.
2. `pi-reviewer` does not approve that PR (the rules do not exist in the base yet): Mafer reviews it.
3. Once it is in the base, the working branch is rebased onto it and work can start.

## When to use a team

- Yes: work with several parts that can be split without stepping on each other (for example, F2: safe upload to Odoo + ML property tests + regression).
- No: a one-line fix, a question or a docs change. **A team spends many more tokens** (each teammate is a full session that loads `CLAUDE.md` and reads the repo); use it only when the work really has several parts.

## Getting started

**Before creating the team:** confirm that `.claude/settings.local.json` does not have `PI_EDIT_CHECKS` (teammates inherit the Claude Code environment and the lock would be open for all of them). If the checks are not in the base branch yet, that file must contain only `PI_CHECKS_BASE=<sha of the checks commit>` (see `docs/runbook.md`, checks-only session).

1. Read `CLAUDE.md`, the phase in `docs/plan.md` and the base `REVIEW.md`. Run `git fetch origin` (the reviewer cannot). If the clone is new, run `scripts/install_githooks.sh` once (`commit-msg` hook that validates the stamp).
2. Create the 3 teammates with the Agent tool, each with its name and its type from `.claude/agents/`. Each file has a single owner:

   | Owner | Files |
   |---|---|
   | `pi-dev` | `scraper/`, `ml_model/`, `run_pipeline.sh`, `infra/`, `log_config.py`, `environment.yml`, `requirements.txt`, and the docs that change with the code: `docs/runbook.md`, `docs/resources.md`, `docs/decisions.md`, `README.md` |
   | `pi-tester` | The tests: `tests/unit/`, `tests/property/`, `tests/e2e/test_*.py`, `tests/differential/test_*.py`, `tests/differential/approved_changes.json` (only with Mafer's OK) and `requirements-dev.txt` |
   | `pi-reviewer` | Nothing (read-only) |
   | Lead (you) | `CLAUDE.md`, `docs/plan.md` and `.claude/skills/team/` (this skill is under `.claude/`, so the lock blocks it too: it is changed as a checks-only change) |
   | Lead, **only in a checks-only change** | The checks (rule 8 of `REVIEW.md`): `REVIEW.md`, `.claude/agents/`, `.claude/skills/verify/`, `.claude/settings.json`, `scripts/` (`verify.sh`, hooks, `install_githooks.sh`), `pytest.ini`, `tests/conftest.py`, `tests/e2e/_helpers.py`, `tests/e2e/_pipeline_sandbox.py`, `tests/differential/_harness.py` and `docs/reliability-checklist.md` |

   **Ownership is disjoint:** two teammates editing the same file overwrite each other. If a task needs another teammate's file, it is requested from its owner by message. If `pi-tester` needs to change a check, he sends you the exact change and you apply it in a checks-only PR.
3. Each teammate's prompt does not include your conversation. Include: the ticket and the phase, which files to touch, what must not change, the base branch (`${PI_BASE_REF:-origin/server-improvement}`) and the instruction to **read `.claude/skills/verify/SKILL.md`** (the frontmatter `skills` field does not apply to teammates; they do load `CLAUDE.md` and the project skills).
4. Create the shared task list (TaskCreate): **5 or 6 tasks per teammate**, each small and verifiable, with explicit dependencies (the property test depends on the pure function existing; the review depends on the test being green). Assign each task to its owner.

## Checks lock

- `scripts/hooks/protect_checks.sh` (PreToolUse on `Edit|Write|MultiEdit`) blocks everyone, you included, from editing the checks and `.claude/state/`.
- **Only you set `PI_EDIT_CHECKS=1`**, and only for a checks-only change: in a separate session, with no active team (teammates would inherit the variable), for example `PI_EDIT_CHECKS=1 claude`. When you finish, close that session. Never set it to unblock a teammate or in a session where production code is touched.
- The `TaskCompleted` gate also fails if the diff against the base mixes checks with `scraper/`, `ml_model/` or `run_pipeline.sh`.

## The cycle

```
pi-dev ──(verify quick green + function contract + "stable tree")──▶ pi-tester
pi-tester ──(tests green, count per marker)──▶ pi-reviewer (with a SHA)
pi-reviewer ──(Important findings)──▶ pi-dev / pi-tester ──▶ new round
no Important findings ──▶ lead: verify full ──▶ commit with stamp
```

- **Gates:**
  - `scripts/verify.sh quick` (unit + property) green before every handoff between teammates.
  - `scripts/verify.sh full` (+ e2e + differential) green before **every** commit. You run it. It exits with FAIL if E2E or differential tests were skipped (for example, without `data/f1_before_2026-11-15/`).
- **Close tasks only with a stable tree.** The working tree is shared and the `TaskCompleted` gate runs `verify.sh` on all of it. A task is marked completed only at a sync point: `pi-dev` announced "stable tree" (no half-done edits) and nobody is editing. Otherwise the gate fails on someone else's half-done change, adds a round to whoever was closing and may escalate through no fault of theirs. If that happens, check `git status --short` and which files failed before blaming the task.
- **Review tied to a SHA.** When `pi-tester` gives green, make a local work commit (no push) and pass the SHA to `pi-reviewer`. The reviewer compares that SHA's tree with the one in `.claude/state/last_verification.json`. If the code changes afterwards, the review is stale: new commit, new SHA, new round. If you combine commits at the end (squash), the approval still holds only if the tree is the same (`git rev-parse <sha>^{tree}`).
- **Approved differences.** If the change moves the ML outputs on purpose, ask Mafer for the OK with the before/after; with her OK, `pi-tester` adds the entry to `tests/differential/approved_changes.json` (`file`, `type`, `column`, `expected_cells`, reason, date, `"approved_by": "Mafer"`; narrowed with `subindustry` or `key` if applicable).
- The hooks enforce this: a task marked completed with a red verification goes back to open with the error. That is no reason to skip the gate.

## 3-rounds rule

One round = one fix + rerunning the check (or reviewing again). If a check keeps failing, or an Important finding stays open, **after 3 rounds** the teammate stops and writes to you. You do not open a fourth round: you escalate to Mafer with the check, the exact error, what was tried in each round and the decision that is needed. The counter lives in `.claude/state/` (ignored by git and blocked by `protect_checks.sh`); nobody deletes it to reset the count. Also check `.claude/state/escalations.log`: the gate records every escalation there even if the teammate did not write to you.

## Commit

- Only you, on the working branch (currently `pi-improvements`), and only with the review free of Important findings and `verify full` green. Check `git diff --cached` for keys before every commit.
- Docs in the same commit as the code (rule from `CLAUDE.md`). Checks and production code, never in the same commit or the same PR.
- **Mandatory stamp** as a trailer in the message:

  ```
  Verified: full (verify.sh full: <n> unit, <n> property, <n> e2e, <n> differential; base origin/server-improvement)
  ```

  Values: `quick`, `full` or `none` (there is no `e2e` stamp: E2E tests are part of `full`), always with the reason or detail in parentheses. Examples: `Verified: quick (no data/f1_before_2026-11-15/: e2e and differential did not run)`, `Verified: none (docs only)`. The stamp says what was run, never more.
- The `commit-msg` hook validates it: the trailer format and, for `quick` or `full`, an OK record in `.claude/state/last_verification.json` for the **same tree** being committed. The record is of the index: `git add` everything that goes in the commit, then run `verify.sh` and then `git commit`, without touching anything in between.
- If a flaky test was deleted, the commit says what it covered, what was seen and what is left to cover.

## Pruning

When closing the ticket, ask `pi-tester` for a list of redundant tests (they repeat another one, test deleted code, can never fail) and `pi-dev` for the docs that went stale (whatever says "Planned (F#)" and already exists, steps that no longer apply). They are deleted with the reason in the commit.

## Closing the team

When no tasks are left open: summary to Mafer (what changed, stamp of each commit, Pre-existing findings left for the backlog, escalations), and shut down the teammates.

## Rules that are never broken (for you too)

- No credentials, account or instance IDs, IPs or emails in code, commits, docs or messages.
- Mafer's OK before: creating AWS resources, accepting EULAs, requesting quotas or writing to Odoo. Confirm the KSA company before reading or writing in Odoo.
- Mumzworld and SACO do not change without the F6 regression test. No CSV with data in the repo.
- No test calls AWS, Odoo, Apify or Bedrock.
