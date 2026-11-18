# CLAUDE.md — Price Intelligence

Context for Claude Code in this repo. Always reply to the user (Mafer) in Spanish; everything written in the repository is in English.

## What it is

Wayakit project that suggests selling prices for the Saudi Arabia (KSA) products based on competitor prices. Today: Selenium scrapers (Amazon, Mumzworld, SACO and B2B stores) and Bedrock (DeepSeek V3.2, Haiku 4.5 fallback; since F2 on branch `f2-hardening`, Gemini before) to decide relevance → `scraper/competitors_complete.csv` → `ml_model/` (price per liter or per piece, p10–p90 trimming, RandomForest) → `product.price.suggestion` in Odoo. It runs on the EC2 `wayakit-app-server` (eu-north-1, almost always stopped): two EventBridge schedules in the `default` group start it on Thursday and Friday at 21:50 (CDMX), the `ec2-user` crontab launches `run_pipeline.sh` on Friday 04:00 UTC (stage 1) and `run_pipeline.sh --stage 2` on Saturday 04:00 UTC (Thursday and Friday 22:00 CDMX), and the `trap EXIT` stops it when it finishes. The instance runs the **`server-improvement`** branch (e25e2c9), not `main`. Access through SSM (no SSH). **F2 must not reach the instance before F3** grants `bedrock:InvokeModel` to the instance role (stage 2 would stop at the Bedrock preflight): Mafer decided (2026-11-18) to merge `f2-hardening` into `server-improvement` only after F3.

Documentation: [docs/runbook.md](docs/runbook.md) (how to operate it and known issues), [docs/resources.md](docs/resources.md) (what exists), [docs/decisions.md](docs/decisions.md) (why).

## Status

