# Resources

Inventory of what exists for Price Intelligence (PI). Wayakit account, region **eu-north-1** (Stockholm) unless stated otherwise. Updated on **2026-11-18** (F3 scripts written on branch `f3-infra`; none of them has been run against AWS yet).

`<account>` = Wayakit's AWS account ID (`aws sts get-caller-identity`). It is never written in the repo.

**Rule:** when adding a resource → script in `infra/` + row here + step in `docs/runbook.md` + row in `docs/decisions.md` if applicable. Every pipeline or infrastructure change updates these docs in the same commit.

Overall status: **everything that exists today was created by hand** (console or CLI). Since F3 the `infra/` scripts exist (see [What is scripted (F3)](#what-is-scripted-f3)), but **none has been applied**: nothing has been created or changed in AWS as part of the plan yet ([plan.md](plan.md)). Each script is applied only with Mafer's OK, after its `--dry-run` (order and steps in `docs/runbook.md`, *Infrastructure as code (F3)*). The inventory was done in read-only mode with the `wayakit-vdc` AWS CLI profile. It was not verified which tags the current resources have, besides `Name`. New resources carry the `project = price-intelligence` tag.

## What exists today

| Resource | Name | How it was created | Status | Approx. cost |
|---|---|---|---|---|
| EC2 instance | `wayakit-app-server` (`Name` tag). t3.medium, Amazon Linux 2023, key pair `wayakit-stockholm-key`. No userData, no Elastic IP, IMDSv2 not verified | By hand | `stopped` almost always. The schedules start it; it shuts itself down with `sudo shutdown -h +1` in the `trap EXIT` (`run_pipeline.sh:52` in e25e2c9). Runs the **`server-improvement`** branch (verified in F1, 2026-11-17). Inside: `ec2-user` crontab (Fri and Sat 04:00 UTC), Python 3.10.20, conda `wayakit_env`, Chrome 146. Registered in SSM (agent 3.3.4108.0) | By hours running (today 2 starts per week, one per stage of the pre-F2 code; the scraping alone can last up to 30 h). Amount pending measurement in Cost Explorer |
| EBS volume (instance root) | 25 GB gp3 | By hand, with the instance | **Unencrypted.** It is the only place where the CSVs, the `.joblib` models and the logs live (and, with the pre-F2 code it runs today, the state files `run_mode.txt` and `last_full_run.txt`; the F3 code has none) | Pending measurement (gp3 price per GB-month) |
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
- No Bedrock application inference profile for PI (the code uses the plain model IDs).
- None of the F3 resources below.

Inventory inside the instance: **done on 2026-11-17 (F1)**. See `docs/runbook.md` (Trigger and Running by hand), `environment.yml` and the 2026-11-17 rows in `docs/decisions.md`. No stored git credentials.

## What is scripted (F3)

**Rows marked "Applied" exist in AWS (runbook steps 1–6 on 2026-11-18, cutover on 2026-11-22); the others (`--create`) do not.** Every script is in `infra/` (branch `f3-infra`), is run from the repo root as `python -m infra.<name>`, can be run more than once (if the resource exists it is updated, never duplicated), accepts `--dry-run` (read calls only; prints what it would change) and never prints the account ID, instance IDs or a full email. Shared names, prefixes and the cron are only in `infra/config.py`. Status of every row: **Script ready (F3), not applied — needs Mafer's OK.** Order, dry-run and apply steps: `docs/runbook.md`, *Infrastructure as code (F3)*.

| Resource | Name | Script | Status | Approx. cost |
|---|---|---|---|---|
| Shared configuration | Profile `wayakit-vdc` (or `AWS_PROFILE`; `default` is refused), region `eu-north-1`, tag `project=price-intelligence`, every name, the cron and the ARN builders. The account ID comes from STS at run time | `infra/config.py`, `infra/_aws.py` | Ready (F3); creates nothing | — |
| S3 bucket for state and releases | `price-intelligence-<account>`, eu-north-1. SSE-S3 (AES256) with bucket key (inert with SSE-S3), SSE-C uploads blocked, Block Public Access (all four), `BucketOwnerEnforced`, versioning, TLS-only bucket policy. Lifecycle: noncurrent versions expire after 30 days, `logs/` after 180 days, incomplete multipart uploads aborted after 7 days, expired delete markers removed; `releases/` has no rule and is not pruned (archives are a few hundred KB; the instance keeps the last 5 release folders on disk). Prefixes `data/`, `models/`, `state/`, `logs/`, `releases/`, created by the first writer. F4 keys: see [Releases and deploys (F4)](#releases-and-deploys-f4). Every call passes `ExpectedBucketOwner` | `infra/bucket.py` | Applied 2026-11-18 (runbook steps 1–6, Mafer's OK); `check_pi` OK | Cents per month at the expected volume (storage and requests only) |
| Instance role and instance profile | `price-intelligence-instance` (role and profile with the same name). **New role**; `Wayakit-EC2-Secrets-Role` is not edited or deleted. Permissions in [Permissions by role](#permissions-by-role). `--audit-old-role`: read-only CloudTrail report of who used the old role in the last 90 days | `infra/instance_role.py` | Applied 2026-11-18 (runbook steps 1–6, Mafer's OK); `check_pi` OK. On the instance since 2026-11-22. **Required before F2 runs on the instance** (`bedrock:InvokeModel`) | No cost |
| Scheduler role | `price-intelligence-scheduler`. Replaces `Wayakit-EventBridge-Role` (not edited or deleted) | `infra/scheduler_role.py` | Applied 2026-11-18 (runbook steps 1–6, Mafer's OK); `check_pi` OK | No cost |
| GitHub OIDC provider + deploy role | Provider `token.actions.githubusercontent.com` (audience `sts.amazonaws.com`; kept if it already exists, never deleted) and role `price-intelligence-deploy`, assumable only from `main` of `Wayakit-Biotechnology-in-Hygiene/wayakit-price-prediction` | `infra/deploy_role.py` | Applied 2026-11-18 with the F3 policy (`pi-mode` tag); **F4 policy re-applied 2026-11-22** (Mafer's OK; `check_pi --only iam oidc`: 33 OK, 0 FAIL) | No cost |
| Bedrock application inference profiles | `price-intelligence-deepseek-v3-2` (copy of the foundation model `deepseek.v3.2`, primary) and `price-intelligence-haiku-4-5` (copy of the system profile `eu.anthropic.claude-haiku-4-5-20251001-v1:0`, fallback), tagged. Their ARNs go to the instance as `PI_RELEVANCE_PRIMARY_MODEL` / `PI_RELEVANCE_FALLBACK_MODEL`. If AWS rejects a profile copied from `deepseek.v3.2`, the script prints `[FALLBACK]` and the primary stays the plain ID `deepseek.v3.2` | `infra/bedrock_profiles.py` | Applied 2026-11-18 (runbook steps 1–6, Mafer's OK); `check_pi` OK | The profiles cost nothing; invocations at the model price (see "External services") |
| Failure alerts (SNS) | Topic `price-intelligence-alerts`, tagged, SSE with the AWS-managed key `alias/aws/sns`. With `--email <address>`: one email subscription (the address is passed at run time, never stored, printed masked; it must be confirmed from the email AWS sends). Published by the `run_pipeline.sh` EXIT trap when a run fails | `infra/alerts.py` | Applied 2026-11-18 (runbook steps 1–6, Mafer's OK); `check_pi` OK; the email subscription must be confirmed from the AWS email | USD 0 (first 1,000 emails a month free) |
| EventBridge Scheduler | Group `price-intelligence` (tagged; single schedules cannot be tagged) and schedule `price-intelligence-monthly`: `cron(50 21 ? * 5L *)`, `America/Mexico_City` (last Thursday of each month, 21:50), flexible window OFF, universal target `ec2:startInstances` on the instance found by Name tag, role `price-intelligence-scheduler`, no retries. **Created DISABLED**; `--enable` turns it on. The two `default` schedules are **disabled** (all their fields kept) by the same apply; `--delete-retired` deletes them | `infra/schedule.py` | Applied 2026-11-22: monthly schedule ENABLED, the two `default` schedules DISABLED (kept for rollback) | No cost (free tier) |
| EC2 instance (adopt) | `wayakit-app-server`, found by Name tag: tags `project` and `Name`, IMDSv2 required (hop limit 2), instance profile `price-intelligence-instance` associated (replaces the current one; prints the previous profile for `--rollback-profile`). Never stops, starts, terminates or resizes it | `infra/ec2_instance.py` | Applied 2026-11-22 | None by itself; fewer running hours (one monthly run plus maintenance) |
| EC2 instance (`--create`) | New t3.medium, latest AL2023, IMDSv2, encrypted 30 GB gp3, shutdown behavior `stop`, no key pair (SSM only), userData that clones `main` and runs `bootstrap.sh`. Refuses if an instance with the Name tag exists | `infra/ec2_instance.py --create` | Documented and unit-tested only; not planned to run | t3.medium on demand while running + EBS while stopped |
| Services on the instance | `pi-pipeline.service` (systemd oneshot, `ec2-user`, enabled, not started): on every boot runs `pi_boot_guard.py` and, only on the last Thursday of the month between 21:40 and 23:00 Mexico City, `run_pipeline.sh` with no arguments (one run, no stages; any argument exits 1). `/etc/price-intelligence.env` (root, 0644, **no secrets**): `PI_LOG_DIR`, `PI_ALERTS_TOPIC_ARN`, `PI_RELEVANCE_PRIMARY_MODEL`, `PI_RELEVANCE_FALLBACK_MODEL`. Conda env `wayakit_env` from `environment.yml`, Chrome, SSM agent. Removes the `ec2-user` crontab lines that launch `run_pipeline.sh` (backup in the log folder) | `infra/instance/bootstrap.sh`, `infra/instance/pi-pipeline.service`, `infra/instance/pi_boot_guard.py` (run on the instance over SSM) | Applied 2026-11-22 | — |
| Verification | Read-only check (`Describe*`/`Get*`/`List*` only) of every row above: `[OK]`, `[WARN]`, `[FAIL]` per setting, exit 1 on any FAIL. Any IAM action outside the spec on a PI role is a FAIL; every S3 read passes `ExpectedBucketOwner` (the account from STS), so a bucket with PI's name in another account is never read. Covers the test "change a schedule from code and see it in AWS" | `infra/check_pi.py` | Ready (F3); read-only, run it after each apply | Fractions of a cent (a handful of S3 reads) |

## Releases and deploys (F4)

**Code ready on branch `f4-cicd` (2026-11-22); nothing of this exists in AWS, GitHub or the instance until the [F4 rollout](runbook.md#f4-rollout-once) is done with Mafer's OK.** How to operate it: [runbook.md, Deploy and rollback](runbook.md#deploy-and-rollback). Why: [decisions.md](decisions.md) (2026-11-22 rows).

| Resource | Name / location | Created by | Status |
|---|---|---|---|
| Release archives | `releases/<sha>.tar.gz` (`git archive <sha> \| gzip -n`) and `releases/<sha>.tar.gz.sha256` (`sha256sum` format) in the PI bucket. Not pruned (a few hundred KB each); overwritten versions expire with the bucket lifecycle (30 days) | `infra/release.py deploy` (GitHub Actions, deploy role) | Planned (F4 rollout) |
| Release pointers | `releases/current` (`<sha>` + newline: the release **every** boot installs) and `releases/previous` (the one before the last deploy). Moved back to the old value if a deploy fails | `infra/release.py` | Planned (F4 rollout) |
| Deploy request | `releases/deploy-request.json`: `{"sha", "request_id", "requested_at", "expires_at"}` (epoch seconds, valid 30 minutes). While valid, the next boot is **deploy-only** (install, write the result, power off). Closed (`expires_at = requested_at`, `"closed": true`) when the deploy ends | `infra/release.py` | Planned (F4 rollout) |
| Deploy results | `state/deploys/<request_id>.json`: `{"request_id", "sha", "result": "ok"\|"failed", "reason", "finished_at", "previous"}`. Written by the instance; the deploy role can read it but never write it | `infra/instance/pi_deploy.py` (instance role) | Planned (F4 rollout) |
| Deployer on the instance | `/opt/price-intelligence/` (root): `pi_deploy.py` and a copy of `pi_boot_guard.py`, outside the releases so a bad release cannot break the deployer; updated from each new release | `infra/instance/bootstrap.sh`, then `pi_deploy.py` itself | Installed 2026-11-22 (bootstrap over SSM) |
| `pi-deploy.service` | systemd oneshot, root, enabled, runs on **every boot** before `pi-pipeline.service` (`/usr/bin/python3 /opt/price-intelligence/pi_deploy.py`, 10 min timeout). `pi-pipeline.service` is ordered after it (ordering only: a failed deploy never blocks the monthly run, which then uses the previous release) | `infra/instance/pi-deploy.service`, installed by `bootstrap.sh` | Installed 2026-11-22 (bootstrap over SSM) |
| Release folders | `/home/ec2-user/pi-releases/<sha>` (ec2-user; the last 5 kept, always including the current and previous targets). `/home/ec2-user/wayakit-price-prediction` becomes a **symlink** to the current one; the old git checkout is renamed once to `/home/ec2-user/wayakit-price-prediction.pre-f4` (never pruned; rollback). Deploy log: `deploy.log` in the log folder (`PI_LOG_DIR`). The release folders are owned by `ec2-user`, and root (`pi_deploy.py`) copies the unit files and the deployer from the active release into `/etc/systemd/system` and `/opt/price-intelligence`: whoever can write a release folder can change what root runs on the next boot. That is no new escalation (`ec2-user` already has passwordless `sudo` on Amazon Linux), but it is why releases come only from S3 through the deploy role | `pi_deploy.py` | Planned: created by the first F4 deploy (when `main` receives the code) |
| GitHub workflow | `.github/workflows/deploy.yml` (`Deploy`): after the `CI` workflow succeeds for a push to `main`, or by hand (`workflow_dispatch` with `sha`, rollback). Actions pinned by commit SHA; `contents: read`, `id-token: write`; one deploy at a time (`concurrency: pi-deploy`) | Repo (lead merges) | Planned (F4 rollout) |
| GitHub secret | `PI_DEPLOY_ROLE_ARN` (repository secret): ARN of `price-intelligence-deploy`, the only AWS value GitHub holds. No access keys anywhere (OIDC, 1 h sessions) | Mafer, GitHub settings | Planned (F4 rollout) |

## What is still planned

| Resource | Name | Script | Status | Approx. cost |
|---|---|---|---|---|
| CloudWatch metrics | Namespace `PriceIntelligence` (the instance role may already publish to it; nothing publishes yet) | To be defined | Planned | Per metric |
| Apify key | A new key inside `prod/wayakit-app` (key name to be defined) | Manual step (value in the console, by Mafer) | Planned (F6) | No additional cost |
| Alarms and budget | — | `infra/alarms.py`, `infra/budget.py` | Outside the tickets' timeframe; meanwhile, the SNS failure alert. Note: CloudWatch alarms cannot publish to a topic encrypted with `alias/aws/sns`; they would need a customer-managed key or another topic | — |

Note on Bedrock: the current decision (2026-11-16) is DeepSeek V3.2 as primary and Haiku 4.5 as fallback, implemented in F2 (`scraper/services/ai_service.py`). The model IDs switch to the application inference profiles without a code change through `PI_RELEVANCE_PRIMARY_MODEL` and `PI_RELEVANCE_FALLBACK_MODEL` (written to `/etc/price-intelligence.env` by `bootstrap.sh`). Whether Bedrock allows a tagged application profile copied from DeepSeek is checked by the first real run of `infra/bedrock_profiles.py`; if not, the primary stays `deepseek.v3.2` and its cost is split by model ID in Cost Explorer.

Outside the scripts (manual steps):

| Resource | Where | Who | Status |
|---|---|---|---|
| DeepSeek V3.2 and Qwen3 agreements (EULA) | Bedrock, eu-north-1 | Accepted with the first invocation of the comparison test, with Mafer's authorization | Done (2026-11-16) |
| Upgrade the Apify plan (VdC account) | Apify Console | Mafer | Decided; tier and date pending |
| Apify key value | Secrets Manager, `prod/wayakit-app` | Mafer, console | Planned (F6) |
| Volume snapshot before starting the instance | EC2 | With Mafer's OK | Done (2026-11-17) |
| **Revoke or rotate the Gemini key** (leaked in the logs) and clean up `<repo>/logs/cron.log` and `<repo>/logs/pipeline_execution.log` on the instance | Google (key's project: pending identification) and instance | IT / Mafer | **Urgent** (confirmed in F1, 2026-11-17). Remove `GEMINI_API_KEY` from the secret when the migration to Bedrock is finished (F2) |
| GitHub secret `PI_DEPLOY_ROLE_ARN` and (optional) environment with manual approval | GitHub, repository settings | Mafer (organization admin) | Secret: created 2026-11-22 (Mafer). Manual approval: not used by default; question 8 still open (enabling it needs the environment, a new OIDC subject and a deploy role re-apply) |
| Confirm the alerts email subscription | The confirmation email AWS sends after `python -m infra.alerts --email <address>` | The owner of the address (Mafer) | Pending until `alerts.py` is applied (F3). Until it is confirmed, alerts reach nobody |
| Review the old instance role audit | Output of `python -m infra.instance_role --audit-old-role` | Mafer | Pending (F3), before switching the instance profile |
| Restore or delete the retired schedules | EventBridge Scheduler, group `default` | With Mafer's OK | They are disabled (not deleted) by `infra/schedule.py`; deleted with `--delete-retired` only after the first successful monthly run |

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
| Writes | `product.price.suggestion` (`create`; in full mode, then `read` + `unlink` of the previous IDs only) | `ml_model/odoo_api_price_suggestion.py` (`upload`, `archive_and_unlink`) | Since F2: create first, record the new IDs, check they exist (`search_count`), then delete the previous IDs saved in `odoo_previous_suggestion_ids.json` in the log folder (`PI_LOG_DIR`, default `<repo>/../logs`); `--dry-run` and `--cleanup-previous`. XML-RPC calls time out after 300 s. See `docs/runbook.md`, *Re-uploading suggestions* |
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

Status: **implemented in the code (F2), not yet on the instance** (it needs the F3 instance role, `infra/instance_role.py`, and optionally the F3 application profiles, `infra/bedrock_profiles.py`). Converse with `temperature=0`, JSON output forced with `outputConfig` (JSON schema, no `toolConfig`), batches of up to 25 titles and a 120 s read timeout, with the instance role (no key in the secret). No new resource was created in F2: DeepSeek's EULA was accepted in the 2026-11-16 test and the Haiku `eu.` profile was already enabled for VdC. Quota in eu-north-1: 10,000 requests/min per model. Details of the comparison test in section 5 of [plan.md](plan.md).

### Gemini (being retired)

| Field | Value |
|---|---|
| Use today | Relevance (`gemini-2.5-flash-lite`) and wipes unit extraction (`gemini-2.5-pro`), in `scraper/services/ai_service.py:54-55` |
| Credential | `GEMINI_API_KEY` in `prod/wayakit-app` |
| Risk | **Confirmed (F1):** the key is in plain text in `<repo>/logs/cron.log` (72 lines) and `<repo>/logs/pipeline_execution.log` (87 lines) on the instance, because it goes in the URL and HTTP errors are logged with it. It does not appear in `~/logs/wayakit_app.log*`. Revoke or rotate now and notify IT |
| Cost | Not documented (pending: identify the Google project and its billing) |
| Status | Removed from the code in F2 (Bedrock). Still used by the pre-F2 code on the instance until F2 is deployed. Afterwards: **revoke the key**, remove `GEMINI_API_KEY` from the secret and clean the logs |

## Permissions by role

The F3 column is what the scripts in `infra/` write (pure builders, pinned by the unit tests and checked by `infra/check_pi.py`). **Not applied yet.** No role gets a `*` action; `Resource: "*"` appears only where AWS requires it (documented in the row). Extra inline or managed policies found on a PI role are reported with `[WARN]` by the apply scripts and never removed; `infra/check_pi.py` reports any action outside the spec (in any policy) as `[FAIL]`, and an unexpected AWS-managed policy too.

| Role | Today | F3 (script ready, not applied) |
|---|---|---|
| Instance role | `Wayakit-EC2-Secrets-Role`: `SecretsManagerReadWrite` (every secret in the account, read and write) and `AmazonSSMManagedInstanceCore` (since 2026-11-17). Kept untouched as the rollback | `price-intelligence-instance` (trust: `ec2.amazonaws.com` only; 1 h sessions), inline policy `price-intelligence-instance-access`: `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:eu-north-1:<account>:secret:prod/wayakit-app-??????`; `s3:ListBucket` on the PI bucket; `s3:GetObject`/`PutObject`/`DeleteObject` only on the data prefixes `bucket/data/*`, `bucket/models/*`, `bucket/state/*` and `bucket/logs/*` (statement `ReadWritePiObjects`); only `s3:GetObject` on `bucket/releases/*` (statement `ReadPiReleases`: releases and `releases/current` are written by the deploy role alone); `cloudwatch:PutMetricData` with `Resource: "*"` (the action has no resource-level permissions) limited by the condition `cloudwatch:namespace = PriceIntelligence`; `sns:Publish` on `price-intelligence-alerts` (no KMS statement: the AWS-managed `alias/aws/sns` key policy covers SNS use in the account, to be confirmed with the first alert); `bedrock:InvokeModel` on the DeepSeek V3.2 foundation model in eu-north-1 (no condition, so the plain ID also works), on the two application profiles that exist, on the Haiku `eu.` system profile, and on the Haiku 4.5 foundation model in the EU regions that profile routes to (read from AWS at run time, not a wildcard) only when called through a Haiku profile (`bedrock:InferenceProfileArn` condition). Plus the managed `AmazonSSMManagedInstanceCore` |
| Scheduler role | `Wayakit-EventBridge-Role`: `AmazonEC2FullAccess`. Kept untouched; unused once the retired schedules are disabled | `price-intelligence-scheduler` (trust: `scheduler.amazonaws.com` with `aws:SourceAccount = <account>`), inline policy `price-intelligence-scheduler-access`: only `ec2:StartInstances` on `arn:aws:ec2:eu-north-1:<account>:instance/<instance-id>` (ID resolved by Name tag; re-run the script if the instance is replaced) |
| Deploy role (GitHub Actions, F4) | Does not exist | `price-intelligence-deploy` (trust: the GitHub OIDC provider, `sts:AssumeRoleWithWebIdentity`, `StringEquals` on `aud = sts.amazonaws.com` and `sub = repo:Wayakit-Biotechnology-in-Hygiene/wayakit-price-prediction:ref:refs/heads/main`; 1 h sessions), inline policy `price-intelligence-deploy-access`. **F4 policy (branch `f4-cicd`, not applied yet; replaces the F3 one, which had `s3:GetObject`/`PutObject` on `releases/current` and `ec2:CreateTags` for the tag key `pi-mode`):** `s3:PutObject` on `bucket/releases/*` (archives, `.sha256`, `current`, `previous`, `deploy-request.json`; statement `PublishReleases`); `s3:GetObject` only on `bucket/releases/current` and `bucket/state/deploys/*` (`ReadPointerAndDeployResults`); `s3:ListBucket` on the bucket only with `StringLike s3:prefix` = `releases/current` or `state/deploys/*` (`ListPointerAndDeployResults`: without it a missing key answers AccessDenied, not "not found"); `ec2:StartInstances` on the instance (`StartPiInstance`); `ec2:DescribeInstances` with `Resource: "*"` (`FindPiInstance`: the action has no resource-level permissions; read-only, finds the instance and checks it is stopped). Nothing else: no `DeleteObject` (S3 archives are not pruned), no write on `state/` (it cannot forge a deploy result), no `CreateTags`, no `StopInstances` |
| Operator (whoever runs `infra/`, SSO profile `wayakit-vdc`) | Admin-level SSO role | Needs, besides the create/update/read calls of each script: `iam:PassRole` on `price-intelligence-scheduler` (`schedule.py`), on `Wayakit-EventBridge-Role` (`schedule.py` disabling the retired schedules, an UpdateSchedule that keeps their role) and on `price-intelligence-instance` (`ec2_instance.py`), `cloudtrail:LookupEvents` (`--audit-old-role`). Without `iam:PassRole` the apply stops with `AccessDenied` (nothing half-done). Check that the SSO role has them before applying (an administrator role does) |
| Roles in me-south-1 | `EC2WayakitSecretsAccessRole`, `EventBridge-EC2-Wayakit-Role`: permissions not inventoried | To be decided (question 10) |

Notes:

- The secret pattern is `prod/wayakit-app-??????` (`config.secret_arn_pattern`; decided 2026-11-18): Secrets Manager always appends `-` plus exactly 6 random characters to the name, and each `?` matches one character, so it matches only this secret and never another one whose name starts with `prod/wayakit-app-` (the earlier `-*` would). `check_pi.py` probes it with a 6-character suffix and checks that `prod/other` and `prod/wayakit-app-other` are denied.
- If the instance's EBS volume used a customer-managed KMS key, `ec2:StartInstances` would also need `kms:CreateGrant` for the scheduler and deploy roles. It is not granted: the root volume is unencrypted today and `--create` uses the default `aws/ebs` key.
