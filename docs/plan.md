# Improvement plan — Price Intelligence (Nov–Dec 2026)

Status: **draft to review with Mafer**, on branch `pi-improvements`, which branches from `server-improvement` (the history up to PR #1 was on `pi-mejoras`). **Correction (F1, 2026-11-17): the instance runs `server-improvement` (e25e2c9), not `main`.** Changes in AWS so far: Apify calibration and model test (Qwen and DeepSeek EULAs accepted, 2026-11-16); in F1 (2026-11-17) a disk snapshot and the `AmazonSSMManagedInstanceCore` policy on the instance role. Nothing in Odoo.

Decided by Mafer (2026-11-16):
- Amazon KSA and Noon KSA are scraped **with Apify, using the same actors as VdC**: `junglee/amazon-crawler` and `saswave/noon-product-scraper`.
- Mumzworld and SACO stay as they are.
- The proposed CI/CD (pull-on-boot plus start-up in "deploy only" mode) is approved.
- **Gemini is replaced by Bedrock: DeepSeek V3.2 as primary and Haiku 4.5 as fallback** (benchmark of 2026-11-16, section 5).
- Nobody is asked to define what counts as "local".
- **Monthly run on the last Thursday of each month.** It does not clash with VdC's, which runs on the first Sunday.
- Apify goes on the VdC account. The Starter plan is upgraded to cover the spend.
- Apify calibration run approved (2026-11-16).

Tickets:
- **A**: [fix Pets and migrate B2C to Apify](https://app.notion.com/p/41abe65f8c1381ffb1dad8a51e4eb87a). Estimate 8, due 2026-11-30.
- **B**: [infrastructure as code and CI/CD to EC2](https://app.notion.com/p/41abe65f8c1381e88806e23603e434b3). Estimate 5, due 2026-12-03.

Reference: the `voz-del-cliente` (VdC) repo. The `infra/` and `docs/` conventions are copied from it.

---

## 1. How it works today (verified)

**Data flow:** `scraper/` (Selenium + Gemini to decide relevance) → `competitors_complete.csv` → `ml_model/` (price per liter or per piece, p10–p90 trimming, RandomForest) → `product.price.suggestion` in Odoo.

**Execution:**
- EC2 instance `wayakit-app-server` (t3.medium, AL2023, eu-north-1). It is almost always stopped.
- Two EventBridge Scheduler schedules in the `default` group start it: Thursday and Friday at 21:50.
- Inside the instance, the `ec2-user` crontab launches `run_pipeline.sh` on Friday 04:00 UTC and `--stage 2` on Saturday 04:00 UTC (Thursday and Friday 22:00 CDMX):
  - **stage 1** prepares the search list;
  - between stages there is a manual review through git;
  - **stage 2** runs `git pull`, scrapes, trains and uploads to Odoo.
- When it finishes, the instance shuts itself down with `trap EXIT`.

**Findings that shape the plan:**

| # | Finding | Where |
|---|---|---|
| 1 | **There is no Noon scraper.** In B2C today there are only Amazon (amazon.sa, Selenium), Mumzworld and SACO. For Pets **only Amazon is used** | `scraper/config.py:9-24` |
| 2 | The model **does not use** the source; since e25e2c9 it does use `company` (the brand on Amazon, Mumzworld and SACO). Neither the seller nor the country of origin is captured. Today there is no way to separate imported from local | `ml_model/2_train_models.py:160-169` |
| 3 | In the 2024 snapshot, the Pets median was about 262 SAR/L, versus about 50 in Home. 39 % of sizes came in fl oz (a sign of imported). With n ≤ 2 per type, the p10–p90 trimming does nothing. **Correction (2026-11-17):** that holds with 1 price or with 2 equal ones; with **2 different prices the trimming leaves the group empty** (finding 12, bug 6) | `2_train_models.py:46-60` |
| 4 | Odoo sales are **fixed to 2025** | `ml_model/odoo_api_cotizations.py:75-76` |
| 5 | The state (CSV, `last_full_run.txt`, models and logs) lives only on the EC2 disk. There is a manual snapshot from 2026-11-17 (F1) | `run_pipeline.sh:7-24` |
| 6 | Stage 2 runs `git pull` without checking the result. Many errors end with exit code 0 (`exit()`, `except` blocks that swallow the error) | `run_pipeline.sh:133`, `odoo_api_price_suggestion.py:52-109` |
| 7 | There are 6 copies of `get_secret()`. All of them read `prod/wayakit-app` | `ml_model/odoo_api_*.py`, `scraper/...` |
| 8 | The instance has no fixed IP and no userData. Since F1 (2026-11-17) it is registered in SSM. There is no GitHub OIDC provider in IAM | AWS |
| 9 | The Odoo query **does not filter by company (KSA/MX)** | `ml_model/odoo_api_*.py` |
| 10 | **The instance runs `server-improvement`** (e25e2c9, "model improvement"), which changes training, prediction and `run_pipeline.sh` (partial mode disabled). `pi-mejoras` was fast-forwarded to e25e2c9 on 2026-11-17 | F1 (instance) |
| 11 | **The last full run (Nov 13–15, 2026) failed in scraping and still uploaded to Odoo.** Scraping hit the 30 h timeout; `competitors_complete.csv` ended up with 53 rows (SACO Automotive 36, AeroSense 14, Fine Airports 3; nothing from Amazon, Home or Pets). The pipeline trained, uploaded in full mode (deleted the previous suggestions) and finished with exit code 0. The current Odoo suggestions come almost entirely from `competitor.product`. GoGreen failed many times with "Could not set language to English" | F1, `logs/pipeline_execution.log` |
| 12 | **The property tests (2026-11-17) found 11 bugs** in the size parsers and in preprocessing and training. Each one has an `xfail(strict=True)` test with its counterexample. Details and phase in [Bugs from the property tests](#bugs-from-the-property-tests-2026-11-17) | `tests/unit/`, `tests/property/` |
| 13 | **The local run does not reproduce the production report.** With the frozen F1 data and the same code (e25e2c9), 520 of 738 `predicted_price` values change, by up to 51.86 SAR; the processed files come out identical and `to_predict` differs only in floating-point noise (~1e-14). Cause under investigation: CPU/BLAS, scikit-learn or numpy versions on the instance, or that run not being e25e2c9. That is why the differential tests compare **base against new version on the same machine**, never against production outputs | `data/f1_before_2026-11-15/`, `tests/differential/` |

### Bugs from the property tests (2026-11-17)

Found by `pi-tester` while writing the unit and property tests for the current code (e25e2c9). They were not fixed: each one remains as `xfail(strict=True)` with the minimal counterexample; when it is fixed, the test passes and the `xfail` must be removed. The parsers in `scraper/utils.py` are used by Mumzworld, SACO, Fine, GoGreen and OfficeSupply (and by the Selenium Amazon scraper until F6): fixing them would change Mumzworld and SACO rows. **Decided (question 13, 2026-11-22): `utils.py` is not changed; bugs 1–4 (and 10–11) are fixed only in the Apify adapters' own parsing (`scraper/scrapers/apify_common.py`).** The strict `xfail` tests of `utils.py` stay as they are.

| # | Bug | Counterexample | Where | Phase |
|---|---|---|---|---|
| 1 | `litre` is not recognized as liter (the branch looks for "liter") | `'1 litre'` → unit `litre`, normalized 1.0 (should be L and 1000); `'6 x 1 litre'` → normalized 6 | `scraper/utils.py:3` (`parse_volume_string`), `:64` (`parse_volume_with_multiplier`) | Shared parser unchanged (question 13); fixed only in the Apify adapters (`scraper/scrapers/apify_common.py`, F6) |
| 2 | `kilograms` is read as grams ("gram" is inside "kilograms" and that branch comes first) | `'2 kilograms'` → unit `g`, normalized 2.0 (should be kg and 2000) | `scraper/utils.py:3` | Shared parser unchanged (question 13); fixed only in the Apify adapters (`scraper/scrapers/apify_common.py`, F6) |
| 3 | Thousands separator | `'Shampoo 1,000 ml'` → quantity 0.0 (the regex takes only "000") | `scraper/utils.py:3` | Shared parser unchanged (question 13); fixed only in the Apify adapters (`scraper/scrapers/apify_common.py`, F6) |
| 4 | Decimal without leading zero | `'Bleach .5 L'` → quantity 5.0 (5 L instead of 0.5 L) | `scraper/utils.py:3` | Shared parser unchanged (question 13); fixed only in the Apify adapters (`scraper/scrapers/apify_common.py`, F6) |
| 5 | `1a` sends `kg` and `litre` to the per-piece model: it only recognizes `ml`, `l`, `fl oz` and `g` as volume | `'Powder 2 kg'` at 30 SAR → `competitor_unit_processed.csv` with total_quantity 2 and price_per_item 15 | `ml_model/1a_preprocess_data.py:79-80` | F7a (small fixes) |
| 6 | **The p10–p90 trimming empties groups with 2 different prices** (bounds 1.1 and 1.9 for [1.0, 2.0]; the filter is inclusive and nothing remains) | In the F1 data, **14 of 151 volumetric types and 1 of 15 unit types end up with no competitor at all** in training | `ml_model/2_train_models.py:46-60` | **F7a, directly for Pets** (small n per type; "robust trimming when n is small"); in Pets (F5), **2 types are emptied** (1 of 19 volumetric and 1 of 3 unit), neither with a Wayakit SKU |
| 7 | Repeated SKUs duplicate quotes (the `merge` on `Product_ID`) | A quote for `P1` with `P1` twice in products → 2 rows. In F1: 4 repeated SKUs, 162 quotes → 163 rows | `ml_model/2_train_models.py:37` (`prepare_wayakit_training_data`) | F7a (and the SKU cleanup in section 7.3) |
| 8 | `process_volumetric_data` does not drop quantities ≤ 0 (the unit one does) | `ml`, total_quantity −500, price 1 → volume_liters −0.5 and price_per_liter −2.0, which go into training | `ml_model/1a_preprocess_data.py:39-59` | F7a (small fixes, C18) |
| 9 | **Possible bug (confirm with Mafer):** the concentration "3X" is taken as 3 pieces | `'Detergent 3X concentrate 500 ml'` → 1500 ml | `scraper/utils.py:64` | Open (question 12) for the shared parser; the Apify adapters never take "3X" as a multiplier (F6) |
| 10 | Minor: scientific notation in the multiplier (only with totals < 1e-4 or ≥ 1e16, unrealistic) | `'(2 Bottles x 0.00001 ml)'` → quantity 2e-05 but normalized 5.0 | `scraper/utils.py:64` | No phase (noted) |
| 11 | Minor: huge numbers in the count parsers raise `ValueError` (`int()` digit limit in Python ≥ 3.10.7); unrealistic in titles | `'9'*4301 + ' wipes'` | `scraper/utils.py:31` (`parse_count_string`) | No phase (noted) |

### Security: notify IT

- **Confirmed in F1:** the Gemini key is in plain text in `logs/cron.log` (72 lines) and `logs/pipeline_execution.log` (87 lines) on the instance, because of the `requests` HTTP errors that include the URL (`scraper/services/ai_service.py:54-55`). It does not appear in `~/logs/wayakit_app.log*`. Action: **revoke or rotate the key in Google now**, scrub or delete those logs and, when migrating to Bedrock, remove it from `prod/wayakit-app`.
- The instance role has `SecretsManagerReadWrite`: it can read and write **all** secrets in the account, including `voz-del-cliente/api-keys`.
- ~~Push credentials on the server~~: verified in F1, there is no `credential.helper` or `~/.git-credentials`, and the https remote carries no credentials.

---

## 2. Design decisions (proposed)

| Topic | Proposal | Why |
|---|---|---|
| Stages | **A single monthly run, always full.** No stage 2, no partial mode and no `git pull`. The search list comes from the versioned `modifiers_mapping.csv`, and every change goes through a PR | Ticket A asks for a monthly run. This removes the git conflict, the mix of versions between stages and one schedule. `server-improvement` already goes in that direction |
| IaC | Same as VdC: **one boto3 script per resource** in `infra/`, re-runnable, with `--dry-run`, docstring in English, shared `config.py` and the `project=price-intelligence` tag | Same tooling as VdC, as ticket B asks |
| CI/CD | **Pull-on-boot.** GitHub Actions (OIDC, no keys) runs the tests, publishes `releases/<sha>.tar.gz` to S3 and updates the `current` pointer. Then it **starts the instance in "deploy only" mode**: the instance installs the release, verifies, switches the symlink atomically and shuts down | Works with the instance stopped. A push arrives with no manual steps. If the deploy fails, the symlink is not touched and the previous version keeps working |
| Rollback | `workflow_dispatch` with a `sha`, which moves the pointer and re-runs the deploy. The instance keeps the last 5 releases | Same pattern as VdC's `--rollback` |
| State | S3 `price-intelligence-<account>`, with the prefixes `data/`, `models/`, `state/`, `logs/` and `releases/` | To recreate the instance without losing anything (criterion of B) |
| Secrets | **Keep `prod/wayakit-app`**, add the Apify key, centralize in a single module (`pi_secrets.py`) and restrict the role to that secret's ARN | It is the minimum. Applies if nobody else uses that secret (question 4) |
| Upload to Odoo | First `create` the new batch and, only if it succeeds, `unlink` the previous one (IDs saved, retryable). Add `--dry-run`. Filter by the KSA company | If something fails, the table is never left empty |
| Pets | See F7a and F7b. The corrections come from the data (robust statistics and, if the actor provides it, the country of origin). No lists that a person has to maintain | Mafer's decision |
| Relevance AI | **Bedrock: DeepSeek V3.2 as primary and Haiku 4.5 as fallback**, with the Converse API, JSON schema and **always in batches**. The `RelevanceAgent` interface is kept so the scrapers do not change | Mafer's decision and the benchmark of 2026-11-16 (section 5) |

---

## 3. Phases

Reference date: 2026-11-16. About 2 weeks remain for the two tickets, so the scope is already trimmed.

### F0 · Base documentation (1–1.5 days) — first

The same structure as VdC is used. The rule: **every pipeline or infrastructure change updates the docs in the same commit.**

- `CLAUDE.md`, for Claude:
  - what the project is;
  - rules that are never broken: credentials, Odoo company, Mafer's OK, Mumzworld and SACO are not touched, no CSV in git;
  - code map and scraper contract;
  - CSV schema;
  - known pitfalls;
  - `infra/` conventions.
- `README.md`, for people: what it does, architecture, how to run it and links.
- `docs/runbook.md`: operating, running by hand, deploying, rollback, reviewing alerts, re-uploading to Odoo and a known-issues table (`Symptom | Cause | Fix`).
- `docs/resources.md`: table of what exists in AWS (`Resource | Name | Script | Cost`).
- `docs/decisions.md`: an append-only `Date | Decision | Reason` table, and a `## Pending` section.
- `environment.yml` with pinned versions, taken from `pip freeze` on the instance.

### F1 · Instance inventory — **done on 2026-11-17**

Result: `ec2-user` crontab (Fri and Sat 04:00 UTC), branch `server-improvement`, Python 3.10.20 (pinned in `environment.yml`), no stored git credentials, Gemini key in the logs, last run failed (finding 11). "Before" data in `data/f1_before_2026-11-15/` (ignored by git): `last_full_run` 2026-11-13 04:00:09, `run_mode` full. The schedules were paused during the inventory and re-enabled.

Original plan:

1. Take a **snapshot of the volume**.
2. Disable the schedules.
3. Start the instance.
4. Review the cron (if it has `@reboot`, turn it off before it fires), the checked-out branch, local changes, the conda environment, the git credentials and the Gemini log.
5. Copy the current CSVs and models to S3. That is the **"before" snapshot** for ticket A.

### F2 · Harden the pipeline (2 days) — **implemented on branch `f2-hardening` (2026-11-17), deployed on 2026-11-22** (with the F3 cutover)

Result (details in `docs/decisions.md` and `docs/runbook.md`):
- Done: `|| exit 1` on every step and no `git pull` in stage 2 (the two stages stay while question 2 is open); `sys.exit(1)` and no swallowed exceptions in the ML and Odoo scripts (Odoo reads with 0 records also exit 1); `scraper/main.py` writes to a temporary file and renames it only on success; `pi_secrets.py` (plus `pi_odoo.py` for the connection) replaces the 6 copies of `get_secret()`; Bedrock (DeepSeek V3.2 + Haiku 4.5) replaces Gemini with the same `RelevanceAgent` interface; safe upload (company check, create first, unlink the saved IDs, `--dry-run`, `--cleanup-previous`); sales from the last rolling 12 months; coverage report `scraper/coverage_report.csv` (section 6, step 1). The ML differential shows no price change.
- **Deployed on 2026-11-22** with the F3 cutover (`server-improvement` @ 636bcf0), after the new instance role granted `bedrock:InvokeModel`. Before that it was held back on purpose: it was merged into `server-improvement` only after F3 (Mafer, 2026-11-18).
- Left for later: `infra/bedrock_profile.py` and the IAM permission (F3); the single monthly stage (question 2); the company filter beyond the one-company check; the bake-off script in the repo; environment parity (finding 13); the property-test bugs (finding 12) and the `TARGET_MAP` case bug (C06), which change rows.

Original plan:


- `|| exit 1` on every step (do not use `set -u`, because it breaks conda) and `sys.exit(1)` in the Odoo scripts.
- Do not delete `competitors_complete.csv` before the new one exists: write to a temporary file and rename it.
- A single monthly stage. Remove `git pull`.
- `pi_secrets.py`.
- `ai_service.py` moves from Gemini to Bedrock, in batches and with the same interface. Needs `infra/bedrock_profile.py` and the `bedrock:InvokeModel` permission on the instance role.
- Safe upload to Odoo with `--dry-run` and a company filter.
- Odoo sales from the last rolling 12 months, instead of fixed 2025.
- **Before starting F2:** push the verification setup (`REVIEW.md`, `.claude/`, `scripts/`, `tests/`, `pytest.ini`, `docs/reliability-checklist.md`) to the base, in a separate PR that Mafer reviews, with no production code. Without it, "the rules are read from the base" does not work (`team` skill).
- **Environment parity (finding 13):** compare the instance versions (numpy, scikit-learn, threadpoolctl/BLAS, CPU) with `environment.yml` and confirm which code produced the Nov 13–15 run, until it is explained why the local run gives 520 different prices. Meanwhile, no test compares against production outputs.
- The bugs from finding 12 are not fixed in F2 (they are not hardening); their strict `xfail` tests stay as a reminder.

### F3 · `infra/` (3 days) — each script is run with Mafer's OK

**Status (2026-11-22): applied.** Steps 1–7 on 2026-11-18 and the cutover (steps 8–13) on 2026-11-22, with Mafer's OK: weekly schedules disabled, monthly schedule ENABLED, instance on `price-intelligence-instance`, F2 + F3 deployed (`server-improvement` @ 636bcf0), bootstrap done, smoke test on the instance (secret read, Bedrock preflight) OK, `check_pi` 74 OK / 0 WARN / 0 FAIL. First monthly run: Thursday 2026-11-26, 21:50 CDMX. Decisions: production branch `main` at the end (deploy role trust); new instance role instead of editing the old one, after a read-only CloudTrail audit; failure alerts by email through SNS (address passed at run time); **one run, no stages** (Mafer), started by the monthly schedule and guarded on boot; schedule created disabled. Next: check the first monthly run (2026-11-26); then revoke the Gemini key and remove it from `prod/wayakit-app`; optionally `schedule.py --delete-retired`.

| Script | What it sets up |
|---|---|
| `config.py` | Profile, region, tag, names and cron |
| `bucket.py` | State and releases bucket: encryption, no public access, lifecycle |
| `instance_role.py` | Least privilege: the PI secret, the bucket, `AmazonSSMManagedInstanceCore` and `PutMetricData`. Replaces `SecretsManagerReadWrite` |
| `ec2_instance.py` | **Adopts** the current instance or **creates it** (`--create`) from AL2023 with a userData that runs `bootstrap.sh` (conda, Chrome, systemd, SSM agent). Requires IMDSv2 |
| `bootstrap.sh` | `pi-deploy.service` (installs the release) and `pi-pipeline.service` (runs the pipeline, except in "deploy only" mode) |
| `schedule.py` | `price-intelligence` group and monthly schedule. The two `default` schedules are retired through the `RETIRED` list |
| `scheduler_role.py` | Only `ec2:StartInstances` on this instance |
| `deploy_role.py` | GitHub OIDC provider and a role limited to `repo:<org>/wayakit-price-prediction:ref:refs/heads/main` |
| `check_pi.py` | Read-only check: `[OK]`, `[FAIL]` or `[WARN]`. Covers the ticket test "change a schedule from code and see it in AWS" |

For later, outside the deadline: `alarms.py` and `budget.py`. In the meantime an SNS notification from the exit trap is used.

### F4 · CI/CD (2 days) — **implemented on branches `f4-ci` (checks) and `f4-cicd` (2026-11-22); rollout steps 1–4 applied; `main` after the other tickets**

**Status (2026-11-22):** code and tests done and merged into `server-improvement` (PR #7 checks, PR #8 code; `CI` green). `ci.yml` (lint, ShellCheck, `py_compile` 3.10 and 3.9, gitleaks, `verify.sh quick`) goes in its own checks-only PR (`f4-ci`). `f4-cicd` has `deploy.yml` (after a green CI on a push to `main`, or `workflow_dispatch` with a `sha` for rollback), `infra/release.py` (publish, move `releases/current`, deploy-only request, start, wait for the result, restore the pointer on failure), `infra/instance/pi_deploy.py` + `pi-deploy.service` (pull-on-boot on every boot: sha256, safe extract, atomic `current` symlink, keeps 5, shuts down only after a deploy-only boot and never inside the monthly window) and the new deploy role policy. Changes against the plan: the deploy-only signal is an S3 request object instead of the `pi-mode` tag, and a changed `environment.yml` is refused unless `--allow-env-update` is run over SSM (see `decisions.md`, 2026-11-22). Applied 2026-11-22 (Mafer): the deploy role policy re-applied (`check_pi --only iam oidc` 0 FAIL) and the GitHub secret `PI_DEPLOY_ROLE_ARN` created. Also applied the same day: `bootstrap.sh` on the instance (`pi-deploy.service` enabled; no release pointer yet, so it does nothing). Pending, after the other tickets (Mafer) but before the ticket B deadline and never on the last Thursday of the month: merge `server-improvement` into `main` (first automatic deploy), then the ticket tests (`docs/runbook.md`, *F4 rollout (once)*). Question 8 (manual approval) is still open; default: none.

Original plan:

- `ci.yml`:
  - lint;
  - `scripts/verify.sh quick` (pytest with the `unit` and `property` markers: Apify converters, scraper contract, infra with MagicMock as VdC does);
  - ML smoke test with synthetic data;
  - gitleaks.
- `deploy.yml` (only on main and with CI green): package → S3 → pointer → start the instance in "deploy only" → wait for the OK signal → done.
- Ticket tests:
  - a minor change reaches EC2;
  - a deliberately broken deploy does not change `current`;
  - rollback to a previous SHA works.

### F5 · Pets diagnosis (1 day), in parallel with F3/F4 — **done on 2026-11-22** ([f5-pets-diagnosis.md](f5-pets-diagnosis.md))

With the F1 snapshot (`data/f1_before_2026-11-15/`; note: that run brought no Pets rows from scraping, so Pets is explained almost entirely by `competitor_products_from_odoo.csv`), for each Pets product type:
- n before and after trimming;
- median, p75 and maximum SAR/L;
- % of sizes in fl oz;
- number of brands;
- % of multipacks;
- rows with price 0 or with 5 mL or less;
- volume of Wayakit SKUs versus the competition's range;
- ratio between the suggested price and the cost, and the current price in Odoo **KSA**.

### F6 · Apify for Amazon and Noon (3 days) — **implemented on branch `f6-apify` (2026-11-22)**

**Status (2026-11-23):** merged into `server-improvement` (PR #10), not on the instance. `APIFY_API_TOKEN` is in `prod/wayakit-app` (Mafer, 2026-11-23). The first paid smoke run (2026-11-23, "pet urine odor remover", USD 0.26 charged) passed the technical checks (token, Bedrock, attribute nesting, SAR prices, Wayakit exclusion, 0 AI failures) and found three problems: Noon's `global` flag marked every Noon row `imported`, 23 of 50 Amazon items had no size, and the budget undercounts (Mafer: accepted). The fixes (branch `f6-smoke-fixes`: no Noon details run and no `global` signal, a fl oz size no longer means `imported`, plain oz and gallons as fl oz, and conservative sizes after four review rounds: an ambiguous title drops the row, kits of different products and solids are not stored, same-product multipacks are kept with their total, and a plain oz or a gallon in a title needs confirmation; Mafer, 2026-11-23) were checked by a second paid smoke run (2026-11-23, same keyword, USD 0.20 counted, about USD 0.25 charged): Amazon 23 rows, Noon 16, 0 AI failures, two `imported` rows (both from a country of origin attribute) and the rest `unknown`; every stored size and origin was checked against the raw data, and the one wrong volume it found (a 17 oz can sold at a case price, stored as 17 fl oz) now gets no size. On the first run's data the old Selenium rule (2 of title, Volume, Item Volume) would size 9 of 50 Amazon items (junglee has no Volume), the merged F6 rule 25 and the conservative rule 29 (29 on the second run's data too). F6 is pulled onto the instance only after `f6-smoke-fixes` is merged.

1. **Choose the actor** for Amazon KSA and for Noon KSA (research of 2026-11-16; result in section 5).
2. **Calibration run** (with OK for the spend): 1 Pets term, about 20 results and a cap of USD 0.50 per actor. It serves to fix the field mapping (price, size, brand, seller, origin) and the real cost. Done on 2026-11-16 (section 5).
3. **Client and adapters** (Mafer's answers of 2026-11-22: questions 5, 6 and 13):
   - `scraper/services/apify_client.py`: REST client on stdlib `urllib` (token only in the `Authorization: Bearer` header), retries on HTTP 429/5xx and network errors (a run start only on 429), polling with `waitForFinish`, dataset pagination, per-run caps (`maxItems`, `maxTotalChargeUsd`, `timeout`, `memory`) and a **process-wide budget of USD 20** (`config.APIFY_RUN_BUDGET_USD`): a run only starts if the spend so far plus its cap fits. A run that ends FAILED/TIMED-OUT/ABORTED is started once more. The token (`APIFY_API_TOKEN`) is read once, through `pi_secrets`, on the first scrape; a preflight (`GET /users/me/limits`) runs before scraping.
   - `scraper/scrapers/apify_common.py`: the adapters' own parsing (volume, count, pack multiplier, price, currency) with the parser bugs 1–4, 10 and 11 fixed here only (a dimension such as "40 x 40 cm" or a breakdown such as "1.5 L (3 x 500 ml)" never multiplies), the Wayakit exclusion (our own brand), the `origin` rule and the shared step "size gate → one relevance batch (`filter_relevant`) → wipes units batch".
   - `scraper/scrapers/amazon_apify.py` (`AmazonApifyScraper`, site `amazon`): `junglee~amazon-crawler` with `https://www.amazon.sa/s?k=<keyword>` (never `countryCode`/`zipCode`); drops results not loaded from SA, sponsored, without a current price (`price.value`, never `listPrice`), not in SAR, and Wayakit's. Size: at least 2 of {title, Item Volume, Size, Unit Count} must agree on the pack total, else the title's size (ml, liters or fl oz only) when no attribute contradicts it (conservative, 2026-11-23); an ambiguous title (a kit of different products, a count, a dose...) or a solid drops the row; a plain oz needs a confirmation that is not another plain oz; per-unit attributes never outvote a title that states a pack, and a ml attribute is read as fl oz only when the title states that number in fl oz. `scraper/scrapers/amazon_scraper.py` (Selenium) is deleted.
   - `scraper/scrapers/noon_apify.py` (`NoonApifyScraper`, new site `noon`): `saswave~noon-product-scraper`, one search run per keyword, output cut to 50 rows, current price `sale_price` else `price`, SAR assumed from `/saudi-en/`, size from the title only (ml, liters or fl oz; none for an ambiguous title or a plain oz / gallon alone); no details run (the 2026-11-22 details run for `shipping_banner_details.global` was removed on 2026-11-23).
   - `scraper/main.py`: `ApifyFatalError` (token, HTTP 4xx, budget) aborts the run like a fatal relevance-AI error; `ApifyRunFailed` is a per-keyword error in the coverage report, and failed Apify keyword searches above 5 % of those attempted (`config.APIFY_MAX_FAILED_KEYWORD_RATE = 0.05`, Mafer, 2026-11-22) make the run exit 1 without replacing the CSV; fewer are logged as a warning. Append mode refuses an output file with a different header.
4. New `seller` and `origin` columns in `CSV_COLUMNS` and `build_row_data()` (15 columns). `origin` is `local`, `imported` or `unknown`, only from the data, in this order: Amazon `Country of Origin` (Saudi Arabia → local, any other → imported), the text "Made in KSA" / "Made in Saudi Arabia" / "صنع في السعودية" → local, otherwise unknown (a size in fl oz and Noon `shipping_banner_details.global` were dropped as signals on 2026-11-23). Selenium and Odoo rows get `seller = ''` and `origin = 'unknown'`. The ML ignores both columns (`1a_preprocess_data.py:16` selects columns by name); they are used from F7b.
5. **Regression test:** Mumzworld and SACO with the same keywords, before and after: identical in the 13 original columns; `mumzworld_scraper.py`, `saco_scraper.py` and `scraper/utils.py` byte-identical to the base (`tests/differential/test_scraper_main_differential.py`).

Expected cost: about USD 13.5 per run (Amazon 55 × ~0.20, Noon search 55 × ~0.045), inside the USD 20 cap (the budget counts about 25 % less than the real charge; accepted, see `docs/decisions.md`, 2026-11-23).

### F7a · ML robustness without origin (2 days)

Restructured by Mafer on 2026-11-22 (F7 split into F7a and F7b). Nothing here needs the `origin` column.

- **Robust trimming when n is small** (bug 6): with 2 different prices per type, the p10–p90 trimming leaves the type without competitors (14 of 151 volumetric types in F1). Applies directly to Pets.
- **Robust market range** with a ceiling of `p75 × volume` (median without extreme values, without multipacks or sizes of 5 mL or less).
- **Act on `volume_outside_market`** (today it is only a flag).
- **Size matching for small packs.**
- **Do not upload no-size parent templates** to Odoo.
- Small fixes: price 0, `kg` treated as volumetric (bug 5), repeated SKUs (bug 7), quantities ≤ 0 (bug 8), each with its strict `xfail` test.
- The price never goes below the 1.30 margin floor.
- **Documented decision about the model** (the ticket asks for it): the other improvements (log target, GroupKFold) are left for a phase 2. They move the prices of all lines. The `approved_quote_price` leakage as a feature was already removed in e25e2c9 (`2_train_models.py:160`), together with the clipping to the market range with 3 or more competitors (`3_predicted_prices.py:126-132`).
- Before/after comparison by subindustry: median of the new/old ratio and % of suggestions above the local p75.

### F7b · Origin-based Pets selection (1 day)

- Uses the F6 `seller` and `origin` columns (`local`, `imported`, `unknown`, only from the data; no hand-maintained brand lists).
- **Only in Pets:** if there are at least N local rows per type, those rows are used; if not, the F7a robust market statistic.
- Implementation note: `ml_model/1a_preprocess_data.py:23` must `reindex(..., fill_value='unknown')` before keeping `origin`, because the Odoo competitor CSV (`competitor_products_from_odoo.csv`) has no such column.

### F8 · Final run and Odoo KSA (1 day + review)

1. Full run with `--dry-run`.
2. Puya and Luisa review the Pets list: previous price, new price, local p75 and cost.
3. With their approval, and with the KSA company confirmed, it is uploaded with the safe scheme.

**Order:** F0 → F1 → F2 → (F3 → F4) in parallel with (F5 → F6 → F7a → F7b) → F8 (Mafer, 2026-11-22: F6 → F7a → F7b → F8).

---

## 4. Open questions (for Mafer)

1. ~~Which branch does the instance run?~~ **Correction (F1):** it runs `server-improvement` (e25e2c9), not `main`. See question 11.
2. ~~Remove the 2 stages?~~ Mafer wants a monthly run, but **the keyword review is needed**: the sites do not find the product by its generic name (e.g. on SACO you have to search "car cleaner" instead of "car disinfectant"). Pending: choose the design of the review (proposal in section 6). Since F3 (Mafer, 2026-11-18) the run has no stages, so the review happens outside the run, on the versioned `modifiers_mapping.csv`.
3. ~~Which day?~~ The last Thursday of each month: `cron(50 21 ? * 5L *)` in EventBridge Scheduler. Time 21:50 in CDMX time (`America/Mexico_City`), confirmed by Mafer on 2026-11-17.
4. ~~Secret and company?~~ Only PI uses `prod/wayakit-app`. The Odoo database PI uses is KSA only (Mafer, 2026-11-17).
5. ~~Apify account?~~ VdC's, with a higher-tier plan. ~~USD cap per run?~~ **USD 20 per pipeline run across all Apify actor runs** (Mafer, 2026-11-22); exceeding it aborts the scraping with exit 1, never silently (`config.APIFY_RUN_BUDGET_USD`, F6).
6. ~~Apify scope?~~ **Amazon (Apify) and Noon in Home, Automotive and Pets**; Apify replaces the Selenium Amazon scraper (Mafer, 2026-11-22; `TARGET_MAP` in F6).
7. ~~What counts as "local"?~~ It is not defined by hand (see F6 `origin` and F7b).
8. ~~GitHub admin?~~ Mafer is an admin of the organization. The repo is already in the organization (`Wayakit-Biotechnology-in-Hygiene/wayakit-price-prediction`). Pending: does the deploy need manual approval?
9. ~~Snapshot and start-up?~~ Done on 2026-11-17 (F1): snapshot, SSM access, inventory and data copied to `data/f1_before_2026-11-15/`. The instance runs `server-improvement`, not `main`.
11. ~~Rebase onto `server-improvement` or discard e25e2c9?~~ Resolved 2026-11-17: PR #1 merged `pi-mejoras` into `server-improvement`, and `pi-improvements` branches from it, so e25e2c9 is kept.
10. What do we do with the leftovers in me-south-1 (2 roles and the `wayakit/test/credentials` secret)? They could not be inventoried from here.
12. Should a concentration "3X" in the title ("Detergent 3X concentrate 500 ml") multiply the volume? Today the shared parser gives 1500 ml (finding 12, bug 9). It is an assumption of the tests, not a confirmed rule. Still open for `scraper/utils.py`; the F6 Apify adapters never treat "3X" as a multiplier (the `x` must be followed by a number).
13. ~~Fix the parser bugs 1–4 in `scraper/utils.py` or only in the Apify adapters?~~ **Only in the new Apify adapters' own parsing** (`scraper/scrapers/apify_common.py`); `scraper/utils.py` is unchanged, so Mumzworld and SACO rows do not change (Mafer, 2026-11-22).

---

## 5. Apify actors and switch to Bedrock (research of 2026-11-16)

The information comes from the public Apify Store and from each actor's public API. No actor was run and no token was used. Each recommendation was reviewed separately by a second agent.

### Amazon KSA: `junglee/amazon-crawler` (confirmed)

- **Why this actor:**
  - It is the same actor VdC uses for Amazon MX, and Apify maintains it.
  - In 30 days it had 99.5 % successful runs, about 3,600 users and LIMITED_PERMISSIONS.
  - For amazon.sa it is enough to pass the URL `https://www.amazon.sa/s?k=…`. The changelog has an explicit fix for `.sa`.
- **Fields it returns:**
  - `price{value,currency}`, which is the current price, and `listPrice` separately;
  - `brand`, `seller.name`, `asin` and `url`;
  - `attributes` / `attributesMapped`: Manufacturer, Volume, Unit Count and Country of Origin when Amazon shows them;
  - `categoryPageData.isSponsored` and `loadedCountryCode`.
- **Input:**
  - `categoryOrProductUrls`: one search URL per keyword;
  - `maxItemsPerStartUrl: 50`, `scrapeProductDetails: true`, `language: "en"`;
  - `maxOffers: 0`, `scrapeSellers: false`.
  - **Do not pass `countryCode` or `zipCode`**: they add USD 0.05 per result, about USD 137 per month.
- **Cost:**
  - USD 0.004 per result on the Starter (BRONZE) plan. With or without details it costs the same.
  - 55 keywords × 50 results ≈ **USD 11 per monthly run**.
- **In the converter:**
  - drop sponsored results (`categoryPageData.isSponsored`);
  - validate `loadedCountryCode == "SA"`;
  - normalize the currency, which can arrive as "SAR", "ر.س" or a symbol.

### Noon KSA: `saswave/noon-product-scraper` (Mafer's decision: the same one as VdC, which already works)

- It searches with `search_urls` (`https://www.noon.com/saudi-en/search/?q=…`) and gets the details with `product_urls`, just like VdC.
- It does not return the keyword or the currency. That is why it is run **once per keyword**, and the currency is taken as SAR from the `/saudi-en/` domain.
- Cost: about USD 0.0009 per result, about USD 2.5 per month with search alone.
- What each row contains is confirmed with the calibration run.

Alternative that was evaluated and **is not used**: `memo23/noon-scraper`.

- **What it had going for it:**
  - It searches by keyword with `country: "saudi"`. The default value is UAE, so it must always be passed.
  - It returns `currency`, `salePrice` and `price`; the current price is `salePrice ?? price`.
  - It also returns `brand`, `storeName` (seller), `sku`, `productUrl` and `sourceQuery`, the keyword that produced each row. The VdC actor does not return that last field.
  - With `scrapeDetails: true` it adds `specifications`.
  - In 30 days it had 99.8 % successful runs.
  - Risk: it is a new actor (from September 2026) with no reviews.
- **Input:** `{"searchQueries": ["<keyword>"], "country": "saudi", "maxItems": 50, "scrapeDetails": true}`, with **one run per keyword**, because `maxItems` is a cap for the whole run.
- **Cost:** USD 0.0008 per row plus USD 0.005 per start, about **USD 2.5 per monthly run**.
- **Country of origin:** there is no dedicated field. It can only come inside `specifications`.

### Calibration run (2026-11-16, keyword "pet urine odor remover")

The raw data stayed outside the repo, in the session scratchpad. Cost: about USD 0.08.

**Amazon (`junglee`, 20 results, USD 0.08):**

| What was checked | Result |
|---|---|
| Site country | 20/20 with `loadedCountryCode = SA` |
| Currency | 18/20 in `SAR`. The other 2 came without a price and are dropped |
| Sponsored | 0 |
| Brand and seller | Brand 20/20, seller 18/20 |
| Size | Comes in `Item Volume`, `Unit Count` and `Size`, but **they often disagree with each other** (e.g. "5 Liters" versus "907 Grams"). The current rule that 2 of 3 sources must agree has to be kept, or it must be resolved with the AI model |
| `Country of Origin` | **Only 3 of 20** (Saudi Arabia, China, Germany) |

**Most useful for the Pets diagnosis:**
- The results are dominated by US brands (Simple Solution, Rocco & Roxie, Nature's Miracle). They are sold by import resellers such as **Desertcart SA** and MigaStore, at prices of **140 to 720 SAR for 0.9–5 L**.
- The Wayakit product shows up at 48.83 SAR for 700 mL.
- The seller is a better signal of imported than the country of origin, because it is almost always filled in.
- **Wayakit's own product shows up in the results**: Wayakit brand and seller must be excluded, as VdC already does.

**Noon (`saswave`, search plus 5 details):**

| What was checked | Result |
|---|---|
| Rows | It returned 50 rows even though the cap was 20: the actor delivers the full page |
| Search fields | `price` and `sale_price` (current price = `sale_price` or, if missing, `price`), `brand` (41/50), `store_name` and `sku`/`url` |
| Details | `specifications` empty in 3 of 5. No country of origin |
| Imported | The details include `shipping_banner_details.global` ("global" shipping, i.e. imported). It must be confirmed whether it works as an imported flag |
| Relevance | There are off-topic results (air purifiers, dog pens), so the AI filter is essential |
| Other sellers | Wayakit and SACO also sell on Noon |

**Conclusion for F6–F7b:** the country of origin almost never comes. To separate imported from local, signals that always come are used: the seller (known import resellers, derived from the data), Noon's "global" shipping, fl oz units and the country of origin when it appears. (2026-11-23: Noon's "global" shipping turned out to be the same on every product and is not used, and fl oz is not used either (Mafer); see `docs/decisions.md`.) Everything comes from the data, with no hand-made lists.

**Apify spend:** about **USD 14 per month** (Amazon about USD 11 and Noon about USD 2.5). It goes on the VdC account, with a higher-tier plan.

### Bedrock instead of Gemini

- **Interface:** `RelevanceAgent` keeps `is_relevant()` and `extract_wipes_units()`, so the Selenium scrapers (Mumzworld, SACO, B2B) do not change. `filter_relevant(titles, query)` and `extract_wipes_units_batch(titles)` are added, used only by the Apify adapters.
- **Bedrock call:**
  - Converse in eu-north-1, with `temperature=0`;
  - JSON output forced with `outputConfig` (JSON schema). DeepSeek does not support `toolConfig` in Converse; this was tested in the benchmark;
  - **25 titles per call**;
  - cache by (keyword, title).
- **Models (research of 2026-11-16, read-only on the account).** Mafer prefers a cheap one that is not Claude.

  | Model | Where it runs | USD per million tokens (input / output) | Per month | JSON schema in Converse | Note |
  |---|---|---|---|---|---|
  | **DeepSeek V3.2** (`deepseek.v3.2`) | eu-north-1, on-demand | 0.74 / 2.22 | ~USD 3.9 | Yes | **Chosen as primary** (test of 2026-11-16) |
  | Qwen3 235B A22B 2507 (`qwen.qwen3-235b-a22b-2507-v1:0`) | eu-north-1, on-demand | 0.22 / 0.88 | ~USD 1.2 | Yes | Did not pass the rule: 3 points below Haiku and failed on clear cases |
  | gpt-oss-20b (`openai.gpt-oss-20b-1:0`) | eu-north-1 | 0.07 / 0.30 | ~USD 0.5 | Yes | Lowest-cost option. It reasons and charges for those tokens |
  | Gemma 3 | **Not in eu-north-1** via Converse; only in eu-west-1 | — | — | 12B yes | Discarded for now |
  | Nova Micro/Lite | `eu.*` profile | 0.04–0.07 / 0.15–0.26 | ~USD 0.3 | No | Very cheap; fine-grained rules doubtful |
  | Claude Haiku 4.5 (reference) | `eu.*` profile | 1.10 / 5.50 | ~USD 6.3 | Yes | — |

  - Qwen and DeepSeek run only in Stockholm, do not need Marketplace and already show as `AVAILABLE/AUTHORIZED`. **The first invocation accepts the provider's EULA.**
  - Each has its own quota: 10,000 RPM.
  - Correction: in eu-north-1, Haiku 4.5 also has 10,000 RPM. The 10 RPM that VdC mentions are for eu-west-1.
  - **Benchmark (2026-11-16, approved by Mafer; it accepted the Qwen and DeepSeek EULAs).**
    - Data: 66 unique titles from the calibration (keyword "pet urine odor remover", Amazon and Noon). Claude labeled them with the rules of the current prompt and flagged 7 doubtful cases.
    - Modes: batches of 22, one title per call (20 titles) and 10 hand-built wipes titles with a known count.
    - Decision rule: keep the cheapest one within 2 points or less of Haiku's accuracy, with 100 % valid JSON.

    | Model | Accuracy | Precision | Recall | Valid JSON | Exact wipes | Batch = single title | Batch latency (p50) | Test cost |
    |---|---|---|---|---|---|---|---|---|
    | Haiku 4.5 | 98.5 % | 96.9 % | 100 % | 24/24 | 10/10 | 100 % | 1.3 s | USD 0.024 |
    | **DeepSeek V3.2** | **97.0 %** | **100 %** | 93.5 % | 24/24 | 10/10 | 85 % | 5.2 s | USD 0.010 |
    | Qwen3 235B | 95.5 % | 96.7 % | 93.5 % | 24/24 | 10/10 | 85 % | 4.6 s | USD 0.004 |

    - DeepSeek **passes the rule** (1.5 points below Haiku). Its 2 errors are doubtful cases: a patio remover and a hard-floor one, which it rejected under the specialized-surface rule. **It never accepted an irrelevant product.** That is what matters most, because a false positive contaminates the reference price.
    - Qwen rejected 2 products that were clearly relevant and accepted a generic upholstery cleaner.
    - DeepSeek and Qwen do not always give the same answer in batch as title by title (85 %). That is why it is better to **always use batch mode**, also in B2B (batches of 1 to N).
    - **Decision:** DeepSeek V3.2 as primary. Haiku 4.5 as fallback, only if DeepSeek fails or returns invalid JSON; it is already enabled on the account for VdC.
    - Limitations: 1 keyword, English titles only, labels made by Claude and hand-built wipes. In the first real run a sample must be reviewed.
    - Scripts and raw results: `scratchpad/calib/bakeoff.py` and `bakeoff_results.json` (temporary). The script moves into the repo as `tests/bakeoff_relevance.py` in F2.
  - The first call with a new schema can take up to about 90 s, because it compiles the grammar: a 120 s timeout must be set.
- **Quota:**
  - It is no longer a constraint: the candidate models have 10,000 RPM in eu-north-1. Even so, batches of 25 titles are used to save time and tokens.
  - With batches, Apify drops from about 5,500 calls to about 220. B2B also goes in batches: the titles of each search are grouped, because the model answers differently in batch and title by title.
  - PI's monthly run (last Thursday) does not coincide with VdC's (first Sunday).
- **Infrastructure:**
  - `infra/bedrock_profile.py`: application profiles with the `project=price-intelligence` tag for DeepSeek V3.2 and Haiku 4.5, if Bedrock allows it for DeepSeek (verified in F2). If it does not, the cost is separated by modelId in Cost Explorer;
  - in `instance_role.py`: `bedrock:InvokeModel` only on `deepseek.v3.2`, the Haiku profile and its models.
- **Errors:**
  - retries with backoff on `ThrottlingException`;
  - **fail loudly**: today, if the key is missing, everything comes out "not relevant" and the pipeline continues. If AI failures exceed 5 %, the pipeline must exit with code 1.
- **Cost:** about USD 4 per month with DeepSeek V3.2.
- **After the cutover:** remove `GEMINI_API_KEY` from `prod/wayakit-app` and revoke the key.

---

## 6. Keyword review without blocking the run (proposal of 2026-11-17)

**Status: under review.** Mafer's answer (2026-11-17): "a single source of truth" does not work for her, because new products are added all the time (baby, home, etc.) and how to search for each one on each active site has to be reviewed. The problem is recall: the product does exist on the site (e.g. SACO), but it does not show up with our product's name and has to be searched for another way. She liked the coverage report and asked for a better proposal than the automatic PR: that is v2 below.

**The problem:**
- Each site finds the product with different words. For example, on SACO "car disinfectant" returns nothing and "car cleaner" does.
- Today this is handled like this:
  - `scraper/modifiers_mapping.csv` stores the keyword per site (`site:keyword`, 149 rows; today only for fine, gogreen, officesupply and aerosense);
  - in addition, someone edits `analysis-odoo.csv` between stage 1 and stage 2, which blocks the run until that review is done.
- Edits made in `analysis-odoo.csv` are lost in the next full run if they are not copied to `modifiers_mapping.csv`.

**Proposal v1 (discarded on 2026-11-17):** keywords in a single CSV, retry during the run and a PR with what worked. Mafer rejected it because it reacts late: new products are searched badly in the first run and the review comes afterwards.

**Proposal v2: discover how to search for each product *before* the monthly run.**

1. **One week before (the Thursday before the last Thursday), automatic check.** The instance starts in "discovery" mode and builds the pending list:
   - **only the product types that are new in Odoo** since the previous run (baby, home, etc.). This keeps the monthly cost low (Mafer's decision, 2026-11-17);
   - the product × site pairs that gave 0 to 2 relevant results are **not** reviewed every month: they are only flagged in the coverage report.
   - **Full review every 6 months:** all products in the mapping, on all active sites, with the same process. It covers changes in the sites' vocabulary and the cases flagged in the report.
2. **Search candidates from three sources,** for each product × active site:
   - **the site's own vocabulary:** search-box suggestions (autocomplete) and category names of similar products already found. It is how the site names things; e.g. SACO says "car cleaner";
   - **DeepSeek:** synonyms in English and Arabic, based on the product name and description in Odoo;
   - **what already worked** on that site for similar products (memory from previous months).
3. **They are actually tested.** Each candidate is searched on the site (first page only, which is cheap) and the results that DeepSeek judges equivalent to our product are counted. The one that brings the most wins.
4. **Review sheet** (only with the pending items; few are expected per month). For each product × site it shows the current keyword, the winner, how many relevant results it brought and 3 examples with links. The reviewer approves, changes the keyword or marks "not on this site".
5. **On the day of the run:**
   - what was approved is used;
   - if nobody reviewed, the automatic winner if it brought 3 or more relevant results; otherwise, the usual keyword;
   - everything is flagged in the coverage report, which feeds the following month's check.
6. **Keywords are stored per product × site,** with history and who approved. Today that is `modifiers_mapping.csv`, but nobody edits it by hand any more: it is filled from the review sheet.

**Where to review (to decide with Mafer):**
- **(a) GitHub:** the sheet as a PR or as a CSV. Free and immediate, but only convenient for IT.
- **(b) Shared sheet:** Google Sheets or similar. Convenient for Sales; needs a service account and a secret.
- **(c) Odoo:** a "keywords per site" field on the product type, where the people who create products already work. It is the best long-term option, but requires developing a module.

Mafer prefers Odoo (2026-11-17). Before designing it, the Odoo `price_intelligence` module (repo `wayakit/wayakit`) is studied.

**Phases:** coverage report (step 1) in F2 · discovery and candidate testing (2–3) in F6, together with Apify · review sheet and "discovery" mode (4–5) in F4. Estimated additional effort: about 3–4 days. Part of this is outside the scope of ticket A, so a separate ticket is advisable.

---

## 7. Odoo: search keywords and link with products (proposal of 2026-11-17)

It comes from the study of the `price_intelligence` module (repo `wayakit/wayakit`, branch `mainTest`; below, `PIM/`), with a design reviewed by a second agent against the code. Until Mafer approves it, it is only a proposal: Odoo was not touched.

### 7.1 How it is today

**Taxonomy**
- Industry → Sub-industry → **Type of product** (`product.classification`).
- The type name is the only unique key (`PIM/models/product_classification.py:49-51`).
- The Sub industry, Industry and Generic product type columns that PI reads are derived from the type. Generic product type is free text (`:21`).

**`product.master`, the PI SKU.** It lives separately from the Odoo products and is linked in three different ways:

| Link | Where it is used | Problem |
|---|---|---|
| `product_id` (Char) = SKU = `default_code` | PI, Financials tab (`wayakit_customization/models/sale_order_financials.py:38-56`), `product.price.suggestion.product_id_str` | **It is not unique**: there are 6 repeated SKUs (12 of 1,927 masters, according to the F1 data). Since it is text, if the SKU changes on one side, it breaks |
| `external_id` (Char, xml-id of `product.template`) | Only the e-commerce price and the name search (`PIM/models/product_master.py:151-191`, `:255-360`, `:802`) | Resolved by hand; if the xml-id changes, it fails silently |
| `uploaded_in_odoo` (manual Bool) | Informational | Nobody maintains it; it can be computed |

- `product.price.suggestion` has no relation either: everything is text (`PIM/models/product_price_suggestion.py:5-56`).
- Only `competitor.product` has `company_id`.

**Keyword granularity:** **per product type × site**, not per SKU.
- There are 1,921 SKUs in 410 types (median: 4 SKUs per type).
- Adding a SKU of a type that already exists does not require new keywords. Only a **new type** needs them.

### 7.2 Proposal A: keywords in Odoo

**New models in `PIM/`**
- **`pi.search.site`:** replaces `TARGET_MAP`. Fields: `code` (amazon, saco…), `name`, `active` and an M2M relation with `product.subindustry`.
  - Since it links by id, the "Land Transportation" case bug disappears.
  - The 7 initial sites are loaded as `noupdate` data.
- **`pi.search.keyword`:** one row per type × site × keyword.
  - **Status:** `proposed`, `approved`, `rejected`, `not_on_site` or `legacy_base`.
  - **Origin:** `manual`, `discovery` or `csv_import`.
  - **Measurement:** `relevant_count`, examples as text ("title — URL") and `last_discovery`, which **is what drives the six-monthly sweep**.
  - The keyword is stored **as is** (without lowercasing it, because that changes today's searches) and is deduplicated case-insensitively.

**On the product type**
- A **"Search Keywords"** tab on the form.
- A **pending / complete** badge in the list, with a "Pending keywords" filter.

**"Price Intelligence > Keywords to review" menu**
- It is an editable list of proposals: the keyword is corrected in the same row and approved, rejected or marked "Not on site" with one click.
- Discovery leaves **one activity per type** for the configured reviewer.

**Filling it in at creation is not mandatory.** Whoever creates a type can write keywords if they already know how it is searched. If not, discovery proposes them. That way Sales is not blocked.

**What is "new" in the monthly discovery**
- A type with no keyword at all.
- A newly activated site.

So that the migration does not trigger a full sweep in the first month, a base row is created for each active pair: `approved` with the base name in retail, and `legacy_base` in B2B without a keyword.

**Integration with PI**
- **Read:** PI reads sites, types and keywords over XML-RPC and builds the same `analysis-odoo.csv`. That retires `modifiers_mapping.csv` and the manual editing.
- **Write:** PI only writes proposals, through a server method that never approves or deletes.
- **If Odoo does not respond, PI exits with code 1.**

**Migration:** a script exports the 213 current keywords from the CSV as an import file. Mafer imports it first in staging and then in production.

**Effort:** about 3–4 days (module 1.5–2 days, migration 0.5, PI reads from Odoo 1). Discovery is already counted in section 6.

### 7.3 Proposal B: clean link between `product.master` and the Odoo products

- **New field `product_tmpl_id`** (M2O to `product.template`) on `product.master`:
  - filled automatically by SKU (`default_code`) with a backfill;
  - also filled when the product is created in Odoo after the master, which is the norm today (`uploaded_in_odoo`).
  - Masters that do not find their product end up in an "Unlinked" filter.
- **`uploaded_in_odoo` becomes computed** (`bool(product_tmpl_id)`).
- **`external_id` is retired:** the e-commerce price and the search go through the M2O. Before that, it is validated that the backfill covers everyone who has an `external_id` today.
- **Unique SKU:**
  - first a report of the 6 repeated SKUs so that Mafer decides which one stays;
  - then `unique(product_id)`.
- **`product.price.suggestion` gains `master_id`** (M2O). PI writes it and `product_id_str` is kept for compatibility. The Financials tab can keep using the SKU or move to the M2O.
- **`product.master` is not merged with `product.template`.** The master has costs, tiers and products that do not exist in Odoo yet. The right thing is to link them, not mix them.

**Effort:** about 2 days, plus the cleanup of duplicates by Mafer.

### 7.4 Where each piece lives, and risks

- **Module code:** `wayakit/wayakit`, branch `mainTest` → staging on odoo.sh → `main`.
- **Data** (reviewer, groups, import, SKU cleanup): in production, by hand by Mafer.
- **PI changes:** this branch.

**Risks**
- Bump the module version and test the upgrade in staging before production, in a window with no PI run.
- The Viewer needs read access on the new models.
- The API user's permissions are still unverified.
- Mumzworld and SACO change where they read their configuration from, so the F6 regression test is needed.
- When Noon is activated, about 120 pairs are discovered at once (Apify cost).

**Tickets opened on 2026-11-17** (approved by Mafer):
- [Price Intelligence in Odoo: search keywords per site](https://app.notion.com/p/41bbe65f8c1381dd8f54c3b0077953c3) (proposal A): estimate 8, due 2026-12-10.
- [Price Intelligence in Odoo: link product.master with products](https://app.notion.com/p/41bbe65f8c1381248fd3fbb2a177d536) (proposal B): estimate 5, due 2026-12-15.

The dates fall after tickets A and B, because the November weeks are already full.
