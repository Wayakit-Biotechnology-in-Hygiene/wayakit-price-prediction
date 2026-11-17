# Review rules — Price Intelligence

Rules that block a PI commit or PR. They are applied by `pi-reviewer` (and by anyone who reviews).

**They are read from the base branch, never from the working tree:**

```bash
git show "${PI_BASE_REF:-origin/server-improvement}:REVIEW.md"
```

That way a change cannot modify its own checks. The base is `origin/server-improvement` (what runs in production); later it will be `origin/main`. If this file does not exist in the base yet, the PR that creates it is reviewed by Mafer: it is the first step of the setup, a PR with only the checks (see the `team` skill).

## Categories

| Category | What it is | Blocks? |
|---|---|---|
| **Important** | A real bug with a concrete failing scenario, or any item in "What blocks" | Yes |
| **Nit** | Minor improvement; nothing about style unless it hides a bug | No |
| **Pre-existing** | It was already in the base and the change does not make it worse | No; it goes to the pending list and is not charged to the author |

Each finding carries `file:line`, category, failing scenario and suggested fix. A review is valid for one SHA (and its tree): if the code changes, it is reviewed again.

## What blocks

1. **Secrets or identifiers in the diff:** credentials, tokens, API keys, account ID (use `<account>`), instance IDs, IPs or emails, in code, docs, tests, fixtures or logs. Also printing the value of a secret.
2. **Odoo writes without a safety net:** all code that writes to Odoo needs a `--dry-run` path that does not write, must confirm or filter the company (KSA) and must not leave the table empty if the `create` fails. Running the real write needs Mafer's OK.
3. **AWS resources created outside `infra/`:** any boto3 `create_*`/`put_*` or CLI command that creates or changes resources outside an `infra/` script (idempotent, with `--dry-run`, profile `wayakit-vdc`, region `eu-north-1`, tag `project=price-intelligence`).
4. **Mumzworld or SACO behaving differently** without evidence from the F6 regression test (same keywords, before and after, identical outputs). This includes changes in `scraper/config.py` or `scraper/main.py` that affect them.
5. **Silent failures:** exiting with code 0 on an error (`exit()`, `return` instead of `sys.exit(1)`), an `except` that swallows the error, `run_pipeline.sh` steps without `|| exit 1`, uploading stale or empty data to Odoo.
6. **New CSVs with data in the repo**, or data from `data/` or `logs/` copied elsewhere.
7. **Tests weakened or deleted without justification:** removing asserts, widening tolerances, adding `skip`/`xfail`/`sleep`/retries, reducing Hypothesis examples, or adding entries to `tests/differential/approved_changes.json` that do not meet its schema: `file`, `type`, `column` (if the type requires it), `expected_cells` (exact), `reason`, `date` and `"approved_by": "Mafer"`; no entries that cover a whole file.
   - The only `skip` allowed: E2E and differential tests when `data/f1_before_2026-11-15/` is missing (and then the stamp cannot say `full`).
   - The only exception for `xfail`: **`xfail(strict=True)` only for a documented bug, with its counterexample and its addressee** (`pi-dev`, or Mafer if it must be decided what is correct). It covers only that counterexample: the general property stays active and the known case is excluded with `assume(...)` or by filtering the strategy. An `xfail` on a whole property, without `strict=True` or without a counterexample, is Important.
8. **Changes to the checks in the same PR as the code they review.** The checks are: `REVIEW.md`, `.claude/` (`agents/`, `skills/`, `settings.json`), `scripts/` (`verify.sh`, `hooks/`, `install_githooks.sh` and the `commit-msg` hook), `pytest.ini`, `tests/conftest.py`, the harnesses `tests/differential/_harness.py`, `tests/e2e/_helpers.py` and `tests/e2e/_pipeline_sandbox.py` (`run_pipeline.sh` shims), `docs/reliability-checklist.md` and the `.claude/state/` state (round counters and verification record). They go in a separate PR against the base, without `scraper/`, `ml_model/` or `run_pipeline.sh`. New tests and `approved_changes.json` entries do go with their change (rule 7).
   - Technical lock: `scripts/hooks/protect_checks.sh` (PreToolUse) blocks editing these paths except with `PI_EDIT_CHECKS=1`, which only the lead sets, in a session with no team and for a checks-only change; the `TaskCompleted` gate fails if the diff against the base mixes checks with production code. The lock not firing does not waive this rule: the reviewer checks it in the diff.
9. **Outdated docs:** a pipeline or infrastructure change without its update in `docs/` in the same commit.
10. **Commit without a stamp:** the trailer `Verified: quick|full|none (reason)` is missing, or the stamp claims more than what was run.
    - The `commit-msg` hook (installed with `scripts/install_githooks.sh`) validates the format and, for `quick` or `full`, that `.claude/state/last_verification.json` has an OK record of that mode for the same tree being committed.
    - `full` is only valid if E2E and differential tests ran: `verify.sh full` ends in FAIL if they were skipped.
    - The reviewer checks that the `tree` of that record equals `git rev-parse <sha>^{tree}` of the reviewed SHA.

## Reliability checklist

Besides the above, review on every change:

- **Verification:** is there evidence from `scripts/verify.sh` (`.claude/state/last_verification.json` for the same tree, how many tests per marker, what was skipped and why)? Does whatever changed behavior have a test that would have failed before the change? Does every new test carry one of the four markers?
- **Pure functions:** is the new pricing, units or cleaning logic in a function without I/O, and does it have property tests (round trip, invariants, idempotence or equivalence)?
- **Differences against the base:** is every difference in the ML outputs in `approved_changes.json`, and does it match what the commit says?
- **Tests without network:** does no test call AWS, Odoo, Apify, Bedrock, Gemini or the stores, and do the E2E tests run in a temporary copy, not in the repo?
- **Determinism:** is `random_state=42` kept, and is there no dependency on the time, on dict order or on files left by another test? Differential comparisons are always base against new on the same machine, never against production outputs (the local run does not reproduce the production report; see `docs/plan.md`).
- **Known pitfalls in `CLAUDE.md`:** subindustry case, the `DictWriter`'s `extrasaction='raise'`, sales fixed to 2025, `is_relevant` returning `False` without an API key, Gemini key in the URL, Wayakit products in the results.
- **Secrets:** they are read only from `prod/wayakit-app` and no other copy of `get_secret()` was added.
