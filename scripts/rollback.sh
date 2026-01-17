#!/bin/bash
set -euo pipefail

# =============================================================================
# FishFeed Rollback Script
# =============================================================================
# Usage: ./rollback.sh [image_tag]
#
# This script rolls back to a previous Docker image version:
# 1. Stops current services
# 2. Pulls specified image tag (or previous tag from history)
# 3. Restarts services
# 4. Verifies health check
#
# Example:
#   ./rollback.sh                    # Rollback to previous image
#   ./rollback.sh abc123def          # Rollback to specific SHA tag
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}/.."
LOG_FILE="${PROJECT_DIR}/logs/rollback-$(date +%Y%m%d-%H%M%S).log"

# Configuration
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.prod.yml"
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"
MAX_RETRIES="${MAX_RETRIES:-30}"
RETRY_INTERVAL="${RETRY_INTERVAL:-2}"
HISTORY_FILE="${PROJECT_DIR}/.deploy-history"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# =============================================================================
# Helper Functions
# =============================================================================

log() {
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo -e "${timestamp} | $1" | tee -a "${LOG_FILE}"
}

log_info() {
    log "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    log "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    log "${RED}[ERROR]${NC} $1"
}

ensure_log_dir() {
    mkdir -p "$(dirname "${LOG_FILE}")"
}

get_previous_tag() {
    if [[ -f "${HISTORY_FILE}" ]]; then
        # Get second-to-last line (previous deployment)
        tail -2 "${HISTORY_FILE}" | head -1
    else
        log_error "No deployment history found at ${HISTORY_FILE}"
        log_error "Cannot determine previous image tag."
        log_error "Please specify a tag manually: ./rollback.sh <image_tag>"
        exit 1
    fi
}

health_check() {
    log_info "Waiting for health check at ${HEALTH_URL}..."

    local attempt=1
    while [[ ${attempt} -le ${MAX_RETRIES} ]]; do
        if curl -sf "${HEALTH_URL}" > /dev/null 2>&1; then
            log_info "Health check passed on attempt ${attempt}/${MAX_RETRIES}"
            return 0
        fi

        log_info "Attempt ${attempt}/${MAX_RETRIES} - waiting ${RETRY_INTERVAL}s..."
        sleep "${RETRY_INTERVAL}"
        ((attempt++))
    done

    log_error "Health check failed after ${MAX_RETRIES} attempts"
    return 1
}

# =============================================================================
# Main
# =============================================================================

main() {
    ensure_log_dir

    local target_tag="${1:-}"

    if [[ -z "${target_tag}" ]]; then
        log_info "No tag specified, looking for previous deployment..."
        target_tag=$(get_previous_tag)
    fi

    log_info "=========================================="
    log_info "Starting FishFeed rollback"
    log_info "=========================================="
    log_info "Target image tag: ${target_tag}"
    log_info "Compose file: ${COMPOSE_FILE}"
    log_info "Log file: ${LOG_FILE}"
    log_info "=========================================="

    cd "${PROJECT_DIR}"

    # Step 1: Set the image tag for rollback
    export IMAGE_TAG="${target_tag}"
    log_info "Setting IMAGE_TAG=${IMAGE_TAG}"

    # Step 2: Pull the specific image
    log_info "Pulling image with tag: ${target_tag}..."
    docker compose -f "${COMPOSE_FILE}" pull

    # Step 3: Restart services with rollback image
    log_info "Restarting services with rollback image..."
    docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans

    # Step 4: Health check
    if health_check; then
        log_info "=========================================="
        log_info "Rollback completed successfully!"
        log_info "Rolled back to: ${target_tag}"
        log_info "=========================================="
        exit 0
    else
        log_error "=========================================="
        log_error "Rollback failed! Services may be in inconsistent state."
        log_error "Manual intervention required."
        log_error "=========================================="
        exit 1
    fi
}

main "$@"
