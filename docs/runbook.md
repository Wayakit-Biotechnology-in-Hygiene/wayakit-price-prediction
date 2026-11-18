# Runbook — Price Intelligence

Practical guide to operating Price Intelligence (PI). Each section solves one concrete task.
To know *what* exists in AWS, see [resources.md](resources.md). To know *why* it is the way it is, see [decisions.md](decisions.md). The improvement plan is in [plan.md](plan.md).

Status as of 2026-11-17: the operation described here is **today's** (branch `server-improvement` on the instance, F1 inventory), plus the **F2 changes** (marked "since F2"): they are in the code of this branch but **not yet on the instance**, which still runs the pre-F2 code (see [F2 is not on the instance yet](#f2-is-not-on-the-instance-yet)). Anything marked **Planned (F#)** does not exist yet; the number is the phase in [plan.md](plan.md). Nothing has been created or changed in AWS for the plan.

AWS commands are run from the repo root, on a Mac with AWS CLI v2. Pipeline commands are run inside the EC2 instance.

---

## Before you start

### Once per machine

1. Have a user in IAM Identity Center with access to the Wayakit account.
2. Configure the SSO profile `wayakit-vdc`, same as in VdC (see `voz-del-cliente/docs/runbook.md`, section *Before you start*):

   | Question | Answer |
   |---|---|
   | SSO region | `eu-north-1` |
   | Default client Region | `eu-north-1` (the CLI suggests `us-east-1`; do not accept it) |
   | Profile name | `wayakit-vdc` |

3. Access to the instance is through **SSM** (since 2026-11-17). Install the AWS CLI [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html). The SSH key (`wayakit-stockholm-key`) is no longer needed.
4. **Local environment** (to run tests and develop), from the repo root:
   ```bash
   conda env create -f environment.yml          # creates the local environment "wayakit-price-prediction"
   conda activate wayakit-price-prediction
   pip install -r requirements-dev.txt          # pytest and hypothesis
   scripts/install_githooks.sh                  # commit-msg hook that validates the "Verified:" stamp
   ```
   The local name (`wayakit-price-prediction`) differs from the instance's (`wayakit_env`, until F3). On the instance, never use `conda env create -f environment.yml` without `-n wayakit_env` (see [Running by hand](#running-by-hand), step 4).

### Every day, or when the session expires

```bash
aws sso login --profile wayakit-vdc
aws sts get-caller-identity --profile wayakit-vdc
```

- **Always use `--profile wayakit-vdc`, never the `default` profile**: on the team's Macs, `default` uses long-term access keys.
- Region: **`eu-north-1` (Stockholm)**. In the console, check the region selector before looking for anything. There are leftovers in the previous region, me-south-1 (2 roles and the secret `wayakit/test/credentials`), that could not be inventoried (plan question 10).
- The Odoo and Gemini credentials live in the secret `prod/wayakit-app` (keys `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_API_TOKEN` and `GEMINI_API_KEY`). Never print or copy the values. Since F2 the code reads them only through `pi_secrets.py` and no longer reads `GEMINI_API_KEY` (Bedrock uses the instance role).

---

## How it runs today

### Trigger

| Schedule (EventBridge Scheduler, group `default`) | Expression | Time zone | What it does |
|---|---|---|---|
| `Wayakit-Start-Thursday-Night` | `cron(50 21 ? * 5 *)` | America/Mexico_City | Starts the `wayakit-app-server` instance (Thursday 21:50 CDMX) |
| `Wayakit-Start-Friday-Night` | `cron(50 21 ? * 6 *)` | America/Mexico_City | Starts the instance (Friday 21:50 CDMX) |

- The schedules **only start** the instance (target `ec2:startInstances`, role `Wayakit-EventBridge-Role`). There are no PI Lambdas or EventBridge rules.
- Inside the instance, the **`ec2-user` crontab** (not in the repo; verified in F1, 2026-11-17) launches the stages. The repo is at `/home/ec2-user/wayakit-price-prediction`, branch **`server-improvement`**:
  ```
  0 4 * * 5 /home/ec2-user/wayakit-price-prediction/run_pipeline.sh >> .../logs/cron.log 2>&1
  0 4 * * 6 /home/ec2-user/wayakit-price-prediction/run_pipeline.sh --stage 2 >> .../logs/cron.log 2>&1
  ```
  In UTC: Friday and Saturday 04:00 = **Thursday and Friday 22:00 CDMX**, 10 minutes after the schedules start the instance. There is no `@reboot`, no custom systemd timers and no `rc.local`: starting the instance at another time launches nothing.
