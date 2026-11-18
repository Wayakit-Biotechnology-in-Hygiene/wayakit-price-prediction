#!/bin/bash
# Bootstrap of the Price Intelligence instance (F3). Idempotent: run it as many times as needed.
#
# What it leaves configured on the instance (Amazon Linux 2023, user ec2-user):
# - SSM agent installed, enabled and running (access is only through SSM, no SSH).
# - git, tzdata and the AWS CLI (the EXIT trap of run_pipeline.sh publishes the SNS alert with it).
# - Google Chrome (installed only if missing; an existing Chrome is not upgraded).
# - conda (Miniforge, conda-forge only) in /home/ec2-user/miniconda, the path run_pipeline.sh falls
#   back to, and the conda env `wayakit_env` created or updated from environment.yml (the name
#   stays `wayakit_env`: run_pipeline.sh activates it by that name).
# - /etc/price-intelligence.env (root, 0644, NO secrets; secrets stay in Secrets Manager) with
#   PI_LOG_DIR, PI_ALERTS_TOPIC_ARN, PI_RELEVANCE_PRIMARY_MODEL and PI_RELEVANCE_FALLBACK_MODEL.
#   A value not passed as an argument keeps what the file already had (a re-run to update conda
#   does not drop the alerts); pass an empty string to remove it. PI_LOG_DIR defaults to
#   /home/ec2-user/logs, the same folder run_pipeline.sh uses by default.
# - pi-pipeline.service installed in /etc/systemd/system and enabled (not started: it runs on the
#   next boot, and only on the scheduled one; see pi_boot_guard.py).
# - The ec2-user crontab lines that launch run_pipeline.sh removed (any other line is kept; the
#   previous crontab is saved in the log folder as crontab-ec2-user.<UTC time>.bak). They must
#   go before the new run_pipeline.sh runs: the old no-argument line would now start a whole run,
#   and the old `--stage 2` line exits 1, whose EXIT trap shuts down a run already in progress.
#   So this is the first step, right after the arguments are checked: a later step that fails
#   (packages, Chrome, conda) still leaves the crontab filtered.
# The F4 deploy service (pi-deploy.service) and the S3 release layout are not part of F3.
#
# It never prints the values of the env file (the topic and profile ARNs contain the account ID),
# only which keys are set.
#
# Cost: none by itself (it runs on the instance; downloads come from dnf, Google and GitHub).
#
# Usage (as root, from the repo at /home/ec2-user/wayakit-price-prediction; e.g. over SSM
# Run Command, or from the userData of `python -m infra.ec2_instance --create`):
#   sudo infra/instance/bootstrap.sh --dry-run
#   sudo infra/instance/bootstrap.sh --alerts-topic-arn <topic ARN> \
#        --primary-model <DeepSeek profile ARN or model ID> --fallback-model <Haiku profile ARN or ID>
# Options: --log-dir DIR, --skip-chrome, --skip-conda, --dry-run (prints what it would change,
# changes nothing), -h/--help.
#
# Sourcing it (`source bootstrap.sh`) only defines the functions, so the pure ones
# (filter_crontab, render_env_file, env_file_value, valid_*) can be tested without root.

PI_USER="ec2-user"
PI_HOME="/home/ec2-user"
REPO_DIR_EXPECTED="$PI_HOME/wayakit-price-prediction"  # fixed in pi-pipeline.service
CONDA_DIR="$PI_HOME/miniconda"                          # run_pipeline.sh fallback path
CONDA_ENV="wayakit_env"                                 # run_pipeline.sh CONDA_ENV_NAME
DEFAULT_LOG_DIR="$PI_HOME/logs"                         # run_pipeline.sh default (<repo>/../logs)
ENV_FILE="/etc/price-intelligence.env"
SERVICE_NAME="pi-pipeline.service"
SERVICE_DST="/etc/systemd/system/$SERVICE_NAME"
MINIFORGE_URL="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh"
CHROME_RPM_URL="https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm"
ENV_KEYS="PI_LOG_DIR PI_ALERTS_TOPIC_ARN PI_RELEVANCE_PRIMARY_MODEL PI_RELEVANCE_FALLBACK_MODEL"

