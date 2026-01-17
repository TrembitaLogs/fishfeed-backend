#!/bin/bash
set -euo pipefail

# =============================================================================
# FishFeed PostgreSQL Backup Script
# =============================================================================
# Usage: ./backup-db.sh [--keep-days N]
#
# This script creates a PostgreSQL backup:
# 1. Creates timestamped backup file
# 2. Compresses with gzip
# 3. Cleans up old backups (default: keep 7 days)
#
# Backups are stored in: ./backups/
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}/.."
BACKUP_DIR="${PROJECT_DIR}/backups"
LOG_FILE="${PROJECT_DIR}/logs/backup-$(date +%Y%m%d-%H%M%S).log"

# Configuration
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.prod.yml"
POSTGRES_CONTAINER="postgres"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-fishfeed}"
KEEP_DAYS="${KEEP_DAYS:-7}"

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

ensure_dirs() {
    mkdir -p "${BACKUP_DIR}"
    mkdir -p "$(dirname "${LOG_FILE}")"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --keep-days)
                KEEP_DAYS="$2"
                shift 2
                ;;
            *)
                log_error "Unknown option: $1"
                exit 1
                ;;
        esac
    done
}

get_container_name() {
    # Get the actual container name from docker compose
    docker compose -f "${COMPOSE_FILE}" ps -q "${POSTGRES_CONTAINER}" 2>/dev/null || echo ""
}

# =============================================================================
# Backup Functions
# =============================================================================

create_backup() {
    local timestamp
    timestamp=$(date +%Y%m%d-%H%M%S)
    local backup_file="${BACKUP_DIR}/fishfeed-${timestamp}.sql.gz"

    log_info "Creating backup: ${backup_file}"

    local container_id
    container_id=$(get_container_name)

    if [[ -z "${container_id}" ]]; then
        log_error "PostgreSQL container not running"
        exit 1
    fi

    # Create backup using pg_dump inside container, compress with gzip
    docker exec "${container_id}" pg_dump \
        -U "${POSTGRES_USER}" \
        -d "${POSTGRES_DB}" \
        --no-owner \
        --no-acl \
        | gzip > "${backup_file}"

    local backup_size
    backup_size=$(du -h "${backup_file}" | cut -f1)
    log_info "Backup created: ${backup_file} (${backup_size})"

    echo "${backup_file}"
}

cleanup_old_backups() {
    log_info "Cleaning up backups older than ${KEEP_DAYS} days..."

    local count
    count=$(find "${BACKUP_DIR}" -name "fishfeed-*.sql.gz" -mtime "+${KEEP_DAYS}" 2>/dev/null | wc -l | tr -d ' ')

    if [[ "${count}" -gt 0 ]]; then
        find "${BACKUP_DIR}" -name "fishfeed-*.sql.gz" -mtime "+${KEEP_DAYS}" -delete
        log_info "Removed ${count} old backup(s)"
    else
        log_info "No old backups to remove"
    fi
}

list_backups() {
    log_info "Available backups:"
    ls -lh "${BACKUP_DIR}"/fishfeed-*.sql.gz 2>/dev/null || log_warn "No backups found"
}

# =============================================================================
# Main
# =============================================================================

main() {
    ensure_dirs
    parse_args "$@"

    log_info "=========================================="
    log_info "Starting FishFeed database backup"
    log_info "=========================================="
    log_info "Database: ${POSTGRES_DB}"
    log_info "Backup dir: ${BACKUP_DIR}"
    log_info "Keep days: ${KEEP_DAYS}"
    log_info "=========================================="

    cd "${PROJECT_DIR}"

    # Step 1: Create backup
    local backup_file
    backup_file=$(create_backup)

    # Step 2: Cleanup old backups
    cleanup_old_backups

    # Step 3: List available backups
    list_backups

    log_info "=========================================="
    log_info "Backup completed successfully!"
    log_info "File: ${backup_file}"
    log_info "=========================================="
}

main "$@"
