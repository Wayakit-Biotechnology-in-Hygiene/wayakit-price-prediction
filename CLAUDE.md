# CLAUDE.md — Price Intelligence

Context for Claude Code in this repo. Always reply to the user (Mafer) in Spanish; everything written in the repository is in English.

## What it is

Wayakit project that suggests selling prices for the Saudi Arabia (KSA) products based on competitor prices. Today: Selenium scrapers (Amazon, Mumzworld, SACO and B2B stores) and Gemini to decide relevance → `scraper/competitors_complete.csv` → `ml_model/` (price per liter or per piece, p10–p90 trimming, RandomForest) → `product.price.suggestion` in Odoo. It runs on the EC2 `wayakit-app-server` (eu-north-1, almost always stopped): two EventBridge schedules in the `default` group start it on Thursday and Friday at 21:50 (CDMX), the `ec2-user` crontab launches `run_pipeline.sh` on Friday 04:00 UTC (stage 1) and `run_pipeline.sh --stage 2` on Saturday 04:00 UTC (Thursday and Friday 22:00 CDMX), and the `trap EXIT` stops it when it finishes. The instance runs the **`server-improvement`** branch (e25e2c9), not `main`. Access through SSM (no SSH).

Documentation: [docs/runbook.md](docs/runbook.md) (how to operate it and known issues), [docs/resources.md](docs/resources.md) (what exists), [docs/decisions.md](docs/decisions.md) (why).

## Status

