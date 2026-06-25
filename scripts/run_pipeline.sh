#!/bin/bash
# run_pipeline.sh - Psychological Pipeline Runner
# Absolute paths for launchd compatibility

set -euo pipefail

# Project root - MUST be absolute path
PROJECT_ROOT="/Users/hayden/Desktop/quant-py"

# Python virtual environment
VENV_PYTHON="${PROJECT_ROOT}/.venv/bin/python"
if [ ! -f "${VENV_PYTHON}" ]; then
    VENV_PYTHON="/usr/bin/python3"
fi

# Log directory
LOG_DIR="${PROJECT_ROOT}/logs"
mkdir -p "${LOG_DIR}"

# Timestamp for log files
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="${LOG_DIR}/pipeline_${TIMESTAMP}.log"

# Environment variables
export PYTHONPATH="${PROJECT_ROOT}:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1

# Load environment variables from .env if exists
if [ -f "${PROJECT_ROOT}/.env" ]; then
    export $(grep -v '^#' "${PROJECT_ROOT}/.env" | xargs)
fi

# Function to log messages
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"
}

log "============================================"
log "Starting Psychological Pipeline"
log "Project Root: ${PROJECT_ROOT}"
log "Python: ${VENV_PYTHON}"
log "Log File: ${LOG_FILE}"
log "============================================"

cd "${PROJECT_ROOT}"

# Initialize database if needed
log "Checking database..."
"${VENV_PYTHON}" -c "
import sqlite3
from db.schema import migrate_psychological_schema
conn = sqlite3.connect('reddit_quant.db')
migrate_psychological_schema(conn)
print('Database migration complete')
" >> "${LOG_FILE}" 2>&1

# Run the psychological pipeline
log "Running psychological pipeline..."
"${VENV_PYTHON}" -m psychological.orchestrator >> "${LOG_FILE}" 2>&1

PIPELINE_EXIT_CODE=$?

if [ $PIPELINE_EXIT_CODE -eq 0 ]; then
    log "Psychological pipeline completed successfully"
else
    log "Psychological pipeline failed with exit code: ${PIPELINE_EXIT_CODE}"
fi

# Export Parquet for backtesting (last 30 days)
log "Exporting Parquet for backtesting..."
END_DATE=$(date -u +"%Y-%m-%d")
START_DATE=$(date -u -v-30d +"%Y-%m-%d" 2>/dev/null || date -u -d "30 days ago" +"%Y-%m-%d")

"${VENV_PYTHON}" run_scraper.py export-parquet \
    --start-date "${START_DATE}" \
    --end-date "${END_DATE}" \
    >> "${LOG_FILE}" 2>&1

EXPORT_EXIT_CODE=$?

if [ $EXPORT_EXIT_CODE -eq 0 ]; then
    log "Parquet export completed successfully"
else
    log "Parquet export failed with exit code: ${EXPORT_EXIT_CODE}"
fi

# Get regime status for default watchlist
log "Getting regime status..."
"${VENV_PYTHON}" run_scraper.py regime-status \
    --tickers "NVDA,AMD,MSFT,GOOGL,META,TSLA,AAPL,AMZN" \
    >> "${LOG_FILE}" 2>&1

log "============================================"
log "Pipeline run completed"
log "============================================"

exit $PIPELINE_EXIT_CODE