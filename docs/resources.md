# Resources

Inventory of what exists for Price Intelligence (PI). Wayakit account, region **eu-north-1** (Stockholm) unless stated otherwise. Updated on **2026-11-17** (F2 code changes; no AWS resource was created or changed in F2).

`<account>` = Wayakit's AWS account ID (`aws sts get-caller-identity`). It is never written in the repo.

**Rule:** when adding a resource → script in `infra/` + row here + step in `docs/runbook.md` + row in `docs/decisions.md` if applicable. Every pipeline or infrastructure change updates these docs in the same commit.

Overall status: **everything that exists today was created by hand** (console or CLI) and there is no script in `infra/`. Nothing has been created or changed in AWS as part of the plan yet ([plan.md](plan.md)). The inventory was done in read-only mode with the `wayakit-vdc` AWS CLI profile. It was not verified which tags the current resources have, besides `Name`. New resources will carry the `project = price-intelligence` tag.

## What exists today

| Resource | Name | How it was created | Status | Approx. cost |
|---|---|---|---|---|
| EC2 instance | `wayakit-app-server` (`Name` tag). t3.medium, Amazon Linux 2023, key pair `wayakit-stockholm-key`. No userData, no Elastic IP, IMDSv2 not verified | By hand | `stopped` almost always. The schedules start it; it shuts itself down with `sudo shutdown -h +1` in the `trap EXIT` (`run_pipeline.sh:52`). Runs the **`server-improvement`** branch (verified in F1, 2026-11-17). Inside: `ec2-user` crontab (Fri and Sat 04:00 UTC), Python 3.10.20, conda `wayakit_env`, Chrome 146. Registered in SSM (agent 3.3.4108.0) | By hours running (today 2 starts per week; stage 2 can last up to 30 h). Amount pending measurement in Cost Explorer |
| EBS volume (instance root) | 25 GB gp3 | By hand, with the instance | **Unencrypted.** It is the only place where the CSVs, the `.joblib` models, `last_full_run.txt` and the logs live | Pending measurement (gp3 price per GB-month) |
| Root volume snapshot | `pi-f1-antes-inventario` (tags `Name` and `project=price-intelligence`) | By hand, 2026-11-17 (F1, Mafer's OK) | Backup before the inventory and the plan's changes | ~USD 1 / month |
| Security group | `launch-wizard-1` | By hand (console wizard) | Only opens SSH (22/tcp) to 2 fixed addresses. No other port | No cost |
| Instance profile / IAM role | `Wayakit-EC2-Secrets-Role`, with the managed policies `SecretsManagerReadWrite` and `AmazonSSMManagedInstanceCore` (the latter added on 2026-11-17, F1, Mafer's OK) | By hand | **Too broad**: it can read and write every secret in the account, including `voz-del-cliente/api-keys`. SSM access enabled | No cost |
| IAM role (Scheduler) | `Wayakit-EventBridge-Role`, with the managed policy `AmazonEC2FullAccess` | By hand | **Too broad**: it only needs to start one instance | No cost |
| EventBridge Scheduler | `default` group: `Wayakit-Start-Thursday-Night` (`cron(50 21 ? * 5 *)`) and `Wayakit-Start-Friday-Night` (`cron(50 21 ? * 6 *)`), America/Mexico_City time zone. Target `ec2:startInstances` on `wayakit-app-server` | By hand | Active (paused during the F1 inventory and re-enabled on 2026-11-17). They only start the instance; inside, the `ec2-user` crontab launches `run_pipeline.sh` at 04:00 UTC (22:00 Mexico City) | No cost (free tier) |
| Secret | `prod/wayakit-app`. Keys: `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_API_TOKEN` (read only through `pi_secrets.py`, since F2) and `GEMINI_API_KEY` (no longer read by the F2 code; still read by the pre-F2 code the instance runs) | By hand | Active, no rotation. Since F2 the name and region live only in `pi_secrets.py` (`SECRET_NAME`, `REGION_NAME`). Only PI uses it (Mafer, 2026-11-17). **The Gemini key leaked into the instance logs** (see Gemini) | ~USD 0.40 / month |
| Leftovers in **me-south-1** | Roles `EC2WayakitSecretsAccessRole` and `EventBridge-EC2-Wayakit-Role`; secret `wayakit/test/credentials` (the one used before 2026-04-05) | By hand | **Not inventoried**: the region endpoint did not respond. The roles were last used in March 2026. What to do with them is question 10 of the plan | Unknown |

What **does not exist** today (verified read-only, except where noted):

- No PI S3 bucket (the only bucket in the account belongs to VdC).
- No PI CloudWatch Logs group.
- PI alarms, SNS topics and budgets: not checked in the inventory; none are known. Confirmed in F1 (and later with `infra/check_pi.py`).
- No PI Lambda or EventBridge rule.
- **No GitHub OIDC provider** in IAM.

Inventory inside the instance: **done on 2026-11-17 (F1)**. See `docs/runbook.md` (Trigger and Running by hand), `environment.yml` and the 2026-11-17 rows in `docs/decisions.md`. No stored git credentials.

## What is planned

Nothing in this table exists yet. Each script is run with Mafer's OK, will be idempotent and will have `--dry-run`, as in VdC. Names marked "to be defined" are set in `infra/config.py`.

| Resource | Name | Script | Status | Approx. cost |
|---|---|---|---|---|
| Shared configuration | Profile, region, tag, names and cron | `infra/config.py` | Planned (F3) | — |
| S3 bucket for state and releases | `price-intelligence-<account>`. Prefixes `data/`, `models/`, `state/`, `logs/` and `releases/` (plus the `current` pointer). Encrypted, no public access, lifecycle | `infra/bucket.py` | Planned (F3) | Pending estimate; cents at the expected volume |
| Instance role (least privilege) | To be defined: new role or the current one with a new policy. Replaces `SecretsManagerReadWrite` | `infra/instance_role.py` | Planned (F3); the Bedrock permission is needed from F2 | No cost |
| EC2 instance | **Adopts** `wayakit-app-server` or **creates it** (`--create`) from AL2023 with a userData that runs `infra/bootstrap.sh` (conda, Chrome, systemd, SSM agent). Requires IMDSv2 | `infra/ec2_instance.py` + `infra/bootstrap.sh` | Planned (F3) | Same as today, fewer hours: one run a month plus the deploy starts |
| Services on the instance | `pi-deploy.service` (installs the release) and `pi-pipeline.service` (runs the pipeline, except in "deploy only" mode; the mechanism to signal that mode is to be defined) | `infra/bootstrap.sh` | Planned (F3) | — |
| EventBridge Scheduler | `price-intelligence` group with a monthly schedule, **last Thursday of each month**: `cron(50 21 ? * 5L *)`. Time and time zone pending (today Mexico City; Asia/Riyadh?). The two `default` schedules are retired with the `RETIRED` list | `infra/schedule.py` | Planned (F3) | No cost (free tier) |
| Scheduler role | To be defined. Only `ec2:StartInstances` on this instance. Replaces `Wayakit-EventBridge-Role` | `infra/scheduler_role.py` | Planned (F3) | No cost |
| GitHub OIDC provider + deploy role | Role to be defined, with trust limited to `repo:<org>/wayakit-price-prediction:ref:refs/heads/main`. Used by `deploy.yml` (no long-lived keys) | `infra/deploy_role.py` | Planned (F3, used in F4). Still need to know who is admin of the GitHub organization (question 8) | No cost |
| Bedrock access | `bedrock:InvokeModel` (Converse needs nothing else; no streaming) on `arn:aws:bedrock:eu-north-1::foundation-model/deepseek.v3.2` (primary), on the Haiku 4.5 inference profile `arn:aws:bedrock:eu-north-1:<account>:inference-profile/eu.anthropic.claude-haiku-4-5-20251001-v1:0` and on the Haiku foundation model in the EU regions that profile routes to (`arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0`, restricted to EU regions); or on application inference profiles with the `project = price-intelligence` tag if `infra/bedrock_profile.py` creates them | `infra/instance_role.py` (+ `infra/bedrock_profile.py` if application profiles are used) | Planned (F3). **Required before the F2 code runs on the instance**: without it, stage 2 stops at the Bedrock preflight (exit 1, nothing uploaded). See the note below | Per use; see "External services" |
| Apify key | A new key inside `prod/wayakit-app` (key name to be defined) | Manual step (value in the console, by Mafer) | Planned (F6) | No additional cost |
| End-of-run notification | SNS topic published from the `trap EXIT` (name to be defined) | To be defined | Planned (F3) | No cost (first 1,000 emails) |
| Verification | Read-only check (`[OK]`, `[FAIL]`, `[WARN]`); covers the test "change a schedule from code and see it in AWS" | `infra/check_pi.py` | Planned (F3) | — |
| Alarms and budget | — | `infra/alarms.py`, `infra/budget.py` | Outside the tickets' timeframe; meanwhile, the SNS notification | — |

Note on Bedrock: the current decision (2026-11-16) is DeepSeek V3.2 as primary and Haiku 4.5 as fallback, implemented in F2 (`scraper/services/ai_service.py`). The model IDs can be switched to application inference profiles without a code change with the environment variables `PI_RELEVANCE_PRIMARY_MODEL` and `PI_RELEVANCE_FALLBACK_MODEL`. Whether Bedrock allows tagged application profiles on DeepSeek is still to be verified (F3); if not, cost is split by modelId. Whatever is decided is recorded in `docs/decisions.md`.

Outside the scripts (manual steps):

| Resource | Where | Who | Status |
|---|---|---|---|
| DeepSeek V3.2 and Qwen3 agreements (EULA) | Bedrock, eu-north-1 | Accepted with the first invocation of the comparison test, with Mafer's authorization | Done (2026-11-16) |
| Upgrade the Apify plan (VdC account) | Apify Console | Mafer | Decided; tier and date pending |
| Apify key value | Secrets Manager, `prod/wayakit-app` | Mafer, console | Planned (F6) |
| Volume snapshot before starting the instance | EC2 | With Mafer's OK | Done (2026-11-17) |
| **Revoke or rotate the Gemini key** (leaked in the logs) and clean up `logs/cron.log` and `logs/pipeline_execution.log` on the instance | Google (key's project: pending identification) and instance | IT / Mafer | **Urgent** (confirmed in F1, 2026-11-17). Remove `GEMINI_API_KEY` from the secret when the migration to Bedrock is finished (F2) |
| GitHub environment and permissions for OIDC | GitHub, Wayakit organization | Mafer (organization admin) | Pending: the repo is in a personal account (question 8) |

## External services

### Odoo (KSA)

**Confirm the company before reading or writing.** The database PI uses is KSA only (Mafer, 2026-11-17). No read filters by `company_id`; since F2 the upload checks, before any write, that the API user sees exactly one company (`res.company`) and exits 1 if it sees more (`pi_odoo.get_single_company()`).

Connection: XML-RPC with an API token, credentials in `prod/wayakit-app` read by `pi_secrets.get_odoo_credentials()`; authentication in `pi_odoo.connect()` (exits 1 if Odoo returns no uid).

| Operation | Model | Where | Note |
|---|---|---|---|
| Reads | `product.master` (active, without Maritime or Defense) | `ml_model/odoo_api_products.py` (`fetch_products`) | Wayakit products and costs (`unit_cost_sar`). 0 records → exit 1 |
| Reads | `sale.order.line` (`order_id.state = sale`, `name like '[FP-%'`) | `ml_model/odoo_api_cotizations.py` (`fetch_sale_lines`) | Last 12 rolling months up to the run date (`sales_window()`, since F2). 0 lines → exit 1 |
| Reads | `competitor.product` (all records) | `ml_model/odoo_api_competitor_products.py` (`fetch_competitor_products`) | Competitor products loaded in Odoo. 0 records → exit 1 |
| Reads | `res.company` (visible companies) | `pi_odoo.get_single_company()`, called by the upload | Company guard before any write (since F2) |
| Writes | `product.price.suggestion` (`create`; in full mode, then `read` + `unlink` of the previous IDs only) | `ml_model/odoo_api_price_suggestion.py` (`upload`, `archive_and_unlink`) | Since F2: create first, then delete the previous IDs saved in `logs/odoo_previous_suggestion_ids.json`; `--dry-run` and `--cleanup-previous`. See `docs/runbook.md`, *Re-uploading suggestions* |
| Writes | `product.price.suggestion.history` (`create`) | `ml_model/odoo_api_price_suggestion.py` (`archive_and_unlink`) | Copy of the previous suggestions (full mode and `--cleanup-previous` only) |

`scraper/odoo_api_connection_products.py` no longer connects to Odoo (since F2): it only reads `ml_model/wayakit_products.csv`.

The final Pets upload is reviewed by Puya and Luisa before writing to Odoo KSA (F8).

### Apify

| Field | Value |
|---|---|
| Account | VdC's (Mafer's decision). The Starter plan is upgraded to cover PI's spend; the new tier is pending |
| Actors | `junglee/amazon-crawler` (Amazon KSA, amazon.sa) and `saswave/noon-product-scraper` (Noon KSA), the same ones VdC uses |
| Approx. cost | ~USD 14 / month: Amazon ~USD 11 (55 keywords × 50 results at USD 0.004) and Noon ~USD 2.5. Do not pass `countryCode` or `zipCode` to junglee (it would add ~USD 137 / month) |
| Per-run cap | Pending (proposal: USD 20; question 5) |
| Calibration run | Done on 2026-11-16, keyword "pet urine odor remover", ~USD 0.08 |
| Token | Today PI has no Apify token. It will be stored in `prod/wayakit-app` (F6). Whether it is a token separate from VdC's within the same account is to be defined |
| Status | Planned (F6). Mumzworld and SACO stay on Selenium |

Note: PI shares the account's monthly spending limit with VdC. The account limit and the monthly guard are in the Apify section of `docs/runbook.md` in the `voz-del-cliente` repo.

### Amazon Bedrock (product relevance)

| Use | Model | Where | Price (USD per 1M tokens, input / output) | Per month (approx.) |
|---|---|---|---|---|
| Primary | DeepSeek V3.2 (`deepseek.v3.2`) | eu-north-1, on-demand | 0.74 / 2.22 | ~USD 4 |
| Fallback (only if DeepSeek fails or returns invalid JSON) | Claude Haiku 4.5 (`eu.` profile) | EU inference profile | 1.10 / 5.50 | ~USD 6.3 if used for everything |

Status: **implemented in the code (F2), not yet on the instance** (it needs the F3 permission). Converse with `temperature=0`, JSON output forced with `outputConfig` (JSON schema, no `toolConfig`), batches of up to 25 titles and a 120 s read timeout, with the instance role (no key in the secret). No new resource was created in F2: DeepSeek's EULA was accepted in the 2026-11-16 test and the Haiku `eu.` profile was already enabled for VdC. Quota in eu-north-1: 10,000 requests/min per model. Details of the comparison test in section 5 of [plan.md](plan.md).

### Gemini (being retired)

| Field | Value |
|---|---|
| Use today | Relevance (`gemini-2.5-flash-lite`) and wipes unit extraction (`gemini-2.5-pro`), in `scraper/services/ai_service.py:54-55` |
| Credential | `GEMINI_API_KEY` in `prod/wayakit-app` |
| Risk | **Confirmed (F1):** the key is in plain text in `logs/cron.log` (72 lines) and `logs/pipeline_execution.log` (87 lines) on the instance, because it goes in the URL and HTTP errors are logged with it. It does not appear in `~/logs/wayakit_app.log*`. Revoke or rotate now and notify IT |
| Cost | Not documented (pending: identify the Google project and its billing) |
| Status | Removed from the code in F2 (Bedrock). Still used by the pre-F2 code on the instance until F2 is deployed. Afterwards: **revoke the key**, remove `GEMINI_API_KEY` from the secret and clean the logs |

## Permissions by role

| Role | Today | Planned |
|---|---|---|
| Instance role | `Wayakit-EC2-Secrets-Role`: `SecretsManagerReadWrite` (every secret in the account, read and write) and `AmazonSSMManagedInstanceCore` (since 2026-11-17) | (F3) `secretsmanager:GetSecretValue` only on the ARN of `prod/wayakit-app`; the PI bucket; `AmazonSSMManagedInstanceCore`; `cloudwatch:PutMetricData`; publish to the notification SNS topic. `bedrock:InvokeModel` on DeepSeek V3.2 and Haiku 4.5 (see *Bedrock access*; F3, required before deploying F2) |
| Scheduler role | `Wayakit-EventBridge-Role`: `AmazonEC2FullAccess` | (F3) Only `ec2:StartInstances` on this instance |
| Deploy role (GitHub Actions) | Does not exist | (F3/F4) OIDC trust only for `main` of the repo; publish to the bucket's `releases/`, move the `current` pointer and start the instance in "deploy only" mode. Exact scope to be defined in `infra/deploy_role.py` |
| Roles in me-south-1 | `EC2WayakitSecretsAccessRole`, `EventBridge-EC2-Wayakit-Role`: permissions not inventoried | To be decided (question 10) |