# --- Pure functions (no side effects; tested by sourcing this file) ---------------------------

# stdin crontab -> stdout without the active lines that launch run_pipeline.sh. Comments and
# every other line are kept as they are.
filter_crontab() {
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            *run_pipeline.sh*)
                [[ "$line" =~ ^[[:space:]]*# ]] || continue ;;
        esac
        printf '%s\n' "$line"
    done
}

# Value of KEY in an env file (KEY=VALUE lines), or nothing. Exit 0 even if missing.
env_file_value() {
    local file="$1" key="$2" line
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "$key="*) printf '%s' "${line#*=}"; return 0 ;;
        esac
    done < "$file"
    return 0
}

# Env file content for systemd's EnvironmentFile. Empty values are left out (the code then uses
# its defaults, e.g. the model IDs in scraper/services/ai_service.py).
render_env_file() {
    local log_dir="$1" topic="$2" primary="$3" fallback="$4"
    echo "# Written by infra/instance/bootstrap.sh. No secrets here: they are read from Secrets Manager."
    echo "PI_LOG_DIR=$log_dir"
    [ -z "$topic" ] || echo "PI_ALERTS_TOPIC_ARN=$topic"
    [ -z "$primary" ] || echo "PI_RELEVANCE_PRIMARY_MODEL=$primary"
    [ -z "$fallback" ] || echo "PI_RELEVANCE_FALLBACK_MODEL=$fallback"
}

valid_log_dir() { [[ "$1" =~ ^/[A-Za-z0-9._/-]+$ ]]; }
valid_topic_arn() { [ -z "$1" ] || [[ "$1" =~ ^arn:aws:sns:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_-]+$ ]]; }
# A Bedrock model ID, system profile ID or application profile ARN (no spaces, quotes or $).
valid_model() { [ -z "$1" ] || [[ "$1" =~ ^[A-Za-z0-9._:/+-]+$ ]]; }

# --- Side effects ---------------------------------------------------------------------------
DRY_RUN=0

say() { echo "bootstrap: $*"; }
die() { echo "bootstrap: ERROR: $*" >&2; exit 1; }

# Runs a command, or only prints it in dry-run mode.
act() {
    if [ "$DRY_RUN" = 1 ]; then
        say "[dry-run] would run: $*"
    else
        "$@"
    fi
}

as_user() { runuser -u "$PI_USER" -- "$@"; }

ensure_ssm_agent() {
    if systemctl is-active --quiet amazon-ssm-agent; then
        say "SSM agent: active."
        return 0
    fi
    say "SSM agent: not active; installing/enabling it."
    rpm -q amazon-ssm-agent > /dev/null 2>&1 || act dnf install -y amazon-ssm-agent
    act systemctl enable --now amazon-ssm-agent
}

ensure_packages() {
    local missing=() pkg
    for pkg in git tzdata; do
        rpm -q "$pkg" > /dev/null 2>&1 || missing+=("$pkg")
    done
    command -v aws > /dev/null 2>&1 || missing+=("awscli-2")
    if [ "${#missing[@]}" -eq 0 ]; then
        say "Packages: git, tzdata and the AWS CLI present."
    else
        say "Packages: installing ${missing[*]}."
        act dnf install -y "${missing[@]}"
    fi
}

ensure_chrome() {
    if command -v google-chrome > /dev/null 2>&1; then
        say "Chrome: present ($(google-chrome --version 2>/dev/null || echo 'version unknown')); not upgraded."
        return 0
    fi
    say "Chrome: missing; installing google-chrome-stable."
    act dnf install -y "$CHROME_RPM_URL"
}

