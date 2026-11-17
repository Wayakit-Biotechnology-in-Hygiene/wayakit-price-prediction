---
name: verify
description: How to run and test Price Intelligence (PI) without touching production. Use it before every commit and after any code change (scraper/, ml_model/, run_pipeline.sh, tests/, scripts/), to pick the checks from docs/reliability-checklist.md, run scripts/verify.sh, run a pipeline stage offline with frozen data, and stamp the commit with the "Verified:" trailer.
allowed-tools: Bash
---

# verify — how to verify PI

Reply to the user (Mafer) in Spanish; everything written in the repository is in English. Everything here runs **locally and with no network**: nothing calls AWS, Odoo, Apify or Bedrock.

## What is never done when verifying

- **Never run `ml_model/odoo_api_price_suggestion.py`** (it writes to and deletes from Odoo). Nor `odoo_api_products.py`, `odoo_api_cotizations.py`, `odoo_api_competitor_products.py` or `scraper/odoo_api_connection_products.py`: they read Odoo with credentials from Secrets Manager.
- Never call AWS (Secrets Manager, Bedrock, EC2, SSM, EventBridge), Odoo, Apify or Gemini from a test or from `verify.sh`. If the code needs it, a fake client is used.
- **Never start the `wayakit-app-server` instance** or run `run_pipeline.sh` for real (it shuts down the machine it runs on and uploads to Odoo). The only exception is its E2E test: it runs in a temporary copy with shims on the `PATH` for `python`, `timeout`, `git`, `conda`, `aws`, `sudo` and `shutdown`, and it is not run without the `sudo` shim.
- Never run the `ml_model/` scripts inside the real repo: they use relative paths and overwrite CSVs and models. Always in a temporary copy (see below).
- Never run `scraper/main.py` (it opens Chrome and scrapes real sites).
- Do not print secrets or copy logs from the instance (`logs/cron.log`, `logs/pipeline_execution.log`, `wayakit_app.log`): they may contain the Gemini key.

## Environment

```bash
cd "$(git rev-parse --show-toplevel)"                                     # in a hook: "$CLAUDE_PROJECT_DIR"
PY="${PI_PYTHON:-$(conda info --base)/envs/wayakit-price-prediction/bin/python}"   # conda env "wayakit-price-prediction", Python 3.10
```

- Local: conda environment `wayakit-price-prediction` (`conda env create -f environment.yml` and `$PY -m pip install -r requirements-dev.txt`). Versions: pandas 2.3.3, scikit-learn 1.7.2, pytest 8.4.2, hypothesis 6.140.2.
- On the instance the environment is still called `wayakit_env` until F3; if it has to be recreated there: `conda env create -f environment.yml -n wayakit_env`.
- `verify.sh` looks for the interpreter in `PI_PYTHON` or in the `wayakit-price-prediction` environment. Do not use another Python: with other versions of numpy or scikit-learn the differential test may give different numbers.
- Once per clone: `scripts/install_githooks.sh` (installs the `commit-msg` hook that validates the stamp; see below).

## The two modes

```bash
scripts/verify.sh quick      # pytest -m "unit or property" -q   (seconds; before every commit)
scripts/verify.sh full       # quick + pytest -m "(e2e or differential) and not unit"   (minutes; if ml_model/, scraper/ or run_pipeline.sh changes; FAILS if E2E or differential tests are skipped)
PI_BASE_REF=origin/main scripts/verify.sh full   # another base version (default origin/server-improvement)
```

| Marker | What it tests | Folder |
|---|---|---|
| `unit` | Individual functions with fixed inputs | `tests/unit/` |
| `property` | Hypothesis: hundreds of inputs per test (`pi` profile, deterministic; `HYPOTHESIS_PROFILE=exploration` to search for new cases) against rules that always hold (round-trip, invariants, idempotence, equivalence) | `tests/property/` |
| `e2e` | Runs real pipeline stages (`ml_model/` and `run_pipeline.sh` with shims) in a temporary copy with frozen data and checks the complete flow | `tests/e2e/` |
| `differential` | Same inputs on the base version (`PI_BASE_REF`) and on the new one; compares outputs against `tests/differential/approved_changes.json` | `tests/differential/` |

For a single test: `$PY -m pytest tests/unit/test_x.py::test_y -q`. Every test carries one of the four markers: a test without a marker runs in no `verify.sh` mode.

`verify.sh` strips the AWS credentials from the environment (`AWS_CONFIG_FILE=/dev/null`, no profile, no IMDS) as a lock. If a test fails with "no credentials", the test is trying to reach AWS: that is the error, not the lock.

### How to read the result

