#!/bin/bash
set -o pipefail

# --- CONFIGURATION ---
PROJECT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
CONDA_ENV_NAME="wayakit_env"
# Logs and state in ONE folder outside the code (F4 releases must not lose them); same rule as log_config.py. Absolute (before the cd) and exported so every step uses it, even via a symlinked code folder.
LOG_DIR="${PI_LOG_DIR:-$(dirname "$PROJECT_DIR")/logs}"; case "$LOG_DIR" in /*) ;; *) LOG_DIR="$PWD/$LOG_DIR" ;; esac; export PI_LOG_DIR="$LOG_DIR"
# --- Log File ---
SCRIPT_LOG_FILE="$LOG_DIR/pipeline_execution.log"
# --- IMPORTANT CHANGE: Detect user (ubuntu or ec2-user) ---
if [ -d "/home/ubuntu" ]; then
    CONDA_BASE_PATH=$(conda info --base 2>/dev/null || echo "/home/ubuntu/miniconda3")
else
    CONDA_BASE_PATH=$(conda info --base 2>/dev/null || echo "/home/ec2-user/miniconda")
fi

# --- Data Files ---
ODOO_PRODUCTS_FILE="$PROJECT_DIR/ml_model/wayakit_products.csv"
ANALYSIS_FILE="$PROJECT_DIR/scraper/analysis-odoo.csv"
COMPETITORS_FILE="$PROJECT_DIR/scraper/competitors_complete.csv"

# --- Logging Functions ---
log_message() {
    local message="$1"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    mkdir -p "$LOG_DIR"
    echo "$timestamp - $message" | tee -a "$SCRIPT_LOG_FILE"
}

run_command() {
    local cmd="$1"
    local description="$2"
    CURRENT_STEP="$description"
    log_message "Starting: $description"
    eval "$cmd" 2>&1 | tee -a "$SCRIPT_LOG_FILE"
    if [ ${PIPESTATUS[0]} -eq 0 ]; then
        log_message "Success: $description completed."
        return 0
    else
        log_message "ERROR: $description failed. Check '$SCRIPT_LOG_FILE' for details."
        return 1
    fi
}

# --- Failure alert (F3) ---
# Only bash builtins plus the utilities above (no tail/sed/hostname): the E2E sandbox puts nothing
# else on the PATH, and on the instance fewer moving parts means the alert still goes out when
# something else is broken.
ALERT_LOG_LINES=20
ALERT_LINE_MAX_CHARS=500
ALERT_PUBLISH_TIMEOUT="60s"
CURRENT_STEP="start"  # step being run when the script exits (for the alert); run_command sets it
shopt -s extglob

# Last $1 lines of file $2. mapfile -s skips the head without keeping it in memory (the log is
# appended to forever); the read loop is the fallback for bash < 4 (macOS, tests only).
last_lines() {
    local n="$1" file="$2" total skip line
    local -a lines=()
    [ -f "$file" ] || return 0
    total=$(wc -l < "$file") || return 0
    skip=$(( total > n ? total - n : 0 ))
    if [ "${BASH_VERSINFO[0]}" -ge 4 ]; then
        mapfile -t -s "$skip" lines < "$file"
    else
        while IFS= read -r line || [ -n "$line" ]; do
            lines+=("$line")
            [ "${#lines[@]}" -gt "$n" ] && lines=("${lines[@]:1}")
        done < "$file"
    fi
    [ "${#lines[@]}" -gt "$n" ] && lines=("${lines[@]:${#lines[@]}-n}")
    [ "${#lines[@]}" -gt 0 ] && printf '%s\n' "${lines[@]}"
    return 0
}

# Redacts secret values from the alert's log excerpt (name in any case, kept as written; values
# become <redacted>). The pre-F2 Gemini client put its API key in URLs that ended up in the logs,
# and a library error can echo a header or a JSON body. A secret name is key, token, secret,
# password or authorization, also inside a longer word (api_key, x-api-key, refreshToken).
# - "Rest of the line" form, REDACT_REST_RE: the name followed by anything but a plain `=`:
#   `name: v`, `"name": "v"`, `'name': 'v'`, `\"name\": ...`, `name = v`, `name= v`,
#   `Authorization: Bearer v`. Where such a value ends cannot be told safely (JSON, headers,
#   spaces), so everything after the separator, to the end of the line, is redacted. The leftmost
#   one wins even inside a quoted or plain value (over-redacting is safe; guessing is not).
# - Plain `name=value` form, REDACT_RE (URL query, shell assignment), in the text before that:
#   the value is a shell word, i.e. a run of bare characters and "..." or '...' parts (only an
#   unclosed quote runs to the end of the text: a closing quote never reopens) up to &,
#   whitespace or the end. Text glued to a quote is part of the value, so none of it is left.
# Every redacted value is followed by &, whitespace or the end of the line, and "<redacted>" holds
# no secret name, so a second pass changes nothing (idempotent). Redaction runs before the length
# cap so a cut never leaves half a secret. The regex loop (not ${line//...}) is what lets a value
# start with a quote and keeps the name's case; each pass consumes the match, so it always ends.
REDACT_RE="([kK][eE][yY]|[tT][oO][kK][eE][nN]|[sS][eE][cC][rR][eE][tT]|[pP][aA][sS][sS][wW][oO][rR][dD]|[aA][uU][tT][hH][oO][rR][iI][zZ][aA][tT][iI][oO][nN])=(\"[^\"]*(\"|$)|'[^']*('|$)|[^&[:space:]\"'])+"
REDACT_REST_RE="([kK][eE][yY]|[tT][oO][kK][eE][nN]|[sS][eE][cC][rR][eE][tT]|[pP][aA][sS][sS][wW][oO][rR][dD]|[aA][uU][tT][hH][oO][rR][iI][zZ][aA][tT][iI][oO][nN])([\\\"']*[[:space:]]*:|[\\\"']+[[:space:]]*=|[[:space:]]+=|=[[:space:]])[[:space:]]*"
redact_secrets() {
    local line rest out match tail
    while IFS= read -r line || [ -n "$line" ]; do
        rest="$line"; out=""; tail=""
        if [[ $line =~ $REDACT_REST_RE ]]; then
            match="${BASH_REMATCH[0]}"
            # First occurrence of the matched text == the leftmost match position.
            rest="${line%%"$match"*}"
            tail="$match<redacted>"
        fi
        while [[ $rest =~ $REDACT_RE ]]; do
            match="${BASH_REMATCH[0]}"
            out+="${rest%%"$match"*}${BASH_REMATCH[1]}=<redacted>"
            rest="${rest#*"$match"}"
        done
        out+="$rest$tail"
        printf '%s\n' "${out:0:$ALERT_LINE_MAX_CHARS}"
    done
}

# Masks instance IDs in $1 and prints the result: every whole token `i-` + 8 to 17 hex digits
# (delimited by the start or end of the text or by a character that is not a letter or digit)
# becomes <instance-id>. EC2 resource-name host names are `i-<hex>.<region>.compute.internal`, and
# instance IDs never go into messages. A regex loop because ${var//...} cannot count repetitions;
# each pass consumes the ID, so it always ends. The leftmost match is also the first occurrence of
# its text, so ${s%%"$m"*} cuts at the right place.
mask_instance_ids() {
    local s="$1" out="" m
    local re='(^|[^0-9A-Za-z])i-[0-9a-fA-F]{8,17}([^0-9A-Za-z]|$)'
    while [[ $s =~ $re ]]; do
        m="${BASH_REMATCH[0]}"
        out+="${s%%"$m"*}${BASH_REMATCH[1]}<instance-id>"
        # The closing delimiter goes back so it can open the next token (i-a.i-b).
        s="${BASH_REMATCH[2]}${s#*"$m"}"
    done
    printf '%s' "$out$s"
}

# Publishes a short failure message to SNS (email) when PI_ALERTS_TOPIC_ARN is set (the instance
# gets it from /etc/price-intelligence.env). Never fails: a broken alert must not block the
# shutdown. The topic ARN is not logged (it holds the account ID) and neither is the AWS error.
send_failure_alert() {
    local rc="$1" region subject message recent host
    [ -n "${PI_ALERTS_TOPIC_ARN:-}" ] || return 0
    IFS=: read -r _ _ _ region _ <<< "$PI_ALERTS_TOPIC_ARN"
    # The default EC2 host name embeds the private IP (ip-a-b-c-d...) or, with resource-name host
    # names, the instance ID (i-0abc....compute.internal); neither goes into messages.
    host="${HOSTNAME:-unknown}"
    host="${host//+([0-9])[-.]+([0-9])[-.]+([0-9])[-.]+([0-9])/<ip>}"
    host=$(mask_instance_ids "$host")
    recent=$(last_lines "$ALERT_LOG_LINES" "$SCRIPT_LOG_FILE" | redact_secrets)
    subject="Price Intelligence pipeline failed (exit $rc)"
    message="The Price Intelligence pipeline failed and the instance is shutting down.
Failed step: $CURRENT_STEP
Exit code: $rc
Host: $host
Time (UTC): $(date -u '+%Y-%m-%d %H:%M:%S')
Log: $SCRIPT_LOG_FILE

Last $ALERT_LOG_LINES lines of the log (values after key/token/secret/password/authorization redacted):
${recent:-(log not found)}"
    if timeout "$ALERT_PUBLISH_TIMEOUT" aws sns publish --region "$region" --topic-arn "$PI_ALERTS_TOPIC_ARN" \
            --subject "$subject" --message "$message" > /dev/null 2>&1; then
        log_message "Failure alert sent (SNS)."
    else
        log_message "WARNING: could not send the failure alert (SNS). Shutting down anyway."
    fi
    return 0
}

# --- Guaranteed shutdown ---
# Fires on ANY script exit (success or exit 1), guaranteeing that
# the instance always shuts down and does not stay on accruing costs.
# The trap does not call exit, so the script keeps the exit code of the step that failed.
on_exit() {
    local rc="$1"
    # A second signal while the alert is being sent must not skip the shutdown: from here on the
    # signals only interrupt the command running (a no-op handler, not '' which children would
    # inherit as "ignored" and then `timeout` could not stop `aws`).
    trap ':' TERM INT HUP
    [ "$rc" -ne 0 ] && send_failure_alert "$rc"
    log_message "Pipeline finished (code=$rc). Scheduling shutdown in 1 minute."
    sudo shutdown -h +1
}
trap 'on_exit $?' EXIT
# A stop in the middle of a run (systemctl stop, instance stop or shutdown, Ctrl-C, SSM session
# closed) ends with the conventional 128+signal code, so the EXIT trap sees a failure: the alert
# goes out and the log shows the code. Bash runs these after the foreground step returns (under
# systemd, KillMode=control-group signals the step too, so that is at once).
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

# --- Single run (Mafer, 2026-11-18: no stages) ---
# One flow per scheduled boot: Odoo products -> scraping list -> scraping -> Odoo reads -> ML ->
# upload. No arguments: anything else exits 1 (and alerts), so an old crontab line calling
# `--stage 2` or `--monthly` cannot start something unexpected. No state files: nothing carries
# over from one run to the next except the CSVs the steps write. The search keywords come from
# the versioned scraper/modifiers_mapping.csv (via the scraping list); reviewing them happens
# outside the run.
if [ "$#" -ne 0 ]; then
    CURRENT_STEP="arguments"
    log_message "ERROR: run_pipeline.sh takes no arguments (got '$*'). There are no stages any more: one run does everything."
    exit 1
fi

log_message "========================================"
log_message "Starting Wayakit Pipeline"
log_message "========================================"

CURRENT_STEP="setup"
log_message "Changing to directory: $PROJECT_DIR"
cd "$PROJECT_DIR" || { log_message "ERROR: Could not access $PROJECT_DIR"; exit 1; }

log_message "Initializing Conda..."
source "${CONDA_BASE_PATH}/etc/profile.d/conda.sh" || { log_message "ERROR: Could not initialize Conda"; exit 1; }

log_message "Activating Conda environment: $CONDA_ENV_NAME"
conda activate "$CONDA_ENV_NAME" || { log_message "ERROR: Could not activate the environment $CONDA_ENV_NAME"; exit 1; }
log_message "Conda environment activated."

# Every step ends in '|| exit 1': a failed step or an expired 'timeout' (exit 124) stops the run
# before the Odoo upload (Nov 13-15, 2026 run: 53 rows and it still uploaded). The EXIT trap still
# shuts the instance down. No 'git pull' (F2): code reaches the instance by a deploy, not mid-run.
run_command "python ml_model/odoo_api_products.py" "1. Fetch ALL products from Odoo" || exit 1
run_command "python scraper/odoo_api_connection_products.py --input_odoo_products_file '$ODOO_PRODUCTS_FILE' --output_analysis_file '$ANALYSIS_FILE'" "2. Generate the scraping list" || exit 1
# The previous competitors file is NOT deleted: scraper/main.py writes to a temporary file and
# replaces it only when the scraping succeeds (C02).
run_command "timeout 30h python scraper/main.py --analysis_file '$ANALYSIS_FILE' --output_mode overwrite --output_file '$COMPETITORS_FILE'" "3. Run scraping" || exit 1
run_command "python ml_model/odoo_api_cotizations.py" "4a. Fetch quotations from Odoo" || exit 1
run_command "python ml_model/odoo_api_competitor_products.py" "4b. Fetch competitor products from Odoo" || exit 1

if [ ! -f "$ODOO_PRODUCTS_FILE" ]; then
    run_command "python ml_model/odoo_api_products.py" "5. (Re)Fetch products from Odoo" || exit 1
fi

run_command "timeout 1h python ml_model/1a_preprocess_data.py" "6. Preprocess competitor data" || exit 1
run_command "timeout 1h python ml_model/1b_preprocess_data.py --run_mode full" "7. Prepare Wayakit data" || exit 1
run_command "timeout 1h python ml_model/2_train_models.py" "8. Train ML models" || exit 1
run_command "timeout 1h python ml_model/3_predicted_prices.py" "9. Generate predictions" || exit 1
run_command "timeout 1h python ml_model/odoo_api_price_suggestion.py --run_mode full" "10. Upload suggestions to Odoo" || exit 1

CURRENT_STEP="finish"
log_message "Deactivating Conda environment."
conda deactivate

log_message "Wayakit Pipeline completed."
log_message "========================================"
exit 0  # the EXIT trap schedules the shutdown