ensure_conda() {
    local tmp
    if [ -x "$CONDA_DIR/bin/conda" ]; then
        say "conda: present in $CONDA_DIR."
    else
        say "conda: missing; installing Miniforge in $CONDA_DIR."
        if [ "$DRY_RUN" = 1 ]; then
            say "[dry-run] would download and verify (sha256) $MINIFORGE_URL and install it as $PI_USER"
        else
            tmp=$(mktemp -d)
            curl -fsSL -o "$tmp/miniforge.sh" "$MINIFORGE_URL"
            curl -fsSL -o "$tmp/miniforge.sh.sha256" "$MINIFORGE_URL.sha256"
            # The .sha256 file names the original installer; check the hash only.
            [ "$(sha256sum "$tmp/miniforge.sh" | cut -d' ' -f1)" = "$(cut -d' ' -f1 "$tmp/miniforge.sh.sha256")" ] \
                || die "Miniforge installer checksum mismatch."
            chmod 0755 "$tmp" && chmod 0644 "$tmp/miniforge.sh"
            as_user bash "$tmp/miniforge.sh" -b -p "$CONDA_DIR"
            rm -rf "$tmp"
        fi
    fi
    if [ -d "$CONDA_DIR/envs/$CONDA_ENV" ]; then
        say "conda env $CONDA_ENV: updating from environment.yml."
        act as_user "$CONDA_DIR/bin/conda" env update -n "$CONDA_ENV" -f "$REPO_DIR/environment.yml"
        return 0
    fi
    say "conda env $CONDA_ENV: creating from environment.yml."
    act as_user "$CONDA_DIR/bin/conda" env create -n "$CONDA_ENV" -f "$REPO_DIR/environment.yml"
}

ensure_env_file() {
    local tmp key
    act install -d -o "$PI_USER" -g "$PI_USER" -m 0755 "$LOG_DIR"
    tmp=$(mktemp)
    render_env_file "$LOG_DIR" "$TOPIC_ARN" "$PRIMARY_MODEL" "$FALLBACK_MODEL" > "$tmp"
    if [ -f "$ENV_FILE" ] && cmp -s "$tmp" "$ENV_FILE"; then
        say "$ENV_FILE: up to date."
    else
        say "$ENV_FILE: writing."
        act install -o root -g root -m 0644 "$tmp" "$ENV_FILE"
    fi
    # Key names only: the values carry the account ID (topic and profile ARNs).
    for key in $ENV_KEYS; do
        if [ -n "$(env_file_value "$tmp" "$key")" ]; then say "  $key: set"; else say "  $key: not set"; fi
    done
    rm -f "$tmp"
    [ -n "$TOPIC_ARN" ] || say "WARNING: PI_ALERTS_TOPIC_ARN is not set: failed runs will not send an email alert."
}

ensure_service() {
    local src="$REPO_DIR/infra/instance/$SERVICE_NAME"
    [ -f "$src" ] || die "$src not found."
    if [ -f "$SERVICE_DST" ] && cmp -s "$src" "$SERVICE_DST"; then
        say "$SERVICE_NAME: up to date."
    else
        say "$SERVICE_NAME: installing."
        act install -o root -g root -m 0644 "$src" "$SERVICE_DST"
        act systemctl daemon-reload
    fi
    if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        say "$SERVICE_NAME: enabled."
    else
        act systemctl enable "$SERVICE_NAME"
    fi
}

remove_old_cron_lines() {
    local current filtered backup removed
    if ! command -v crontab > /dev/null 2>&1; then
        say "crontab: not installed; nothing to remove."
        return 0
    fi
    current=$(crontab -u "$PI_USER" -l 2>/dev/null) || current=""
    filtered=$(printf '%s\n' "$current" | filter_crontab)
    if [ "$filtered" = "$current" ]; then
        say "crontab of $PI_USER: no run_pipeline.sh lines."
        return 0
    fi
    removed=$(( $(printf '%s\n' "$current" | wc -l) - $(printf '%s\n' "$filtered" | wc -l) ))
    say "crontab of $PI_USER: removing $removed run_pipeline.sh line(s); other lines are kept."
    backup="$LOG_DIR/crontab-$PI_USER.$(date -u '+%Y%m%dT%H%M%SZ').bak"
    if [ "$DRY_RUN" = 1 ]; then
        say "[dry-run] would save the current crontab to $backup and install the filtered one"
        return 0
    fi
    install -d -o "$PI_USER" -g "$PI_USER" -m 0755 "$LOG_DIR"
    printf '%s\n' "$current" > "$backup"
    chown "$PI_USER:$PI_USER" "$backup" && chmod 0600 "$backup"
    if [ -z "${filtered//[[:space:]]/}" ]; then
        crontab -u "$PI_USER" -r
    else
        printf '%s\n' "$filtered" | crontab -u "$PI_USER" -
    fi
    say "crontab of $PI_USER: previous version saved to $backup."
}