- At the end it prints `===== Summary =====`, one line per block (`OK` or `FAIL` with the pytest count) and `Result: OK|FAIL`. With OK it also prints the **suggested trailer** for the commit.
- Exit code: 0 all good; 1 some test failed **or none were collected** (a verification that runs nothing does not count as green); 2 incorrect usage or `PI_BASE_REF` does not exist (`git fetch origin`).
- `-rfEs` lists the failed tests, the errors and the skipped tests with their reason. If `data/f1_before_2026-11-15/` (ignored by git) is missing, E2E and differential tests are skipped and **`full` exits with FAIL**: without the data, the highest stamp is `quick`, stating that E2E and differential tests did not run.
- Each run leaves `.claude/state/last_verification.json` with the mode, the result and the verified **tree** (`git write-tree` of the index, i.e. what has already been `git add`ed). That is why the order is `git add` everything that goes in the commit, then `verify.sh`, then `git commit`; if there are changes not added with `git add` or untracked files, `verify.sh` warns and does not suggest a trailer. The `commit-msg` hook and `pi-reviewer` read that record: if you change or add a file after verifying, the tree no longer matches and it has to be rerun.

## Running a stage offline (temporary copy + frozen data)

The data from the Nov 13–15, 2026 run is in `data/f1_before_2026-11-15/` (`scraper/` and `ml_model/`). The ML stages (`1a`, `1b`, `2`, `3`) do not use the network; they take about 30 s in total.

```bash
PY="${PI_PYTHON:-$(conda info --base)/envs/wayakit-price-prediction/bin/python}"
REPO="$(git rev-parse --show-toplevel)"
T="$(mktemp -d)"; mkdir "$T/repo"                      # log_config.py writes to ../logs → ends up in $T/logs
rsync -a --exclude .git --exclude data --exclude logs "$REPO/" "$T/repo/"   # code from the working tree
# For the base version instead: git -C "$REPO" archive "${PI_BASE_REF:-origin/server-improvement}" | tar -x -C "$T/repo"
cp -R "$REPO/data/f1_before_2026-11-15/ml_model/." "$T/repo/ml_model/"
cp -R "$REPO/data/f1_before_2026-11-15/scraper/."  "$T/repo/scraper/"
cd "$T/repo"
$PY ml_model/1a_preprocess_data.py                 # competitors_complete + competitor_products_from_odoo → competitor_*_processed.csv
$PY ml_model/1b_preprocess_data.py --run_mode full # → wayakit_products_to_predict_odoo.csv
$PY ml_model/2_train_models.py                     # → ml_model/trained_models/*.joblib
$PY ml_model/3_predicted_prices.py                 # → ml_model/wayakit_prediction_report.csv
```

Watch out:
- **The scripts exit with 0 even when they fail** (`exit()`, `except` blocks that swallow the error). The exit code is not enough: check that the output file exists, is new and meets the rules of the feature map.
- A single stage can be run: the inputs of each one are already in the frozen data.
- Same code + same data on the same machine = same trees (RandomForest with `random_state=42`; verified on 2026-11-17 by training twice). Prediction with `n_jobs=-1` can vary by ~1e-15 between calls (sum of trees across threads), which is why the differential test compares with a 1e-6 tolerance.
- Rerunning the current code on the frozen data does **not** reproduce the production `wayakit_prediction_report.csv`: 520 of 738 prices change, by up to 51.86 SAR, even though the processed files come out identical. The cause is under investigation (CPU/BLAS, scikit-learn or numpy versions on the instance, or that run not being e25e2c9). That is why the differential test compares **base against new on the same machine and on the same inputs**, never against production outputs.

## Feature map (what the user sees)

| Stage | Input | Output | Expected result |
|---|---|---|---|
| Odoo products | `product.master` (KSA) | `ml_model/wayakit_products.csv` | Active products. **Not run when verifying** (Odoo) |
| Search list | `wayakit_products.csv` + `scraper/modifiers_mapping.csv` | `scraper/analysis-odoo.csv` | One row per product × site with its keyword (`Search Modifiers`) |
| Scraping | `analysis-odoo.csv`, `TARGET_MAP` | `scraper/competitors_complete.csv` (13 columns, `scraper/config.py:3-7`) | Rows with `price_sar` > 0 and `total_quantity` > 0, from several sources. Few rows or a source at 0 = bad run (Nov 13–15: 53 rows). **Not run when verifying** (network) |
| Preprocessing `1a` | `competitors_complete.csv`, `competitor_products_from_odoo.csv` | `competitor_volumetric_processed.csv`, `competitor_unit_processed.csv` | Price per liter or per piece finite and > 0; p10–p90 trim |
| Preprocessing `1b` | the two above + `wayakit_products.csv` | `wayakit_products_to_predict_odoo.csv` | Only predictable products (type with competition) |
| Training `2` | quotes + processed files | `trained_models/*.joblib` | Two models (volumetric and units) and market statistics |
| Prediction `3` | models + `wayakit_products_to_predict_odoo.csv` | `wayakit_prediction_report.csv` | **One row per row of `wayakit_products_to_predict_odoo.csv`** (738 with the frozen data; `Product_ID` can repeat: there are duplicate SKUs in Odoo); `predicted_price` without NaN; `predicted_price ≥ round(cost_per_unit × 1.30, 2)` (the price is rounded to 2 decimals); with ≥ 3 competitors, clipped to the market range unless the floor wins |
| Upload | report | `product.price.suggestion` in Odoo | **Never when verifying.** Only with Mafer's OK and the KSA company confirmed |

