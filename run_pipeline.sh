#!/bin/bash
set -o pipefail

# --- CONFIGURATION ---
PROJECT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
CONDA_ENV_NAME="wayakit_env"
LOG_DIR="$PROJECT_DIR/logs"

# --- State and Log Files ---
LAST_FULL_RUN_FILE="$LOG_DIR/last_full_run.txt"
RUN_MODE_FILE="$LOG_DIR/run_mode.txt" # State file
SCRIPT_LOG_FILE="$PROJECT_DIR/logs/pipeline_execution.log"
# --- IMPORTANT CHANGE: Detect user (ubuntu or ec2-user) ---
if [ -d "/home/ubuntu" ]; then
    CONDA_BASE_PATH=$(conda info --base 2>/dev/null || echo "/home/ubuntu/miniconda3")
else
    CONDA_BASE_PATH=$(conda info --base 2>/dev/null || echo "/home/ec2-user/miniconda")
fi

# --- Data Files ---
ODOO_PRODUCTS_FILE="$PROJECT_DIR/ml_model/wayakit_products.csv"
NEW_PRODUCTS_TEMP_FILE="$PROJECT_DIR/ml_model/wayakit_new_products_temp.csv"
ANALYSIS_FILE_FULL="$PROJECT_DIR/scraper/analysis-odoo.csv"
ANALYSIS_FILE_PARTIAL="$PROJECT_DIR/scraper/analysis-odoo_partial.csv"
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

# --- Guaranteed shutdown ---
# Fires on ANY script exit (success or exit 1), guaranteeing that
# the instance always shuts down and does not stay on accruing costs.
trap 'rc=$?; log_message "Pipeline finished (code=$rc). Scheduling shutdown in 1 minute."; sudo shutdown -h +1' EXIT

# --- Stage Logic ---
STAGE=1
if [ "$1" == "--stage" ] && [ "$2" == "2" ]; then
    STAGE=2
else
    STAGE=1
fi

# ==================================================================
# --- START OF STAGE 1 (Preparation and Shutdown) ---
# ==================================================================
if [ "$STAGE" == "1" ]; then

    # Partial mode disabled (legacy): the pipeline always runs in full, month after month.
    IS_FULL_RUN="true"

    log_message "========================================"
    log_message "Starting Wayakit Pipeline (STAGE 1)"
    log_message "========================================"
    log_message "Partial mode disabled: always a FULL run."

    log_message "Changing to directory: $PROJECT_DIR"
    cd "$PROJECT_DIR" || { log_message "ERROR: Could not access $PROJECT_DIR"; exit 1; }

    log_message "Initializing Conda..."
    source "${CONDA_BASE_PATH}/etc/profile.d/conda.sh" || { log_message "ERROR: Could not initialize Conda"; exit 1; }

    log_message "Activating Conda environment: $CONDA_ENV_NAME"
    conda activate "$CONDA_ENV_NAME" || { log_message "ERROR: Could not activate the environment $CONDA_ENV_NAME"; exit 1; }
    log_message "Conda environment activated."

    log_message "--- STEP 1: Preparing scraping list ---"
    run_command "python ml_model/odoo_api_products.py" "1a. Fetch ALL products from Odoo (with date)" || exit 1

    if [ "$IS_FULL_RUN" == "true" ]; then
        log_message "Mode: FULL RUN (Preparation)"
        run_command "python scraper/odoo_api_connection_products.py --input_odoo_products_file '$ODOO_PRODUCTS_FILE' --output_analysis_file '$ANALYSIS_FILE_FULL'" "1b. Generate FULL scraping list" || exit 1
        echo "full" > "$RUN_MODE_FILE"
        echo "$(date -u '+%Y-%m-%d %H:%M:%S')" > "$LAST_FULL_RUN_FILE"
        log_message "File '$ANALYSIS_FILE_FULL' is ready for manual review."

    else
        log_message "Mode: PARTIAL RUN (Preparation)"
        run_command "python ml_model/filter_new_products.py --input '$ODOO_PRODUCTS_FILE' --timestamp '$LAST_FULL_RUN_FILE' --output '$NEW_PRODUCTS_TEMP_FILE'" "1b. Filter NEW products" || exit 1
        echo "partial" > "$RUN_MODE_FILE"
        log_message "File '$NEW_PRODUCTS_TEMP_FILE' is ready."
    fi

    log_message "--- STAGE 1 (Preparation) completed ---"
    log_message "Deactivating Conda environment."
    conda deactivate

    log_message "========================================"
    exit 0  # the EXIT trap schedules the shutdown

fi
# --- END OF STAGE 1 ---