usage() { sed -n '2,/^$/s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; }

main() {
    set -euo pipefail
    local arg_log_dir="" arg_topic="" arg_primary="" arg_fallback=""
    local has_log_dir=0 has_topic=0 has_primary=0 has_fallback=0 skip_chrome=0 skip_conda=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --log-dir) [ $# -ge 2 ] || die "--log-dir needs a value"; arg_log_dir="$2"; has_log_dir=1; shift 2 ;;
            --alerts-topic-arn) [ $# -ge 2 ] || die "--alerts-topic-arn needs a value"; arg_topic="$2"; has_topic=1; shift 2 ;;
            --primary-model) [ $# -ge 2 ] || die "--primary-model needs a value"; arg_primary="$2"; has_primary=1; shift 2 ;;
            --fallback-model) [ $# -ge 2 ] || die "--fallback-model needs a value"; arg_fallback="$2"; has_fallback=1; shift 2 ;;
            --skip-chrome) skip_chrome=1; shift ;;
            --skip-conda) skip_conda=1; shift ;;
            --dry-run) DRY_RUN=1; shift ;;
            -h|--help) usage; return 0 ;;
            *) die "unknown argument '$1' (see --help)" ;;
        esac
    done

    [ "$(id -u)" -eq 0 ] || die "run it as root (sudo)."
    REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
    [ -d "$REPO_DIR_EXPECTED" ] && [ "$REPO_DIR" = "$(cd -- "$REPO_DIR_EXPECTED" && pwd -P)" ] \
        || die "the repo must be at $REPO_DIR_EXPECTED (pi-pipeline.service runs it from there); this copy is at $REPO_DIR."

    # Arguments win; otherwise keep the current file's value (empty string = remove).
    LOG_DIR=$(env_file_value "$ENV_FILE" PI_LOG_DIR)
    TOPIC_ARN=$(env_file_value "$ENV_FILE" PI_ALERTS_TOPIC_ARN)
    PRIMARY_MODEL=$(env_file_value "$ENV_FILE" PI_RELEVANCE_PRIMARY_MODEL)
    FALLBACK_MODEL=$(env_file_value "$ENV_FILE" PI_RELEVANCE_FALLBACK_MODEL)
    if [ "$has_log_dir" = 1 ]; then LOG_DIR="$arg_log_dir"; fi
    if [ "$has_topic" = 1 ]; then TOPIC_ARN="$arg_topic"; fi
    if [ "$has_primary" = 1 ]; then PRIMARY_MODEL="$arg_primary"; fi
    if [ "$has_fallback" = 1 ]; then FALLBACK_MODEL="$arg_fallback"; fi
    LOG_DIR="${LOG_DIR:-$DEFAULT_LOG_DIR}"
    valid_log_dir "$LOG_DIR" || die "invalid --log-dir (absolute path, letters, digits, . _ - /)."
    valid_topic_arn "$TOPIC_ARN" || die "invalid --alerts-topic-arn (expected an SNS topic ARN)."
    valid_model "$PRIMARY_MODEL" || die "invalid --primary-model."
    valid_model "$FALLBACK_MODEL" || die "invalid --fallback-model."

    if [ "$DRY_RUN" = 1 ]; then say "DRY RUN: nothing will be changed."; fi
    # First, before any step that can fail (dnf, Chrome, conda downloads): if the F3 code is
    # already on the instance, an old crontab line left behind by a failed bootstrap would start a
    # whole run (Friday line) or shut the instance down mid-run (`--stage 2`). It only needs LOG_DIR.
    remove_old_cron_lines
    ensure_ssm_agent
    ensure_packages
    if [ "$skip_chrome" = 1 ]; then say "Chrome: skipped (--skip-chrome)."; else ensure_chrome; fi
    if [ "$skip_conda" = 1 ]; then say "conda: skipped (--skip-conda)."; else ensure_conda; fi
    ensure_env_file
    ensure_service
    say "done. The pipeline runs on the next scheduled boot (last Thursday of the month, 21:50 America/Mexico_City)."
}

# Sourced (tests): define the functions only.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
