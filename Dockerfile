FROM python:3.14-slim

WORKDIR /app

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
