FROM python:3.14-slim

WORKDIR /app

# Install postgresql-client-18 (matches postgres:18-alpine server) for pg_dump,
# used by the database-backup scheduler. The official PGDG apt repo is added
# to get a matching major version — Debian's default postgresql-client is
# typically older than the server.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

# Copy uv from official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Application version (injected from git tag at build time)
ARG APP_VERSION=0.1.0
ENV APP_VERSION=${APP_VERSION}

# Copy application code
COPY app ./app

# Copy Alembic configuration
COPY alembic.ini ./
COPY alembic ./alembic

# Copy seed / maintenance scripts
COPY scripts ./scripts

# Expose port
EXPOSE 8000

# Run the application
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