- When it finishes, any exit of the script (success or error) fires `trap EXIT`, which schedules `sudo shutdown -h +1` (`run_pipeline.sh:52`). There is no `StopInstances` via API.
- If stage 1 is still running when the Friday schedule fires, that `StartInstances` does nothing (the instance is already running). Since the cron is at a fixed time, stage 2 is launched anyway on Saturday 04:00 UTC even if stage 1 has not finished (both would run at the same time).
- Planned (F3): a single monthly schedule, the last Thursday of each month (`cron(50 21 ? * 5L *)`), in the group `price-intelligence`. At 21:50 CDMX time (Mafer's decision, 2026-11-17).

### Stage 1: prepare the search list (`run_pipeline.sh:65-109`)

1. The run is **always full**: since e25e2c9 partial mode is disabled (`IS_FULL_RUN="true"`, `run_pipeline.sh:67-68`). `last_full_run.txt` (in the [log folder](#files-it-produces)) is still written, but it no longer decides anything.
2. Activates conda (`wayakit_env`) and runs `ml_model/odoo_api_products.py` → `ml_model/wayakit_products.csv` (`run_pipeline.sh:86`).
3. `scraper/odoo_api_connection_products.py` generates `scraper/analysis-odoo.csv` from the Odoo products and `scraper/modifiers_mapping.csv`, and writes `run_mode.txt=full` and `last_full_run.txt` (`run_pipeline.sh:90-93`). Since F2 it does not connect to Odoo or read secrets (it only reads the CSV), and a missing or malformed `modifiers_mapping.csv` is an error (exit 1) instead of a warning with empty modifiers.
4. The partial branch (`ml_model/filter_new_products.py` → `ml_model/wayakit_new_products_temp.csv`, `run_mode.txt=partial`, `run_pipeline.sh:95-100`) is still in the code, but is no longer reached.
5. Exits and the instance shuts down.

In stage 1, every step has `|| exit 1`: if something fails, the stage ends with an error (and shuts down anyway).

### Stage 2: scraping, training and upload to Odoo (`run_pipeline.sh:114-221`)

1. Reads `run_mode.txt` from the [log folder](#files-it-produces); if it does not exist, exits with an error.
2. Since F2 there is **no `git pull`** (before: `run_pipeline.sh:133` of e25e2c9, without checking the result). Code reaches the instance by hand (see [Deploy and rollback](#deploy-and-rollback)).
3. Full (`run_pipeline.sh:146-169`):
   - scrapes with `timeout 30h` (line 154). Since F2 the previous `scraper/competitors_complete.csv` is **not deleted**: `scraper/main.py` writes to `scraper/competitors_complete.tmp.csv` and replaces the CSV only if the run succeeds (see [Scraper: fail loudly and temp file](#scraper-fail-loudly-and-temp-file));
   - reads the quotations (last 12 rolling months) and competitor products from Odoo;
   - runs `1a_preprocess_data.py`, `1b_preprocess_data.py --run_mode full`, `2_train_models.py` and `3_predicted_prices.py`, each with `timeout 1h`;
   - uploads to Odoo with `odoo_api_price_suggestion.py --run_mode full` (line 167; safe order since F2, see [Re-uploading suggestions](#re-uploading-suggestions)).
4. Partial (`run_pipeline.sh:171-212`). Still in the code, but since e25e2c9 stage 1 always writes `run_mode.txt=full`, so it is not reached:
   - if `wayakit_new_products_temp.csv` does not exist, exits 1 (since F2); if it has 1 line or fewer, it does nothing;
   - otherwise, it generates `scraper/analysis-odoo_partial.csv`, scrapes in `append` mode with `timeout 4h` and continues like the full run, but with `--run_mode partial`.
5. Any other value in `run_mode.txt` → exit 1 (since F2; before, it logged the error and exited with 0).
6. Exits and the instance shuts down.

Since F2, **every step has `|| exit 1`**: a step that fails, or a `timeout` that expires (exit 124), stops the run before the following steps and before the Odoo upload; the `trap EXIT` still shuts the instance down. The Odoo and ML scripts exit with code 1 on any error (no more `exit()` with code 0 or `except` blocks that only log), and an Odoo read that returns 0 records is an error. Before F2 the stage 2 steps had no `|| exit 1` and a failing step let the following ones run with whatever files existed (Nov 13–15, 2026 run).

The branch `origin/server-improvement` (e25e2c9) disables partial mode and changes training and prediction. The instance runs **`server-improvement`** (verified in F1, 2026-11-17), and `pi-improvements` contains e25e2c9 with no production changes since, so the line numbers in this document are from that commit.

### Files it produces

Everything lives **only on the instance's disk** (volume without snapshots). Paths are relative to the repo root; the scripts assume the working directory is that root.

**Log folder** (since F2): every log and state file goes to one folder **outside the code**, `PI_LOG_DIR` if it is set and non-empty, otherwise `<repo>/../logs` (`/home/ec2-user/logs` on the instance). `log_config.py`, `run_pipeline.sh` and `odoo_api_price_suggestion.py` apply the same rule; `run_pipeline.sh` exports the folder to every step. Use an absolute path in `PI_LOG_DIR`. It is outside the code so a new code folder (F4 releases) does not lose the state. Before F2, `run_pipeline.sh` wrote to `<repo>/logs` and only `wayakit_app.log` was in `<repo>/../logs`.

| File | Written by | Stage |
|---|---|---|
| `<log folder>/pipeline_execution.log` | `run_pipeline.sh` (output of every step) | 1 and 2 |
| `<log folder>/last_full_run.txt`, `<log folder>/run_mode.txt` | `run_pipeline.sh` | 1 |
| `<log folder>/wayakit_app.log` (rotation 5 MB × 3) | `log_config.py` (`LOG_DIR`, `setup_logging()`) | 1 and 2 |
| `ml_model/wayakit_products.csv` | `odoo_api_products.py` | 1 |
| `ml_model/wayakit_new_products_temp.csv` | `filter_new_products.py` (partial only) | 1 |
| `scraper/analysis-odoo.csv` (versioned in git) | `odoo_api_connection_products.py` (full only) | 1 |
| `scraper/analysis-odoo_partial.csv` | `odoo_api_connection_products.py` (partial only) | 2 |
| `scraper/competitors_complete.csv` | `scraper/main.py` (replaced only when the run succeeds, since F2) | 2 |
| `scraper/competitors_complete.tmp.csv` | `scraper/main.py` while it runs; left behind on failure for diagnosis (since F2) | 2 |
| `scraper/coverage_report.csv` | `scraper/main.py`, at the end of every run, also on error (since F2; see [Coverage report](#coverage-report)) | 2 |
| `ml_model/wayakit_cotizations.csv` | `odoo_api_cotizations.py` | 2 |
| `ml_model/competitor_products_from_odoo.csv` | `odoo_api_competitor_products.py` | 2 |
| `ml_model/competitor_volumetric_processed.csv`, `competitor_unit_processed.csv` | `1a_preprocess_data.py` | 2 |
| `ml_model/wayakit_products_to_predict_odoo.csv` | `1b_preprocess_data.py` | 2 |
| `ml_model/trained_models/*.joblib` (models, columns and `*_market_stats.joblib`) | `2_train_models.py:110-111,171-178` | 2 |
| `ml_model/wayakit_prediction_report.csv` | `3_predicted_prices.py` (uploaded by `odoo_api_price_suggestion.py`) | 2 |
| `<log folder>/odoo_previous_suggestion_ids.json` | `odoo_api_price_suggestion.py --run_mode full` (since F2): IDs of the previous suggestions (`ids`) and, once the create returns, of the new batch (`created_ids`), until the previous ones are archived and deleted. It only stays behind if the upload failed or was killed | 2 |
| `ml_model/price_suggestion_dry_run.csv` | `odoo_api_price_suggestion.py --dry-run` (since F2), by hand only | — |

There are no CloudWatch Logs groups or S3 bucket for PI. The "before" snapshot (CSVs from the Nov 13–15, 2026 run) was copied on 2026-11-17 to `data/f1_before_2026-11-15/` in the local repo, ignored by git; the `.joblib` models remain in the disk snapshot. Planned (F3): bucket `price-intelligence-<account>` with `data/`, `models/`, `state/`, `logs/` and `releases/`.

### Knowing whether the last run worked

There are no alerts or emails today. Planned (F3): SNS notification from the exit trap.

1. **Without starting the instance:** in Odoo (KSA company, see [Reviewing results in Odoo](#reviewing-results-in-odoo)), check the creation date of the `product.price.suggestion` records. If it matches the last full run, the upload happened.
2. **CloudTrail** (eu-north-1): `StartInstances` events on Thursdays and Fridays at 21:50 CDMX confirm that the schedules fired. They do not say whether the pipeline finished successfully.
3. **With the instance running** (see [Running by hand](#running-by-hand)): read the full `pipeline_execution.log` (pre-F2 code: `<repo>/logs/`; since F2: the [log folder](#files-it-produces)). With the pre-F2 code on the instance, **searching for `Success` (`Éxito` in logs written before the English translation reaches the instance) is not enough**: several scripts exit with code 0 even when they fail. Since F2 the last line says `Pipeline finished (code=N)`: `code=0` means every step succeeded; any other code means the run stopped at the step logged just before with `ERROR: ... failed`, and nothing after it ran. Also check `scraper/coverage_report.csv` (since F2) for sites or keywords with 0 results.

---

## Manual review between stages

How it works today (only in **full** runs):

1. Stage 1 (Thursday) regenerates `scraper/analysis-odoo.csv` on the instance's disk and logs that the file is ready for manual review (`run_pipeline.sh:93`).
2. Before stage 2 (Friday 21:50 CDMX), someone reviews the search list (column `Search Modifiers`), commits `scraper/analysis-odoo.csv` to `main` and, for the change to last, also `scraper/modifiers_mapping.csv`. The history has "weekly analysis-odoo edition" commits (6be62f5 and 7a5b506). Some were made from the server (author "Ubuntu", November 2025).
3. Before F2, stage 2 ran `git pull` and used the reviewed list. **Since F2 there is no `git pull` in stage 2**: a reviewed list only reaches the instance if someone pulls it by hand through SSM before stage 2 (see [Deploy and rollback](#deploy-and-rollback)); otherwise stage 2 uses the list stage 1 generated on the disk.

Who reviews today and how is an open question (plan question 2).

Risks:

- Stage 1 **rewrites a versioned file on the instance**. With the pre-F2 code, if someone pushed their review, `git pull` fails with "local changes would be overwritten", the error is not checked and stage 2 continues with the **unreviewed** list. For a manual pull (since F2), first `git checkout -- scraper/analysis-odoo.csv` on the instance.
- Edits made only in `analysis-odoo.csv` are lost on the next full run, because that file is regenerated from `modifiers_mapping.csv`.
- In **partial** runs (disabled since e25e2c9) there was no review: `analysis-odoo_partial.csv` is generated inside stage 2 (`run_pipeline.sh:187`).
- Before F2, the `git pull` also brought in any code change on the checked-out branch (`server-improvement`), in the middle of the run.
- Verified in F1: the instance has no stored git credentials (no `credential.helper` and no `~/.git-credentials`).

Mafer confirmed that **the keyword review is needed** (the sites do not find some products by their generic name). The new design, which must not hold up the monthly run, is under review (`plan.md`, section 6).

---

## Running by hand

> **Warning.** Starting the instance at another time does not launch the pipeline (the cron is Friday and Saturday 04:00 UTC, with no `@reboot`). But stage 2 in full mode **writes to Odoo** and deletes the previous suggestions, and running `run_pipeline.sh` shuts down the instance when it finishes. Before starting it:
> 1. Get Mafer's OK.
> 2. If you will be working close to the cron times, [pause the run](#pausing-the-run) (disable both schedules) and comment out the crontab.
>
> There is a disk snapshot from 2026-11-17 (`pi-f1-antes-inventario`, see `docs/resources.md`).

1. **Start the instance.** EC2 console (Stockholm) → *Instances* → filter by `Name = wayakit-app-server` → *Instance state → Start*. The public IP changes on every start (it has no Elastic IP). To see it without the console:
   ```bash
   aws ec2 describe-instances --profile wayakit-vdc \
     --filters "Name=tag:Name,Values=wayakit-app-server" \
     --query "Reservations[].Instances[].[InstanceId,State.Name]" --output table
   ```
2. **Connect through SSM** (since 2026-11-17; the role has `AmazonSSMManagedInstanceCore`). Wait 1–2 minutes for the instance to show as "Online" in SSM. No key is needed and the security group does not need to be opened:
   ```bash
   aws ssm describe-instance-information --profile wayakit-vdc --query "InstanceInformationList[].[InstanceId,PingStatus]" --output table
   aws ssm start-session --profile wayakit-vdc --target <instance-id>   # requires the Session Manager plugin
   sudo su - ec2-user
   ```
   For a one-off command without an interactive session, use Run Command (`aws ssm send-command --document-name AWS-RunShellScript ...`). That is how the F1 inventory was done.
3. **Check the cron and any run in progress:**
   ```bash
   crontab -l; sudo crontab -l; ls /etc/cron.d; systemctl list-timers --all
   ps aux | grep -E "run_pipeline|python|chrome" | grep -v grep
   sudo shutdown -c    # cancels a shutdown scheduled by the trap (you have 1 minute)
   ```
   To pause the cron, comment out its lines with `crontab -e` and note what was changed so you can restore it.
4. **Prepare the environment.** Repo at `/home/ec2-user/wayakit-price-prediction` (branch `server-improvement`), conda at `/home/ec2-user/miniconda`, environment `wayakit_env` with Python 3.10.20 (versions in `environment.yml`). `run_pipeline.sh:14-18` looks for conda at `conda info --base` or, failing that, at `/home/ec2-user/miniconda` (or at `/home/ubuntu/miniconda3` if `/home/ubuntu` exists, a leftover from the previous server; the current AMI is Amazon Linux 2023, user `ec2-user`).
   ```bash
   cd /home/ec2-user/wayakit-price-prediction
   source "$(conda info --base)/etc/profile.d/conda.sh"
   conda activate wayakit_env
   git status; git branch --show-current; git log -1 --oneline
   ```
   If the environment has to be recreated on the instance: `conda env create -f environment.yml -n wayakit_env`. Without `-n`, `environment.yml` creates `wayakit-price-prediction` (the local name) and `run_pipeline.sh:6` does not find it.
5. **Run the steps**, always from the repo root (the scripts' paths are relative). This is the full run of `run_pipeline.sh`, step by step and **without** the automatic shutdown:
   ```bash
   # Stage 1
   python ml_model/odoo_api_products.py
   python scraper/odoo_api_connection_products.py --input_odoo_products_file ml_model/wayakit_products.csv --output_analysis_file scraper/analysis-odoo.csv
   # Stage 2 (since F2 the competitor CSV is only replaced if scraping succeeds; with the pre-F2 code, back it up first:
   # cp scraper/competitors_complete.csv scraper/competitors_complete.bak.csv)
   timeout 30h python scraper/main.py --analysis_file scraper/analysis-odoo.csv --output_mode overwrite --output_file scraper/competitors_complete.csv
   python ml_model/odoo_api_cotizations.py
   python ml_model/odoo_api_competitor_products.py
   python ml_model/1a_preprocess_data.py
   python ml_model/1b_preprocess_data.py --run_mode full
   python ml_model/2_train_models.py
   python ml_model/3_predicted_prices.py
   # Check the records without touching Odoo (since F2): no secrets, no connection
   python ml_model/odoo_api_price_suggestion.py --run_mode full --dry-run
   # Upload to Odoo: only with Mafer's OK and the KSA company confirmed (see "Re-uploading suggestions")
   python ml_model/odoo_api_price_suggestion.py --run_mode full
   ```
   - To test **without writing to Odoo**, stop after `3_predicted_prices.py` and review `ml_model/wayakit_prediction_report.csv`, or (since F2) run the upload with `--dry-run`, which writes the exact records it would send to `ml_model/price_suggestion_dry_run.csv`.
   - After each step, check `echo $?` (since F2 any error gives a code other than 0) and the end of `wayakit_app.log` in the [log folder](#files-it-produces) (`../logs/` from the repo root unless `PI_LOG_DIR` is set). With the pre-F2 code a step can exit with code 0 even if it failed.
   - The full scraping takes up to 30 h. If the session drops, the process dies: run it with `nohup … > <file>.log 2>&1 &` (or in `tmux`, if installed).
   - Running the steps by hand does **not** update `last_full_run.txt` or `run_mode.txt`. Since e25e2c9 it does not matter for the mode: stage 1 always writes `run_mode.txt=full`.
   - Stage 1 rewrites `scraper/analysis-odoo.csv`, which is versioned: `git status` will show it as modified.
6. **When done:** close any leftover Chrome processes (`pkill -f chrome`), restore the cron, shut down the instance (`sudo shutdown -h now` or from the console) and re-enable the schedules if applicable.

Running the whole script (`bash run_pipeline.sh` or `bash run_pipeline.sh --stage 2`) also works, but **it shuts down the instance when it finishes**.

---

## Pausing the run

Today, from the console: **Amazon EventBridge → Scheduler → Schedules** (Stockholm) → select `Wayakit-Start-Thursday-Night` and `Wayakit-Start-Friday-Night` → **Disable**. To resume, **Enable**. With the schedules disabled, the instance does not start and the internal cron does not run.

Check the state (read-only):

```bash
aws scheduler list-schedules --group-name default --profile wayakit-vdc
aws scheduler get-schedule --group-name default --name Wayakit-Start-Thursday-Night --profile wayakit-vdc
```

- Do not touch the schedules in the `voz-del-cliente` group: they belong to VdC.
- If a run in progress has to be stopped: connect through SSM, stop the process and check whether the upload to Odoo has already started (step 9 in the log). With the pre-F2 code, if it is stopped in the middle of a full-mode upload, the suggestions table may end up empty. Since F2 it is never left empty: if the process was killed before the new batch was confirmed, rerun the full upload (it discards the leftover file and replaces everything); if it was killed after, the table has old and new rows and `--cleanup-previous` finishes it. `--cleanup-previous` itself refuses to delete anything when the new batch was not confirmed (see [Re-uploading suggestions](#re-uploading-suggestions)).
- Planned (F3): `python infra/schedule.py --disable` / `python infra/schedule.py`, as in VdC. The two `default` schedules are retired with the `RETIRED` list.

---

## Reviewing results in Odoo

> **Confirm the company before reading or writing.** PI belongs to **KSA** (prices in SAR, scrapers for .sa sites) and the database it uses is KSA only (Mafer, 2026-11-17), but **no read filters by company**: no domain uses `company_id` or `allowed_company_ids`. Since F2 the upload refuses to write if the API user sees more than one company. When querying Odoo, by hand or with the Odoo connector from Claude, first confirm that it is the KSA company.

What PI writes:

| Model | What it stores |
|---|---|
| `product.price.suggestion` | The current suggestion per product: `product_id_str`, `product_name`, `product_type`, `generic_product_type`, `subindustry`, `industry`, `volume_units`, `production_cost`, `suggested_price`, `profit`, `predicted_price_per_unit`, `model_confidence`, `market_min_found`, `market_max_found`, `competitors_count` (`row_to_record()` in `odoo_api_price_suggestion.py`) |
| `product.price.suggestion.history` | Copy of the previous suggestions, with `original_create_date`. Since F2 they are archived **after** the new batch is created (`archive_and_unlink()`) |

What to check:

- After a **full** run, that the number of `product.price.suggestion` records matches the rows in `ml_model/wayakit_prediction_report.csv`. If there are twice as many, it was also uploaded in partial mode (see Known issues). After a partial run, the table has the previous suggestions plus those for the new products: the partial CSV only contains the new ones (`1b_preprocess_data.py:23-27`).
- That the creation date is that of the last run.
- `suggested_price` against `production_cost`: the price never goes below cost × 1.30 (`3_predicted_prices.py:134-136`). Since e25e2c9, if there are 3 or more competitors, the prediction is clipped to the `market_min_found`–`market_max_found` range before the floor (`3_predicted_prices.py:126-132`); the floor wins, so it can end up above the market maximum. With fewer than 3 competitors **there is no ceiling**. In Pets it tends to come out high (see [plan.md](plan.md), F5 and F7).
- `market_min_found` and `market_max_found` come only from competitors, without Wayakit prices (`2_train_models.py:177-178`).
- A low `competitors_count` (1 or 2) means the suggestion relies on very little data and that the clip to the market range was not applied.
- The report also includes `clamped_to_market` and `volume_outside_market` (`3_predicted_prices.py:150-151`), but `odoo_api_price_suggestion.py` does not upload them: they are only visible in `ml_model/wayakit_prediction_report.csv`.

What PI reads from Odoo: `product.master` (active), `sale.order.line` (confirmed sales; since F2 the **last 12 rolling months** up to the run date, before fixed to 2025), `competitor.product` (all records) and, before writing, `res.company` (since F2).

---

## Re-uploading suggestions

Uploads the contents of `ml_model/wayakit_prediction_report.csv` to `product.price.suggestion`. It is run inside the instance, from the repo root, with `wayakit_env` active. **It writes to Odoo: only with Mafer's OK and with the KSA company confirmed.** This section describes the F2 script; the pre-F2 behavior (still on the instance) is at the end.

```bash
python ml_model/odoo_api_price_suggestion.py --run_mode full --dry-run   # 1. check: no secrets, no Odoo
python ml_model/odoo_api_price_suggestion.py --run_mode full             # 2. upload (Mafer's OK)
python ml_model/odoo_api_price_suggestion.py --cleanup-previous          # 3. only if step 2 says the cleanup failed
```

| Mode | What it does |
|---|---|
| `--dry-run` | Builds the records and writes them to `ml_model/price_suggestion_dry_run.csv` (change it with `--dry-run-output`). Reads no secret and connects to nothing. Exits 0, or 1 if the report is missing or broken (see below). With `--cleanup-previous` it only says how many IDs the file has |
| `--run_mode full` | (1) Checks the company. (2) Reads the IDs of the current suggestions and saves them to `odoo_previous_suggestion_ids.json` in the [log folder](#files-it-produces) (`ids`). (3) Creates the new batch in one call (Odoo creates all or none). (4) Writes the new IDs to the file (`created_ids`) as soon as the create returns. (5) Checks that those new records exist (`search_count`), copies **only the saved previous IDs** to `product.price.suggestion.history` and deletes **only those IDs**. (6) Deletes the JSON file. If the file already exists when it starts: without `created_ids` (an earlier upload was killed before its create was confirmed) it is deleted with a warning and the upload goes on, re-reading every current ID, so whatever that earlier create did is replaced too; with `created_ids` the upload refuses (see below) |
| `--run_mode partial` (or **without** `--run_mode`: it is the default value) | Checks the company and only runs `create`, without deleting. If there were already suggestions, they end up duplicated |
| `--cleanup-previous` | Finishes step 5 of a full upload from `odoo_previous_suggestion_ids.json` in the log folder (skips the archive if the file says it was already done, so the history is not copied twice), then deletes the file. Uploads nothing. **Refuses (exit 1, deletes nothing) if the file has no `created_ids`, or if those new records do not all exist in Odoo**: then the previous suggestions may be the only ones left |

Other options: `--input` (report, default `ml_model/wayakit_prediction_report.csv`) and `--ids-file` (default `odoo_previous_suggestion_ids.json` in the log folder: `$PI_LOG_DIR` or `<repo>/../logs`). A cleanup must run with the same `PI_LOG_DIR` as the upload that left the file (or pass `--ids-file`).

What can happen and what to do:

| Log message (exit 1 in every case) | State of Odoo | What to do |
|---|---|---|
| `Expected exactly 1 company visible to the Odoo user, found N` | Untouched | The API user sees more than one company: a company filter is needed in the code. Do not upload; tell Mafer |
| `No valid records to upload` or `N of M rows could not be parsed (more than 5%)` | Untouched | The report is broken (missing columns, NaN prices, unreadable `volume`). Rerun `3_predicted_prices.py` and check it. Each skipped row is logged as `Skipping row N (Product ID: …)`. Below 5 % the bad rows are skipped with a warning and the rest is uploaded |
| `… exists: a previous full upload created its batch but did not finish its cleanup` | Old and new rows from the previous upload | Run `--cleanup-previous` first, then the full upload again |
| `… from an interrupted upload has no created_ids; discarding it` (warning, the upload goes on) | An earlier full upload was killed during its create | Nothing: this upload replaces every current suggestion |
| `--cleanup-previous`: `… has no created_ids: the new batch was never confirmed` or `Only N of the M new suggestions … exist` | Untouched by this command; the previous suggestions may be the only ones | Do not delete by hand. Rerun the full upload (with no `created_ids` it discards the file itself; in the second case check the table in Odoo, then delete the file and rerun) |
| `Creating the new suggestions failed … The previous suggestions were not touched` | Previous suggestions untouched (the JSON file is removed). After a timeout or a lost connection, Odoo may still have created the new batch | Fix the cause (Odoo down, field error in the log) and rerun the full upload: it replaces every current suggestion, including a batch created by the failed call |
| `… archiving/deleting the previous ones failed … retry with … --cleanup-previous` | **Old and new rows at the same time** (never empty) | Run `--cleanup-previous` |

- XML-RPC is not transactional across calls: between steps 3 and 5 there are a few seconds with old and new rows. Each call times out after 300 s (`pi_odoo.XMLRPC_TIMEOUT_S`).
- Before F2 (still on the instance until it is deployed): full mode **deleted first and created afterwards** (domain `id != 0`); if the `create` failed, the table was left **empty** and the script exited with code 0. To recover an empty table with the same CSV: `--run_mode partial`. If the CSV did not exist or had no valid rows, it exited with code 0 without uploading anything.

---

## Verifying a change before committing

Locally, with the `wayakit-price-prediction` environment (see [Before you start](#before-you-start)). None of this calls AWS, Odoo, Apify or Bedrock. Details in the [`verify`](../.claude/skills/verify/SKILL.md) skill; rules in [REVIEW.md](../REVIEW.md) and [reliability-checklist.md](reliability-checklist.md), read from the base branch.

```bash
scripts/verify.sh quick   # unit + property, seconds
scripts/verify.sh full    # + e2e + differential against origin/server-improvement, ~1.5 min
```

- `full` needs `data/f1_before_2026-11-15/` (ignored by git) and `git fetch origin`. If the data is missing, it ends in FAIL: the commit can only say `quick`.
- Each run leaves `.claude/state/last_verification.json` (mode, result and tree) and a line in `.claude/state/verifications.jsonl`. The `commit-msg` hook requires the trailer `Verified: <quick|full|none> (<detail>)` and, for `quick` or `full`, an OK record for the same tree. `verify.sh` records the index tree, so the order is `git add` of everything that goes in the commit, then `verify.sh` and then `git commit`, without touching anything in between. The trailer goes in the last paragraph of the message, next to `Co-Authored-By` if present (no blank line between the two).
- The checks (`REVIEW.md`, `.claude/`, `scripts/`, `pytest.ini`, `tests/conftest.py`, the harnesses in `tests/` and `docs/reliability-checklist.md`) go in a PR separate from production code. `scripts/hooks/protect_checks.sh` blocks editing them from Claude Code unless `PI_EDIT_CHECKS=1`, which only the lead sets in a session without a team.
- **Checks-only session** (to change checks):
  1. Mafer creates `.claude/settings.local.json` with `{"env": {"PI_EDIT_CHECKS": "1"}}` (local file, ignored by git). While it exists, **no team is assembled**, because the teammates would inherit the permission.
  2. Only checks and docs are changed, without `scraper/`, `ml_model/` or `run_pipeline.sh`. Then `git add`, `verify.sh full` and commit with its stamp.
  3. Mafer deletes `.claude/settings.local.json` when done **and restarts the Claude Code session**: the variable stays in the environment of the session that is already open (confirmed on 2026-11-17), so the lock does not close again until a new session is opened.
- **While the checks are not on the base branch:** the `TaskCompleted` gate compares against `merge-base(PI_CHECKS_BASE, HEAD)`. Until the checks PR is merged into `server-improvement`, the whole setup counts as "changed checks" and the gate would block any task that touches production. So, after the checks commit and before starting a team, Mafer leaves only `{"env": {"PI_CHECKS_BASE": "<sha of the checks commit>"}}` in `.claude/settings.local.json` (without `PI_EDIT_CHECKS`). The hooks read the Claude Code environment, not a terminal's. When the PR is merged into the base, it is removed and the default value (`origin/server-improvement`) comes back.
- If `verify.sh` fails due to XPASS, a bug that had `xfail(strict=True)` was fixed: that `xfail` must be removed (`pi-tester` does it).
- If the same check fails 3 times in a row, stop and escalate to Mafer (3-rounds rule).

---

## Deploy and rollback

### Today

- There is no CI/CD or deploy. With the pre-F2 code still on the instance, code reaches it through the `git pull` at the start of stage 2 (`run_pipeline.sh:133` in e25e2c9): the checked-out branch (`server-improvement`), without a pinned version and without checking the result (the instance has no stored git credentials, so with a private repo that pull fails silently).
- **Since F2 there is no `git pull` in the pipeline. Until F4, deploy by hand through SSM**, with Mafer's OK and with the schedules paused (see [Pausing the run](#pausing-the-run)) so the pull does not happen while a stage is running:
  ```bash
  # inside the instance (SSM session, user ec2-user), from the repo root, with no stage running
  ps aux | grep -E "run_pipeline|python" | grep -v grep     # must print nothing
  git status                                               # stage 1 leaves scraper/analysis-odoo.csv modified
  git checkout -- scraper/analysis-odoo.csv                # only if that is the only local change
  git pull --ff-only origin server-improvement
  git log -1 --oneline                                     # note the SHA in the change's PR
  ```
  The pull needs no credentials: the GitHub repo is public, so it is anonymous HTTPS (decided 2026-11-18). Never put a personal GitHub account or token on the instance, the repo, the logs or the shell history. If the repo is made private, use a read-only deploy key generated on the instance (Mafer configures it). Then re-enable the schedules.
- **Rollback today:** `git revert` of the commit on `server-improvement`, push, and the same manual pull. Do not run `git checkout <sha>` inside the instance without Mafer's OK: it leaves the instance off the branch.

### F2 is not on the instance yet

- The F2 relevance AI uses Bedrock with the instance role, which **has no `bedrock:InvokeModel` until F3**: with the F2 code, stage 2 stops at the Bedrock preflight (exit 1, nothing uploaded). Do not deploy F2 to the instance before F3 creates that permission.
- While the instance runs the pre-F2 `run_pipeline.sh`, its stage 2 `git pull` could bring F2 in by itself once it is merged into `server-improvement`, rewriting `run_pipeline.sh` while bash is running it. Decided 2026-11-18: F2 is merged into `server-improvement` only after F3 creates the permission; the schedules and the crontab are not paused.
- **The state files move when F2 is deployed**: from `<repo>/logs` to the [log folder](#files-it-produces), `/home/ec2-user/logs` by default (where `wayakit_app.log` already is). Run stage 1 before the first F2 stage 2 (or copy `run_mode.txt` and `last_full_run.txt` there); otherwise stage 2 stops because `run_mode.txt` is missing. Point the `ec2-user` crontab redirection (`>> .../logs/cron.log`) to the same folder (instance configuration, F3).
- The old `<repo>/logs/cron.log` and `<repo>/logs/pipeline_execution.log` on the instance still contain the Gemini key: scrub or delete them as already planned (see [Security](#security)); moving the folder does not clean them.

### Planned (F4): pull-on-boot

1. GitHub Actions (OIDC, no keys) runs the tests on every push to `main`.
2. With CI green, it publishes `releases/<sha>.tar.gz` to S3 and updates the `current` pointer.
3. It starts the instance in **"deploy only"** mode: the instance installs the release, verifies it, switches the symlink atomically and shuts down, without running the pipeline.
4. If the deploy fails, the symlink is not touched and the previous version keeps working.
5. **Rollback:** `workflow_dispatch` with a `sha`, which moves the pointer and runs the deploy again. The last 5 releases are kept on the instance.

This depends on F3 (bucket, roles, GitHub OIDC provider, SSM agent, `bootstrap.sh`), which does not exist yet. The exact steps will be written here when they are implemented.

---

## Apify calibration and model test

Both runs were done on 2026-11-16 from Mafer's Mac, with her approval. The results are in [plan.md](plan.md), section 5. The scripts and raw data stayed **outside the repo**, in that session's temporary scratchpad.

| Run | How it was run | Cost |
|---|---|---|
| Apify calibration (`calibrate.py`) | VdC client (`voz-del-cliente/pipeline/apify.py`), token from `voz-del-cliente/api-keys` (never printed). Keyword "pet urine odor remover". `junglee~amazon-crawler` with the amazon.sa search URL, 20 results, `scrapeProductDetails: true`, no `countryCode` or `zipCode`, cap USD 0.15. `saswave~noon-product-scraper` with the `noon.com/saudi-en` search, 1 page, cap USD 0.10, plus the details of 5 products (cap USD 0.05) | ~USD 0.08 |
| Bedrock model test (`bakeoff.py`) | boto3 with the `wayakit-vdc` profile in eu-north-1, Converse API with `temperature=0` and JSON output with schema. DeepSeek V3.2, Qwen3 235B and Haiku 4.5 on 66 titles from the calibration: batches of 22, one title per call and 10 wipes titles. The DeepSeek and Qwen EULAs were accepted with it | ~USD 0.04 in total |

- Running them again costs money (Apify and Bedrock): ask for Mafer's OK and set caps per actor (`maxItems` and `maxTotalChargeUsd`).
- The Apify account is VdC's: PI's spending competes with that account's monthly limit (see `voz-del-cliente/docs/runbook.md`, section *Apify*).
- Planned (F2): the model test script moves into the repo as `tests/bakeoff_relevance.py`. [plan.md](plan.md) does not say whether the calibration script also moves into the repo; pending decision (it would be useful to repeat the calibration with other keywords in F6).

---

## Relevance AI (Bedrock)

Since F2 (not yet on the instance; see [F2 is not on the instance yet](#f2-is-not-on-the-instance-yet)).

- `scraper/services/ai_service.py` calls Amazon Bedrock (Converse API, eu-north-1) with the **instance role**; it reads no secret. Primary model `deepseek.v3.2` (DeepSeek V3.2, on-demand); fallback `eu.anthropic.claude-haiku-4-5-20251001-v1:0` (Claude Haiku 4.5, EU cross-region inference profile). To switch models without a code change (e.g. to application inference profile ARNs after F3), set `PI_RELEVANCE_PRIMARY_MODEL` and `PI_RELEVANCE_FALLBACK_MODEL`.
- Every call: temperature 0, JSON forced with `outputConfig` (JSON schema; no `toolConfig`), up to 25 titles per call (the Selenium scrapers send 1 title per call through the same batch path), read timeout 120 s (the first call with a new schema compiles the grammar, up to ~90 s).
- Retries: botocore adaptive retries (3 attempts) plus exponential backoff with jitter (3 attempts, 2 s base, 60 s cap) on `ThrottlingException`, `ServiceUnavailableException`, `ModelNotReadyException`, `ModelTimeoutException`, `InternalServerException` and network timeouts; then the fallback model. Network errors left after botocore's retries (connection, read or connect timeout, closed connection) are transient: back off, then fallback. Invalid JSON, or an answer that does not cover every title exactly once, goes straight to the fallback. Answers are cached per (keyword, title), and per title for wipes, during the run.
- Before scraping starts, `scraper/main.py` makes one tiny preflight call to **each** model (primary and fallback). If either fails (missing credentials, `AccessDeniedException`, an unknown model ID, an invalid answer, or throttling after the retries), the run stops at once with exit 1, before Chrome starts. A run never starts without a working fallback, so a permission problem on the fallback cannot appear hours into the run.
- Logs and error messages show only the model name (for an ARN, only the part after the last `/`, never the account ID), the exception class and the AWS error code. The AWS error message is never logged, and the error is not chained to the botocore exception: AWS messages can contain the role ARN with the account and instance IDs.
- A title for which both models fail counts as an **AI failure** (treated as not relevant; wipes count 0). At the end, if AI failures exceed 5 % of the titles asked (relevance and wipes together), `scraper/main.py` exits 1 and does not replace `competitors_complete.csv`. The log line `Relevance AI: N titles evaluated, R relevant, F failures (rate X %)` gives the numbers.

## Scraper: fail loudly and temp file

Since F2 (`scraper/main.py`).

- The scraper writes to a temporary file next to the output (`scraper/competitors_complete.tmp.csv`) and only replaces `scraper/competitors_complete.csv` (`os.replace`, atomic on the same filesystem) when the run succeeds. In `append` mode the existing CSV is copied to the temp file first. If the run fails, the previous `competitors_complete.csv` is untouched and the partial rows stay in the `.tmp.csv` for diagnosis (ignored by git; the next run overwrites it).
- Exit 1 when: the instructions file is missing or unreadable; ChromeDriver cannot be installed; the Bedrock client or the preflight of either model fails; a fatal AI error happens during scraping; any unexpected error in the main loop; zero rows were scraped in total; AI failures > 5 %. An exception in one site/keyword search does not abort the run: it is logged and appears in the `error` column of the coverage report.
- `timeout 30h` sends SIGTERM: `main.py` turns it into an exit (code 143 internally; `timeout` returns 124), Chrome is closed by the scrapers' `finally` and the coverage report is still written. The previous CSV is untouched and `run_pipeline.sh` stops before training.
- New flag: `--coverage_file` (default `scraper/coverage_report.csv`).

## Coverage report

`scraper/coverage_report.csv` (since F2). Written at the end of every scraper run, also when it exits with an error (best effort; it shows how far the run got). Ignored by git (`*.csv`). One row per subindustry × type of product × site × keyword actually searched (sites skipped because a B2B site has no specific keyword, and subindustries missing from `TARGET_MAP`, do not appear).

| Column | Meaning |
|---|---|
| `subindustry`, `type_of_product`, `site`, `keyword` | What was searched; `keyword` is the exact search term used on that site |
| `found` | Product titles the relevance AI evaluated during that search (for AeroSense, which does not use the AI, = rows written) |
| `relevant` | Titles the AI accepted |
| `rows_written` | Rows added to `competitors_complete.csv` |
| `ai_failures` | Titles where both models failed (relevance + wipes) |
| `error` | Exception class if the search failed (e.g. `TimeoutException`, `RelevanceAgentFatalError`), empty otherwise |

How to read it:

- `found` 0 with no error: the site returned nothing for that keyword. Try another keyword for that site (the recall problem in `plan.md`, section 6).
- `found` > 0 and `relevant` 0: the site has results but none match (keyword too generic, or the product is not sold there).
- `relevant` > `rows_written`: possible when the scraper's own limit stops early.
- `ai_failures` > 0: Bedrock problems in that search.
- The log ends with the list of pairs with 0 relevant results.

---

## Changing a key

All PI credentials are in the secret `prod/wayakit-app` (eu-north-1), as key/value pairs. Keys never go through chat, the repo, a file or Notion.

1. AWS console (Stockholm) → **Secrets Manager → `prod/wayakit-app` → Retrieve secret value → Edit** → *Key/value* tab.
2. Change only the value of the relevant key. **Do not delete the other rows.** → **Save**.
3. If it was a replacement, revoke the old key in its service.

- Since F2 the secret is read only by `pi_secrets.py` (`get_secret()`, `get_odoo_credentials()`; name and region in `SECRET_NAME` and `REGION_NAME`). If an Odoo key is missing or empty, the scripts exit 1 with the **names** of the missing keys, never their values. Planned (F6): the Apify key in the same secret.
- After the first successful Bedrock run on the instance: remove `GEMINI_API_KEY` from the secret (the F2 code no longer reads it), revoke the key and clean the logs (see Known issues, *Security*).

---

## Known issues

Errors that have already appeared or that are visible in the code, and what to do. Line numbers are from e25e2c9 (`server-improvement`, what the instance runs; `pi-improvements` contains that commit with no production changes since).

### Execution

| Symptom | Cause | Fix |
|---|---|---|
| The log says `Success` for a step that failed (pre-F2 code) | Several scripts end with `exit()`, which returns 0: `odoo_api_products.py:48,54,67,126`, `odoo_api_cotizations.py:49,55,66,101`, `odoo_api_price_suggestion.py:52,56,65,78,109`. Others catch the exception and continue (`1a_preprocess_data.py:87-90`, `2_train_models.py:182-183`) | Read the full log, not just `Success`. **Fixed in F2**: every error exits with code 1 |
| Stage 2 trains and uploads to Odoo even though scraping failed (pre-F2 code) | The stage 2 steps do not have `\|\| exit 1` (`run_pipeline.sh:154-167,187-201` in e25e2c9), and the full run deletes `competitors_complete.csv` before scraping (line 151) | Check the log before trusting the suggestions. **Fixed in F2**: `\|\| exit 1` per step and the new CSV is written to a temporary file |
| Stage 2 used the unreviewed list or old code (pre-F2 code) | `git pull` failed ("local changes would be overwritten") because stage 1 rewrites `scraper/analysis-odoo.csv`, and nobody checks the result (`run_pipeline.sh:133` in e25e2c9) | See [Manual review between stages](#manual-review-between-stages). **Fixed in F2**: no `git pull` in the pipeline; deploy by hand (see [Deploy and rollback](#deploy-and-rollback)) |
| Stage 1 and stage 2 run at the same time (possible, not observed) | The stage 2 cron is at a fixed time (Saturday 04:00 UTC) and does not check whether stage 1 is still running | Check `pipeline_execution.log`. Planned (F3): a single monthly schedule |
| **The Nov 13–15, 2026 run uploaded suggestions to Odoo with failed scraping** | Scraping hit the `timeout 30h` and exited with an error, but `competitors_complete.csv` only had 53 rows (SACO Automotive 36, AeroSense 14, Fine Airports 3; nothing from Amazon, Home or Pets). The following steps do not check the error: it trained, uploaded in full mode (deleted the previous ones) and ended with code 0 | The current suggestions in Odoo come almost entirely from `competitor.product`. **F2**: `\|\| exit 1` per step, the CSV is only replaced on success, and safe upload. Still pending: a minimum of rows per source (the coverage report shows the counts) |
| `wayakit_app.log` is not in the repo's `logs/` (pre-F2 code) | `log_config.py` goes up one level from the repo root, while the pre-F2 `run_pipeline.sh` writes to `<repo>/logs` | Look for it at `../logs/wayakit_app.log`. **Since F2** every log and state file is in that one folder (`PI_LOG_DIR`, default `<repo>/../logs`; `decisions.md`, 2026-11-18) |
| A stage 2 run stops at `2. Run scraping` with `AccessDeniedException` (F2 code) | The instance role has no `bedrock:InvokeModel` (planned for F3) | Expected until F3; nothing was uploaded. See [F2 is not on the instance yet](#f2-is-not-on-the-instance-yet) |
| The upload exits 1 with `… exists: a previous full upload created its batch but did not finish its cleanup` (F2 code) | A previous full upload created the new batch (`created_ids` in the file) but failed to archive/delete the old one | Run `python ml_model/odoo_api_price_suggestion.py --cleanup-previous` (see [Re-uploading suggestions](#re-uploading-suggestions)). If it refuses, follow its message: never delete suggestions by hand |
| An Odoo step fails with `TimeoutError` (F2 code) | Every XML-RPC call times out after 300 s (`pi_odoo.XMLRPC_TIMEOUT_S`); before, a hung call blocked until `timeout 1h` killed the process | Check that Odoo responds. A timeout during the upload's create is covered by the rows in [Re-uploading suggestions](#re-uploading-suggestions) |
| A `.joblib` does not load after updating packages | `requirements.txt` does not pin versions of pandas, numpy or scikit-learn | Do not update packages in `wayakit_env` by hand. `environment.yml` (2026-11-17) pins the instance's versions (Python 3.10.20, scikit-learn 1.7.2, pandas 2.3.3, numpy 2.2.6). To recreate it on the instance: `conda env create -f environment.yml -n wayakit_env` |
| The local run gives different prices than the production report | With the frozen data in `data/f1_before_2026-11-15/`, the local run changes 520 of 738 `predicted_price` values (up to 51.86 SAR), even though the processed files come out identical. Cause under investigation: CPU/BLAS, scikit-learn or numpy versions, or that run not being e25e2c9 | Do not compare local outputs against production. The differential tests compare base against the new version on the same machine (`scripts/verify.sh full`). See `plan.md`, finding 13 |

### Scraping

| Symptom | Cause | Fix |
|---|---|---|
| The run ends with 0 products and no error (pre-F2 code) | `GEMINI_API_KEY` is missing from `prod/wayakit-app` or could not be read: `is_relevant` returns `False` for everything (`scraper/services/ai_service.py:138-139`) and the only trace is the "API KEY not found" warning | Check that the key exists in the secret (without printing the value). **Fixed in F2**: Bedrock, preflight call, 0 rows → exit 1 and AI failures > 5 % → exit 1 |
| Products are silently discarded (pre-F2 code) | On a 429, Gemini waits 60 s; on the third attempt it returns `False` (`ai_service.py:151-154`) | Search for "RATE LIMIT HIT" in `wayakit_app.log`. **F2**: Bedrock with backoff and fallback; failures are counted (`ai_failures` in the coverage report) |
| Land transportation has no competitor data | `config.py:19` says `'Land Transportation'` and `analysis-odoo.csv` says `Land transportation` (31 rows). The comparison is case-sensitive (`main.py:333`) and gives no warning | Pending: fix the key in `scraper/config.py:19` (code change, with Mafer's OK) |
| 4 SACO exclusions are not applied | Missing commas in `SACO_EXCLUSIONS` (`config.py:65,70`): Python concatenates the strings | Pending: add the commas |
| GoGreen: "Could not set language to English" many times | Seen in the Nov 13–15, 2026 run (`gogreen_scraper.py:48`); switching the site's language fails | Pending: review the GoGreen scraper (code change, with Mafer's OK) |
| Chrome processes are left open | In SACO, `driver.get` and `_handle_overlays` are outside the `try` (`saco_scraper.py:122-123`) and errors other than Timeout do not close the browser | `pkill -f chrome` when running by hand. The instance shuts down at the end of the scheduled run |
| Scraping does not start: `Could not install ChromeDriver` | `ChromeDriverManager().install()` resolves the driver on every run and downloads it if it is not cached (`main.py:263`), and depends on the network and the Chrome version | Retry. On the instance: Google Chrome 146.0.7680.177 and webdriver-manager 4.0.2 (F1) |
| Inflated price per liter on Amazon | No pack multiplier: a "Pack of 3, 500 ml" is stored as 500 ml with the price of 3 (`amazon_scraper.py:80-109`). In Mumzworld, the pack regex takes the volume as the multiplier (`mumzworld_scraper.py:58`) | Planned (F6/F7): pack multiplier in the Apify adapters and robust statistics in Pets |
| Wayakit products in the Amazon and Noon results | Neither the Amazon scraper nor the actors exclude the Wayakit brand or seller (seen in the calibration) | Planned (F6): exclude the Wayakit brand and seller, as VdC does |

### ML and Odoo

| Symptom | Cause | Fix |
|---|---|---|
| 2026 sales do not influence the suggestions (pre-F2 code) | Dates fixed from 2025-01-01 to 2025-12-31 (`odoo_api_cotizations.py:75-76` in e25e2c9) | **Fixed in F2**: last 12 rolling months (`sales_window()`) |
| Rows with price 0 or products in `kg` with odd prices | `0.00` rows are not discarded and `kg` is not in `volumetric_units`, so it falls into price per piece (`1a_preprocess_data.py:55-57,79`) | Planned (F7): small fixes |
| `product.price.suggestion` empty (pre-F2 code) | In full mode `unlink` runs first and then `create`; if the `create` failed, the table is left empty (`odoo_api_price_suggestion.py:148-182` in e25e2c9) | Re-upload with `--run_mode partial` (see [Re-uploading suggestions](#re-uploading-suggestions)). **Fixed in F2**: `create` first, then `unlink` of the previous IDs only |
| Duplicate suggestions | `odoo_api_price_suggestion.py` was run without `--run_mode` (defaults to `partial`) or twice in partial | Re-upload with `--run_mode full`, only if `wayakit_prediction_report.csv` is from a full run (the partial one only contains new products) |
| Data from another company in sales or suggestions | No read filters by company (`ml_model/odoo_api_*.py`); the database is KSA only (Mafer, 2026-11-17) | Confirm the KSA company before reading or writing. **F2**: the upload exits 1 if the API user sees more than one company; reads would need a `company_id` filter if a second company ever appears |

### Apify (Planned, F6; seen in the calibration)

| Symptom | Cause | Fix |
|---|---|---|
| Noon returns 50 rows even though `maxItems` was 20 | `saswave/noon-product-scraper` delivers the full page | Run once per keyword, with a spending cap (`maxTotalChargeUsd`), and truncate in the converter |
| On Amazon, sizes do not match between `Item Volume`, `Unit Count` and `Size` (e.g. "5 Liters" vs "907 Grams") | Amazon fills those fields inconsistently | Keep the rule that 2 of 3 sources must agree, or resolve it with the AI model |
| Country of origin almost never comes through | `Country of Origin` came in 3 of 20 on Amazon and does not come on Noon | Use signals that do come through: seller, Noon "global" shipping, units in fl oz (F7) |

### Security

| Symptom | Cause | Fix |
|---|---|---|
| **The Gemini key is in plain text** in `<repo>/logs/cron.log` (72 lines) and `<repo>/logs/pipeline_execution.log` (87 lines) on the instance (confirmed in F1). It does not appear in `~/logs/wayakit_app.log*` | The key goes in the URL (`ai_service.py:54-55`) and `requests.exceptions.HTTPError` exceptions are logged with the full URL | Do not copy or print those logs without redacting `key=`. **Revoke or rotate the key in Google now**, clean up or delete the logs and notify IT (Mafer). F2 removes Gemini from the code; after the first Bedrock run on the instance, remove the key from `prod/wayakit-app` |
| The instance role can read and write every secret in the account, including `voz-del-cliente/api-keys` | `Wayakit-EC2-Secrets-Role` has `SecretsManagerReadWrite` | Notify IT. Planned (F3): `instance_role.py` with least privilege |