Not maintained here. Phases, findings and open questions: [docs/plan.md](docs/plan.md). What is still to be decided: [docs/decisions.md#pending](docs/decisions.md#pending). Nothing new has been created in AWS for PI yet; anything marked "Planned (F#)" does not exist.

## Rules that are never broken

- **No credentials** in code, repo, logs or chat; no account ID either (use `<account>`), emails, IPs or instance IDs. Never print the value of a secret, not even to verify it. Check `git diff --cached` for keys before every commit.
- **Secrets only in Secrets Manager `prod/wayakit-app`** (the code reads the keys `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_API_TOKEN` and `GEMINI_API_KEY`). Read them from a single module: Planned (F2) `pi_secrets.py`. Today there are 6 copies of `get_secret()`; do not add another one: `ml_model/odoo_api_products.py:14`, `ml_model/odoo_api_cotizations.py:15`, `ml_model/odoo_api_competitor_products.py:17`, `ml_model/odoo_api_price_suggestion.py:21`, `scraper/odoo_api_connection_products.py:17`, `scraper/services/ai_service.py:12`.
- **SSO profile `wayakit-vdc`**, never `default`. **Region `eu-north-1`** for everything.
- **Tag `project=price-intelligence`** on every new resource.
- **Ask Mafer for confirmation before:** creating resources in AWS, accepting model agreements or EULAs, requesting quotas, or writing prices to Odoo (running `odoo_api_price_suggestion.py`).
- **Odoo: confirm the company (KSA or MX) before reading or writing.** Today no query filters by `company_id`; KSA is assumed because everything is in SAR, but the code does not enforce it.
- **Mumzworld and SACO are not changed** (ticket A). Any shared change (`config.py`, `main.py`) needs the F6 regression test.
- **Do not add CSVs with data to the repo.** `.gitignore` excludes `*.csv` and `scraper/.gitignore` re-includes `modifiers_mapping.csv`, `analysis-odoo.csv` and `analysis-odoo_partial.csv`. Today `scraper/modifiers_mapping.csv`, `scraper/analysis-odoo.csv` and `ml_model/wayakit_products_competition.csv` are versioned (the last one holds historical competitor prices); do not add others.
- **Docs are updated in the same commit** as the code or infrastructure change.
- **`origin` (local / imported; Planned column, F6–F7) comes only from the data** (seller, Noon "global" shipping, fl oz, country of origin when present). No hand-maintained brand lists (Mafer's decision).

## Code map

- **`run_pipeline.sh`.** Stage 1 (`:65-109`): always full since e25e2c9 (`IS_FULL_RUN="true"`, `:68`; partial mode was disabled and its branch, `:95-100`, is no longer reached), reads products from Odoo (`:86`) and generates `scraper/analysis-odoo.csv` for manual review (`:90-93`). Stage 2 (`--stage 2`, `:116-215`): `git pull` (`:133`), scraping with `timeout 30h` (`:154`), Odoo reads (`:155-156`), ML and upload (`:163-167`; the partial branch, `:172-202`, is still in the code but is not reached).
- **Scraper contract:** `__init__(driver_path, relevance_agent)` and `scrape(keyword, search_mode) -> list[dict]`, with `search_mode` in `{'volume', 'units'}` (exception: AeroSense does not receive `relevance_agent`). Each dict has `Product`, `Price_SAR` (full package), `Company`, `URL`, `Unit of measurement` (`ml`, `L`, `fl oz`, `units`) and `Total quantity` (pack total, > 0). Registered in `scraper/main.py:282-290` and in `TARGET_MAP` (`scraper/config.py:9-24`).
- **`competitors_complete.csv`** (`scraper/config.py:3-7`): `date, industry, subindustry, type_of_product, generic_product_type, product, price_sar, company, source, url, unit_of_measurement, total_quantity, channel`. Built in `scraper/main.py:368-383`.
- **Price per liter or per piece:** `ml_model/1a_preprocess_data.py:47-70`, not in the scraper.
- **Model** (`ml_model/2_train_models.py:160-169`): two RandomForests (volumetric and units; 300 trees, `min_samples_leaf=1`, 80/20 holdout metrics in the log, `:62-80`) with `volume_liters` or `total_quantity` plus `type_of_product`, `subindustry`, `channel`, `generic_product_type`, `industry` and `company` (the brand on Amazon, Mumzworld and SACO). It no longer uses `approved_quote_price` as a feature (it is still the target for the Wayakit rows). It does not use the source. The market range (`*_market_stats.joblib`) comes only from competitors (`:177-178`). In prediction, with 3 or more competitors, the price is clipped to `market_min`–`market_max` (`ml_model/3_predicted_prices.py:126-132`) and then the margin floor `cost × 1.30` is applied (`:135-136`), which always wins.
- **Odoo** (XML-RPC). Reads `product.master`, `sale.order.line` and `competitor.product`. Writes `product.price.suggestion`: in full mode it copies to `product.price.suggestion.history`, does an `unlink` of everything and then a `create` (`ml_model/odoo_api_price_suggestion.py:110-182`). If the `create` fails, the table is left empty and the script exits with 0.

## Known pitfalls

- **Silent failures:** `exit()` exits with 0 (`odoo_api_price_suggestion.py:52,56,65,78,109`, also in `odoo_api_products.py` and `odoo_api_cotizations.py`); `1a_preprocess_data.py:87-90` swallows exceptions; the stage 2 steps have no `|| exit 1`. The pipeline can log `Success` and upload stale data.
- **`git pull` in stage 2** (`run_pipeline.sh:133`) does not check the result and conflicts with `analysis-odoo.csv`, which stage 1 rewrites.
- **Case-sensitive subindustry:** `TARGET_MAP.get` (`main.py:333`) uses `'Land Transportation'` and the data has `'Land transportation'`; those rows are never scraped and there is no warning.
- **New CSV column = `config.py` + `main.py` at the same time:** `csv.DictWriter` (`main.py:255`) uses the default `extrasaction='raise'`.
- **Sales fixed to 2025** (`ml_model/odoo_api_cotizations.py:75-76`): 2026 sales do not enter training.
- **Without an API key, `is_relevant` returns `False`** for everything (`scraper/services/ai_service.py:138-139`): the run ends with no products (except AeroSense, which does not use the AI) and without an error.
- **The Gemini key goes in the URL** (`ai_service.py:54-55`) and **is in plain text** in `logs/cron.log` and `logs/pipeline_execution.log` on the instance (confirmed in F1). Do not copy those logs anywhere or print them without redacting `key=`.
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

- **Today:** Gemini over HTTP (`gemini-2.5-flash-lite` for relevance, `gemini-2.5-pro` to count wipes units), one title per call.
- **Planned (F2):** Bedrock with the Converse API in eu-north-1: **DeepSeek V3.2** (`deepseek.v3.2`) as primary and **Claude Haiku 4.5** as fallback if it fails or returns invalid JSON. `temperature=0`, JSON enforced with `outputConfig` (JSON schema; DeepSeek does not support `toolConfig` in Converse), batches of 25 titles, 120 s timeout (the first call with a new schema compiles the grammar). `RelevanceAgent` keeps `is_relevant()` and `extract_wipes_units()`; if failures exceed 5 %, exit with code 1. When done, revoke the Gemini key and remove it from `prod/wayakit-app`.

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

Local: conda, environment **`wayakit-price-prediction`** (`conda env create -f environment.yml`; for the tests, also `pip install -r requirements-dev.txt`). On the instance: conda, environment `wayakit_env` (hardcoded in `run_pipeline.sh:6`) until F3; if it has to be recreated there, `conda env create -f environment.yml -n wayakit_env` (without `-n` it would create `wayakit-price-prediction` and `run_pipeline.sh` would not find it). `requirements.txt` does not pin versions; on the instance: Python 3.10.20, conda in `/home/ec2-user/miniconda`, Chrome 146. [environment.yml](environment.yml) pins the instance versions (`pip freeze`, 2026-11-17); for Bedrock with `outputConfig`, boto3 may need to be upgraded in F2 (tested locally with 1.43.115). Data from the last run for diagnosis: `data/f1_before_2026-11-15/` (ignored by git). The scripts use relative paths: run them from the repo root.

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

Today there is no CI/CD: the code arrives through the stage 2 `git pull`. Planned: GitHub Actions (OIDC, no keys) publishes `releases/<sha>.tar.gz` to S3, moves the `current` pointer and starts the instance in "deploy only" mode (pull-on-boot, atomic symlink). Rollback with `workflow_dispatch` and a `sha`. Details in [docs/runbook.md](docs/runbook.md).
