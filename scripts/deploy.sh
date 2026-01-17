#!/bin/bash
set -euo pipefail

# =============================================================================
# FishFeed Deployment Script
# =============================================================================
# Usage: ./deploy.sh [--skip-backup] [--skip-migrations]
#
# This script performs a full deployment:
# 1. Creates database backup (optional)
# 2. Pulls latest Docker images
# 3. Runs database migrations
# 4. Restarts services with zero-downtime strategy
# 5. Verifies health check
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# On server, script is in project root; locally it's in scripts/
if [[ -f "${SCRIPT_DIR}/docker-compose.yml" ]]; then
    PROJECT_DIR="${SCRIPT_DIR}"
else
    PROJECT_DIR="${SCRIPT_DIR}/.."
fi
LOG_FILE="${PROJECT_DIR}/logs/deploy-$(date +%Y%m%d-%H%M%S).log"

# Configuration - on server we use docker-compose.yml, locally docker-compose.prod.yml
if [[ -f "${PROJECT_DIR}/docker-compose.yml" ]]; then
    COMPOSE_FILE="${PROJECT_DIR}/docker-compose.yml"
else
    COMPOSE_FILE="${PROJECT_DIR}/docker-compose.prod.yml"
fi
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"
MAX_RETRIES="${MAX_RETRIES:-30}"
RETRY_INTERVAL="${RETRY_INTERVAL:-2}"

# Flags
SKIP_BACKUP=false
SKIP_MIGRATIONS=false

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

parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --skip-backup)
                SKIP_BACKUP=true
                shift
                ;;
            --skip-migrations)
                SKIP_MIGRATIONS=true
                shift
                ;;
            *)
                log_error "Unknown option: $1"
                exit 1
                ;;
        esac
    done
}

# =============================================================================
# Deployment Steps
# =============================================================================

backup_database() {
    if [[ "${SKIP_BACKUP}" == "true" ]]; then
        log_warn "Skipping database backup (--skip-backup flag)"
        return 0
    fi

    log_info "Creating database backup..."
    if [[ -f "${SCRIPT_DIR}/backup-db.sh" ]]; then
        "${SCRIPT_DIR}/backup-db.sh"
    else
        log_warn "backup-db.sh not found, skipping backup"
    fi
}

pull_images() {
    log_info "Pulling latest Docker images..."
    docker compose -f "${COMPOSE_FILE}" pull
}

run_migrations() {
    if [[ "${SKIP_MIGRATIONS}" == "true" ]]; then
        log_warn "Skipping migrations (--skip-migrations flag)"
        return 0
    fi

    log_info "Running database migrations..."
    docker compose -f "${COMPOSE_FILE}" run --rm api uv run alembic upgrade head
}

restart_services() {
    log_info "Restarting services..."
    docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans
}

health_check() {
    log_info "Waiting for API container to become healthy..."

    local attempt=1
    while [[ ${attempt} -le ${MAX_RETRIES} ]]; do
        # Use docker exec with python since curl isn't installed in the container
        if docker exec fishfeed-api-1 python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)" > /dev/null 2>&1; then
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

cleanup_old_images() {
    log_info "Cleaning up old Docker images..."
    docker image prune -f --filter "until=168h" || true
}

# =============================================================================
# Main
# =============================================================================

main() {
    ensure_log_dir
    parse_args "$@"

    log_info "=========================================="
    log_info "Starting FishFeed deployment"
    log_info "=========================================="
    log_info "Compose file: ${COMPOSE_FILE}"
    log_info "Health URL: ${HEALTH_URL}"
    log_info "Log file: ${LOG_FILE}"
    log_info "=========================================="

    cd "${PROJECT_DIR}"

    # Step 1: Backup database
    backup_database

    # Step 2: Pull latest images
    pull_images

    # Step 3: Run migrations
    run_migrations

    # Step 4: Restart services
    restart_services

    # Step 5: Health check
    if health_check; then
        log_info "=========================================="
        log_info "Deployment completed successfully!"
        log_info "=========================================="

        # Cleanup old images after successful deployment
        cleanup_old_images

        exit 0
    else
        log_error "=========================================="
        log_error "Deployment failed! Consider running rollback."
        log_error "Run: ./scripts/rollback.sh"
        log_error "=========================================="
        exit 1
    fi
}

main "$@"