# ==================================================================
# --- START OF STAGE 2 (Post-Review Run) ---
# ==================================================================
if [ "$STAGE" == "2" ]; then

    log_message "========================================"
    log_message "Starting Wayakit Pipeline (STAGE 2)"
    log_message "========================================"

    if [ ! -f "$RUN_MODE_FILE" ]; then
        log_message "ERROR: State file '$RUN_MODE_FILE' not found."
        exit 1
    fi

    RUN_MODE=$(cat "$RUN_MODE_FILE")
    log_message "Continuation mode detected: $RUN_MODE"
    
    # Make sure the manual Git changes are applied
    log_message "Updating project from Git..."
    cd "$PROJECT_DIR" || { log_message "ERROR: Could not access $PROJECT_DIR"; exit 1; }
    git pull
    
    log_message "Initializing Conda..."
    source "${CONDA_BASE_PATH}/etc/profile.d/conda.sh" || { log_message "ERROR: Could not initialize Conda"; exit 1; }

    log_message "Activating Conda environment: $CONDA_ENV_NAME"
    conda activate "$CONDA_ENV_NAME" || { log_message "ERROR: Could not activate the environment $CONDA_ENV_NAME"; exit 1; }
    log_message "Conda environment activated."
    
    
    # --- START: FULL RUN Flow (Stage 2) ---
    if [ "$RUN_MODE" == "full" ]; then
        log_message "Continuing with FULL flow..."
        ANALYSIS_FILE_TO_USE="$ANALYSIS_FILE_FULL"
        SCRAPER_OUTPUT_MODE="overwrite"
        
        if [ -f "$COMPETITORS_FILE" ]; then
            log_message "Cleaning existing competitors file."
            rm "$COMPETITORS_FILE"
        fi
        
        run_command "timeout 30h python scraper/main.py --analysis_file '$ANALYSIS_FILE_TO_USE' --output_mode '$SCRAPER_OUTPUT_MODE' --output_file '$COMPETITORS_FILE'" "2. Run scraping (Mode: $SCRAPER_OUTPUT_MODE)"
        run_command "python ml_model/odoo_api_cotizations.py" "3a. Fetch quotations from Odoo"
        run_command "python ml_model/odoo_api_competitor_products.py" "3b. Fetch competitor products from Odoo"
        
        if [ ! -f "$ODOO_PRODUCTS_FILE" ]; then
            run_command "python ml_model/odoo_api_products.py" "4. (Re)Fetch products from Odoo"
        fi

        # --- CHANGE: Added 'timeout 1h' (1 hour) to the ML scripts ---
        run_command "timeout 1h python ml_model/1a_preprocess_data.py" "5. Preprocess competitor data"
        run_command "timeout 1h python ml_model/1b_preprocess_data.py --run_mode full" "6. Prepare Wayakit data (Mode: full)"
        run_command "timeout 1h python ml_model/2_train_models.py" "7. Train ML models"
        run_command "timeout 1h python ml_model/3_predicted_prices.py" "8. Generate predictions"
        run_command "timeout 1h python ml_model/odoo_api_price_suggestion.py --run_mode full" "9. Upload suggestions to Odoo (Mode: full)"

    # --- END: FULL RUN Flow (Stage 2) ---

    # --- START: PARTIAL RUN Flow (Stage 2) ---
    elif [ "$RUN_MODE" == "partial" ]; then

        log_message "Continuing with PARTIAL flow..."
        log_message "Checking for new products in '$NEW_PRODUCTS_TEMP_FILE'..."
        LINE_COUNT=$(wc -l < "$NEW_PRODUCTS_TEMP_FILE" | xargs) 

        if [ "$LINE_COUNT" -le 1 ]; then
            log_message "No new products found ($LINE_COUNT lines). No run required."
            log_message "Skipping STAGE 2."
        else
            log_message "Found $LINE_COUNT new products. Continuing..."
            
            ANALYSIS_FILE_TO_USE="$ANALYSIS_FILE_PARTIAL"
            SCRAPER_OUTPUT_MODE="append"
            # --- CHANGE: Added 'timeout 4h' (4 hours) to the partial scraping ---
            run_command "python scraper/odoo_api_connection_products.py --input_odoo_products_file '$NEW_PRODUCTS_TEMP_FILE' --output_analysis_file '$ANALYSIS_FILE_PARTIAL'" "1c. Generate PARTIAL scraping list (post-review)"
            run_command "timeout 4h python scraper/main.py --analysis_file '$ANALYSIS_FILE_TO_USE' --output_mode '$SCRAPER_OUTPUT_MODE' --output_file '$COMPETITORS_FILE'" "2. Run scraping (Mode: $SCRAPER_OUTPUT_MODE)"
            run_command "python ml_model/odoo_api_cotizations.py" "3a. Fetch quotations from Odoo"
            run_command "python ml_model/odoo_api_competitor_products.py" "3b. Fetch competitor products from Odoo"

            if [ ! -f "$ODOO_PRODUCTS_FILE" ]; then
                run_command "python ml_model/odoo_api_products.py" "4. (Re)Fetch products from Odoo"
            fi
            
            # --- CHANGE: Added 'timeout 1h' (1 hour) to the ML scripts ---
            run_command "timeout 1h python ml_model/1a_preprocess_data.py" "5. Preprocess competitor data"
            run_command "timeout 1h python ml_model/1b_preprocess_data.py --run_mode partial" "6. Prepare Wayakit data (Mode: partial)"
            run_command "timeout 1h python ml_model/2_train_models.py" "7. Train ML models"
            run_command "timeout 1h python ml_model/3_predicted_prices.py" "8. Generate predictions"
            run_command "timeout 1h python ml_model/odoo_api_price_suggestion.py --run_mode partial" "9. Upload suggestions to Odoo (Mode: partial)"
        fi
    
    else
        log_message "ERROR: Unknown run mode '$RUN_MODE'."
    fi
    # --- END: PARTIAL RUN Flow (Stage 2) ---

    log_message "Deactivating Conda environment."
    conda deactivate

    log_message "Wayakit Pipeline (STAGE 2) completed."
    log_message "========================================"
    exit 0  # the EXIT trap schedules the shutdown
fi
# --- END OF STAGE 2 ---