Not maintained here. Phases, findings and open questions: [docs/plan.md](docs/plan.md). What is still to be decided: [docs/decisions.md#pending](docs/decisions.md#pending). Nothing new has been created in AWS for PI yet; anything marked "Planned (F#)" does not exist.

## Rules that are never broken

- **No credentials** in code, repo, logs or chat; no account ID either (use `<account>`), emails, IPs or instance IDs. Never print the value of a secret, not even to verify it. Check `git diff --cached` for keys before every commit.
- **Secrets only in Secrets Manager `prod/wayakit-app`** (the code reads the keys `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME` and `ODOO_API_TOKEN`; `GEMINI_API_KEY` is no longer read since F2 and must be revoked and removed). Read them **only** through `pi_secrets.py` (`get_secret`, `get_odoo_credentials`); Odoo connections go through `pi_odoo.py`. Do not add another copy of `get_secret()`. Bedrock needs no secret (instance role).
- **SSO profile `wayakit-vdc`**, never `default`. **Region `eu-north-1`** for everything.
- **Tag `project=price-intelligence`** on every new resource.
- **Ask Mafer for confirmation before:** creating resources in AWS, accepting model agreements or EULAs, requesting quotas, or writing prices to Odoo (running `odoo_api_price_suggestion.py`).
- **Odoo: confirm the company (KSA or MX) before reading or writing.** Today no query filters by `company_id`; KSA is assumed because everything is in SAR, but the code does not enforce it.
- **Mumzworld and SACO are not changed** (ticket A). Any shared change (`config.py`, `main.py`) needs the F6 regression test.
- **Do not add CSVs with data to the repo.** `.gitignore` excludes `*.csv` and `scraper/.gitignore` re-includes `modifiers_mapping.csv`, `analysis-odoo.csv` and `analysis-odoo_partial.csv`. Today `scraper/modifiers_mapping.csv`, `scraper/analysis-odoo.csv` and `ml_model/wayakit_products_competition.csv` are versioned (the last one holds historical competitor prices); do not add others.
- **Docs are updated in the same commit** as the code or infrastructure change.
- **`origin` (local / imported; Planned column, F6–F7) comes only from the data** (seller, Noon "global" shipping, fl oz, country of origin when present). No hand-maintained brand lists (Mafer's decision).

## Code map

- **`run_pipeline.sh`.** Stage 1 (`:65-110`): always full since e25e2c9 (`IS_FULL_RUN="true"`; the partial branch is no longer reached), reads products from Odoo (`:86`) and generates `scraper/analysis-odoo.csv` for manual review (`:90`). Stage 2 (`--stage 2`, `:116-221`): no `git pull` since F2, scraping with `timeout 30h` (`:154`), Odoo reads (`:155-156`), ML and upload (`:163-167`; the partial branch, `:172-205`, is still in the code but is not reached). Every step carries `|| exit 1` (F2).
- **Scraper contract:** `__init__(driver_path, relevance_agent)` and `scrape(keyword, search_mode) -> list[dict]`, with `search_mode` in `{'volume', 'units'}` (exception: AeroSense does not receive `relevance_agent`). Each dict has `Product`, `Price_SAR` (full package), `Company`, `URL`, `Unit of measurement` (`ml`, `L`, `fl oz`, `units`) and `Total quantity` (pack total, > 0). Registered in `make_scrapers()` (`scraper/main.py`) and in `TARGET_MAP` (`scraper/config.py:9-24`).
- **`competitors_complete.csv`** (`scraper/config.py:3-7`): `date, industry, subindustry, type_of_product, generic_product_type, product, price_sar, company, source, url, unit_of_measurement, total_quantity, channel`. Built in `build_row_data()` (`scraper/main.py`).
- **Price per liter or per piece:** `ml_model/1a_preprocess_data.py:47-70`, not in the scraper.
- **Model** (`ml_model/2_train_models.py:160-169`): two RandomForests (volumetric and units; 300 trees, `min_samples_leaf=1`, 80/20 holdout metrics in the log, `:62-80`) with `volume_liters` or `total_quantity` plus `type_of_product`, `subindustry`, `channel`, `generic_product_type`, `industry` and `company` (the brand on Amazon, Mumzworld and SACO). It no longer uses `approved_quote_price` as a feature (it is still the target for the Wayakit rows). It does not use the source. The market range (`*_market_stats.joblib`) comes only from competitors (`:177-178`). In prediction, with 3 or more competitors, the price is clipped to `market_min`–`market_max` (`ml_model/3_predicted_prices.py:126-132`) and then the margin floor `cost × 1.30` is applied (`:135-136`), which always wins.
- **Odoo** (XML-RPC). Reads `product.master`, `sale.order.line` and `competitor.product`. Writes `product.price.suggestion` (`ml_model/odoo_api_price_suggestion.py`, `upload()`): since F2 it checks there is exactly one company, saves the current IDs to `logs/odoo_previous_suggestion_ids.json`, `create`s the new batch and only then archives to `product.price.suggestion.history` and `unlink`s the saved IDs; any failure exits 1 and a failed cleanup is retried with `--cleanup-previous`. `--dry-run` writes the records to a CSV and reads no secrets. Sales come from the last 12 months (`sales_window()` in `ml_model/odoo_api_cotizations.py`). Importing the Odoo scripts has no side effects: each has `main(argv, session=None)`.
- **Relevance AI** (`scraper/services/ai_service.py`, `RelevanceAgent`) and **scraper main** (`scraper/main.py`): see "Relevance AI" below. `main.py` writes to `competitors_complete.tmp.csv` and renames it only on success, exits 1 on fatal errors, and always writes `scraper/coverage_report.csv` (found vs relevant per subindustry × type × site × keyword).

## Known pitfalls

- **Silent failures (fixed in F2 on `f2-hardening`, still live on the instance until it is deployed):** bare `exit()`, swallowed exceptions and stage 2 steps without `|| exit 1`. On the branch every error exits 1; do not reintroduce `exit()` or an `except` that only logs.
- **`git pull` in stage 2** is still on the instance (old `run_pipeline.sh`) and can pull new code of `server-improvement` on its own; F2 removes it. Until F4, code reaches the instance with a manual pull over SSM (how it authenticates is still to be decided).
- **Case-sensitive subindustry:** `TARGET_MAP.get` in `sites_for()` (`scraper/main.py`) uses `'Land Transportation'` and the data has `'Land transportation'`; those rows are never scraped and there is no warning.
- **New CSV column = `config.py` + `main.py` at the same time:** `csv.DictWriter` (`_run()` in `scraper/main.py`) uses the default `extrasaction='raise'`.
- **Odoo reads with 0 records exit 1** (F2): products, sales (rolling 12 months) and competitor products. An empty read would otherwise leave the previous run's CSV in use.
- **The Gemini key is in plain text** in `logs/cron.log` and `logs/pipeline_execution.log` on the instance (confirmed in F1; the old `ai_service.py` put it in the URL). Do not copy those logs anywhere or print them without redacting `key=`. Bedrock errors are logged with the model, exception class and AWS error code only (AWS messages can carry ARNs with the account ID).
- **`log_config.LOG_DIR` is `<repo>/../logs`**, while `run_pipeline.sh` and the upload's IDs file use `<repo>/logs` (Pending 17).
- **A failing step does not stop the pipeline:** in the 13–15 Nov 2026 run, scraping hit the 30 h timeout with 53 rows and training and the Odoo upload in full mode still happened.
- **Inconsistent sizes in Apify:** in `junglee`, `Item Volume`, `Unit Count` and `Size` often do not match; keep the 2-out-of-3-sources rule.
- **Wayakit products show up in the results** (Amazon and Noon). Today nothing excludes them; the Apify adapters must discard the Wayakit brand and seller.

## `infra/` conventions — Planned (F3)

- One script per resource, with boto3. Each one can be run more than once (if the resource exists, it updates it; it never duplicates it), accepts `--dry-run` and has a docstring in English that says what it leaves configured and how to use it.
- Shared configuration in `infra/config.py` (profile, region, tag, names, cron). Do not duplicate constants.
- Do not store the account ID or emails in the repo: the ID is obtained with STS and the email is passed as an argument.
- Do not name scripts after standard library modules (`secrets.py` broke boto3 in VdC).
- When adding a resource: new script + row in `docs/resources.md` + step in `docs/runbook.md` + decision in `docs/decisions.md` if applicable.

## Relevance AI

- **F2 (branch `f2-hardening`):** Bedrock with the Converse API in eu-north-1: **DeepSeek V3.2** (`deepseek.v3.2`) as primary and **Claude Haiku 4.5** (`eu.anthropic.claude-haiku-4-5-20251001-v1:0`) as fallback when it fails or returns invalid JSON; override with `PI_RELEVANCE_PRIMARY_MODEL` / `PI_RELEVANCE_FALLBACK_MODEL` (F3 application profiles). `temperature=0`, JSON enforced with `outputConfig` (JSON schema; never `toolConfig`), batches of up to 25 titles (`filter_relevant`, `extract_wipes_units_batch`; `is_relevant()` and `extract_wipes_units()` are batches of 1 through the same path, so the Selenium scrapers do not change), 120 s read timeout, backoff on throttling, cache by (query, title). Credential or permission errors raise at once; `preflight()` runs before scraping; AI failures above 5 % make `scraper/main.py` exit 1 before replacing the CSV. Tests only with a fake client (`tests/unit/_fake_bedrock.py`).
- **Pending:** F3 grants `bedrock:InvokeModel` (DeepSeek and the Haiku profile and models) to the instance role; then revoke the Gemini key and remove it from `prod/wayakit-app`.

## Apify — Planned (F6)

- Amazon KSA: `junglee/amazon-crawler` with the URL `https://www.amazon.sa/s?k=…`. **Do not pass `countryCode` or `zipCode`** (they charge USD 0.05 extra per result).
- Noon KSA: `saswave/noon-product-scraper`, **one run per keyword** (it does not return the keyword or the currency; SAR is assumed from `/saudi-en/`).
- Account: the VdC one. Today the only token is in `voz-del-cliente/api-keys` (VdC's); PI must not read that secret. Where the PI token lives is still to be decided: the plan proposes a new key in `prod/wayakit-app` (question 4 in `docs/plan.md`).

## How development works (team and verification)

- **Team:** for a ticket with several parts, the main session is the lead (the only one that commits) and directs `pi-dev`, `pi-tester` and `pi-reviewer` (`.claude/agents/`). Manual: skill [`team`](.claude/skills/team/SKILL.md), with the table of who owns each file.
- **Verification:** skill [`verify`](.claude/skills/verify/SKILL.md) and `scripts/verify.sh quick|full` (unit, property, e2e, differential; no network). `full` before every commit that touches `ml_model/`, `scraper/` or `run_pipeline.sh`.
- **Review rules:** [REVIEW.md](REVIEW.md) and [docs/reliability-checklist.md](docs/reliability-checklist.md), read **from the base branch** (`git show "${PI_BASE_REF:-origin/server-improvement}:REVIEW.md"`). Checks go in a PR separate from the code; `scripts/hooks/protect_checks.sh` blocks them unless `PI_EDIT_CHECKS=1` is set (only the lead, in a checks-only change).
- **3 rounds:** if the same check keeps failing (or the same Important finding stays open) after 3 attempts, stop and escalate to Mafer with the error, what was tried and the decision that is needed.
- **Commit stamp:** trailer `Verified: <quick|full|none> (<detail>)`. The `commit-msg` hook (`scripts/install_githooks.sh`, once per clone) validates it against `.claude/state/last_verification.json` for the same tree.

## Environment

Local: conda, environment **`wayakit-price-prediction`** (`conda env create -f environment.yml`; for the tests, also `pip install -r requirements-dev.txt`). On the instance: conda, environment `wayakit_env` (hardcoded in `run_pipeline.sh:6`) until F3; if it has to be recreated there, `conda env create -f environment.yml -n wayakit_env` (without `-n` it would create `wayakit-price-prediction` and `run_pipeline.sh` would not find it). `requirements.txt` does not pin versions; on the instance: Python 3.10.20, conda in `/home/ec2-user/miniconda`, Chrome 146. [environment.yml](environment.yml) pins the instance versions (`pip freeze`, 2026-11-17); boto3/botocore 1.42.83 already support `outputConfig` in Converse (checked in F2), no upgrade needed. Data from the last run for diagnosis: `data/f1_before_2026-11-15/` (ignored by git). The scripts use relative paths: run them from the repo root.

```bash
python ml_model/odoo_api_products.py                     # reads Odoo → ml_model/wayakit_products.csv
python scraper/odoo_api_connection_products.py --input_odoo_products_file ml_model/wayakit_products.csv --output_analysis_file scraper/analysis-odoo.csv
python scraper/main.py --analysis_file scraper/analysis-odoo.csv --output_mode overwrite --output_file scraper/competitors_complete.csv
python ml_model/odoo_api_cotizations.py && python ml_model/odoo_api_competitor_products.py
python ml_model/1a_preprocess_data.py && python ml_model/1b_preprocess_data.py --run_mode full
python ml_model/2_train_models.py && python ml_model/3_predicted_prices.py
python ml_model/odoo_api_price_suggestion.py --run_mode full   # WRITES to Odoo: only with Mafer's OK and the company confirmed
```

## Deployment — Planned (F4)

Today there is no CI/CD: on the instance the code still arrives through the old stage 2 `git pull` (removed in F2; after F2 is deployed, a manual pull over SSM until F4). Planned: GitHub Actions (OIDC, no keys) publishes `releases/<sha>.tar.gz` to S3, moves the `current` pointer and starts the instance in "deploy only" mode (pull-on-boot, atomic symlink). Rollback with `workflow_dispatch` and a `sha`. Details in [docs/runbook.md](docs/runbook.md).