## Which checks to run: `docs/reliability-checklist.md`

1. **Read the checklist from the base branch**, not from the change's branch (a change cannot relax its own rules):
   ```bash
   git show "${PI_BASE_REF:-origin/server-improvement}":docs/reliability-checklist.md
   ```
   If it does not exist in the base yet, use the local copy and say so in the trailer.
2. Read the diff: `git diff "${PI_BASE_REF:-origin/server-improvement}"...HEAD --stat` and `git diff --cached`.
3. Mark the rows whose **Trigger** matches the files or patterns in the diff. Typically **3 to 10**. If none match, say so.
4. Run only those (plus `verify.sh quick` always, and `full` if the diff touches `ml_model/`, `scraper/` or `run_pipeline.sh`).
5. Record the IDs (`C01`…) in the trailer.

## If something fails: at most 3 rounds

- A **round** = changing something and rerunning the check that failed.
- If the same check is still failing after the **3rd round**, **stop**: no more changes. Escalate to Mafer with: the failing check, the summarized output (without secrets), the 3 things that were tried and the current hypothesis. The round counter is kept by the hooks in `.claude/state/` (ignored by git); do not edit it by hand to "reset" the count.
- A test is not "fixed" to make it pass if what is wrong is the code. If the test is wrong, say so and explain it.

## Flaky tests

- Forbidden: `sleep`, retries (`pytest-rerunfailures`, loops), `@pytest.mark.skip`/`xfail` to hide the failure, raising tolerances without a reason.
- **Only exception for `xfail`:** `xfail(strict=True)` only for a documented bug, with the minimal counterexample in `reason` and the addressee (`pi-dev`, or Mafer if what is correct has to be decided). The `xfail` covers only that counterexample: the general property stays active (the known case is excluded with `assume(...)` or by filtering the strategy) and the bug stays in a minimal test with `@example`. When the bug is fixed, the XPASS makes the verification fail and the `xfail` is removed.
- Look for the root cause: fixed seed (Hypothesis with `derandomize` or `@seed`, `random_state`), row order (sort before comparing), dates (inject the date), shared files (each test in its own `tmp_path`), network (there is no network).
- If the cause is not found, **the test is deleted** and documented in the commit. If it reveals a real risk, ask `pi-dev` (owner of `docs/runbook.md`) to add it under Known issues; `pi-tester` does not edit the runbook.

## The checks do not change together with the code

- The checks are the paths in rule 8 of `REVIEW.md` (`scripts/`, `pytest.ini`, `tests/conftest.py`, the harnesses `tests/e2e/_helpers.py`, `tests/e2e/_pipeline_sandbox.py` and `tests/differential/_harness.py`, `.claude/`, `REVIEW.md`, `docs/reliability-checklist.md` and `.claude/state/`).
- `scripts/hooks/protect_checks.sh` (PreToolUse on `Edit|Write|MultiEdit`) blocks editing them unless `PI_EDIT_CHECKS=1` is set, which only the lead sets, in a session without a team and for a checks-only change.
- The `TaskCompleted` gate fails if the diff against the base mixes checks with production code (`scraper/`, `ml_model/`, `run_pipeline.sh`): they go in separate PRs.
- New tests (`tests/unit/`, `tests/property/`, `test_*.py`) and entries in `approved_changes.json` do go with the code they test.

## Commit stamp: the `Verified:` trailer

Every commit carries a final line `Verified: <quick|full|none> (<detail or reason>)` stating how it was verified (same format as `REVIEW.md`, rule 10). `scripts/verify.sh` prints the suggested trailer when it finishes green. It goes in the last paragraph of the message: if the commit carries `Co-Authored-By`, put `Verified:` right above it, in the same paragraph (no blank line between the two), or the `commit-msg` hook does not recognize it as a trailer. Formats:

```
Verified: full (verify.sh full — quick: OK (<n> passed); e2e-differential: OK (<n> passed); checklist C01, C03, C17 read from origin/server-improvement)
Verified: quick (verify.sh quick — quick: OK (<n> passed); e2e and differential did not run: data/f1_before_2026-11-15 missing; checklist C08)
Verified: none (docs only)
```

- `none` is valid and mandatory when nothing was run; always with the reason in parentheses.
- **The `commit-msg` hook** (installed by `scripts/install_githooks.sh`) reads the final message with `git interpret-trailers --parse` and rejects the commit if the trailer does not match `Verified: <quick|full|none> (<detail>)`. If it says `quick` or `full`, it also requires an OK record of that mode in `.claude/state/verifications.jsonl` for the **same tree** about to be committed. The record is the tree of the **index** (what has already been `git add`ed), so the order is: check `git status` so nothing unrelated gets in, `git add` everything that goes in the commit, `verify.sh` and `git commit`, without touching anything in between.
- Never put `OK` for something that was not run on this version of the code, and never copy the trailer from another commit.
- Before the commit: `git diff --cached` without keys or IDs (check C19) and without new CSVs (C20).
