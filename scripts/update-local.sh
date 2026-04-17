#!/bin/bash
# Pull latest main and bring dev services up-to-date on a CT or dev host.
# Intended for the Proxmox fishfeed-dev CT (mp0 bind-mounted repo), not prod.
# Prod deployments go through scripts/deploy.sh.
#
# Usage:   ./scripts/update-local.sh
# Effects: git pull --ff-only, rebuild images if deps changed,
#          docker compose up -d, run migrations if any new revisions,
#          health-check /health.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

step() { echo -e "${GREEN}==>${NC} $*"; }
warn() { echo -e "${YELLOW}WARN${NC} $*" >&2; }
fail() { echo -e "${RED}ERROR${NC} $*" >&2; exit 1; }

[[ -f docker-compose.yml ]] || fail "docker-compose.yml not found — not in project root."
[[ -d .git ]] || fail ".git not found — must run inside a git checkout."

step "Fetching origin"
git fetch --quiet origin

BEFORE=$(git rev-parse HEAD)
UPSTREAM=$(git rev-parse '@{u}')

if [[ "$BEFORE" = "$UPSTREAM" ]]; then
  step "Already at ${BEFORE:0:7} — nothing to pull"
  exit 0
fi

step "Pulling (fast-forward only)"
git pull --ff-only

AFTER=$(git rev-parse HEAD)
CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")

echo "${CHANGED}" | sed 's/^/    /'

NEED_BUILD=false
NEED_MIGRATE=false
if echo "$CHANGED" | grep -qE '^(pyproject\.toml|uv\.lock|Dockerfile|docker-compose\.yml)$'; then
  NEED_BUILD=true
fi
if echo "$CHANGED" | grep -qE '^alembic/versions/'; then
  NEED_MIGRATE=true
fi

if $NEED_BUILD; then
  step "Rebuilding images (deps or Dockerfile changed)"
  docker compose build
fi

step "Bringing services up"
docker compose up -d

if $NEED_MIGRATE; then
  step "Running Alembic migrations"
  docker compose exec -T api uv run alembic upgrade head
fi

step "Health check"
for i in $(seq 1 15); do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:8000/health || true)
  if [[ "$code" = "200" ]]; then
    step "/health 200 OK (took ${i} attempt(s))"
    step "Updated ${BEFORE:0:7} → ${AFTER:0:7}"
    exit 0
  fi
  sleep 2
done

warn "/health did not return 200 within 30s"
docker compose ps
exit 1
