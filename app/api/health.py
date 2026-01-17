"""Health check endpoints for monitoring and orchestration."""

import time
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.redis import get_redis

router = APIRouter(tags=["Health"])

settings = get_settings()
_start_time = time.time()


@router.get("/health")
async def health_liveness() -> dict[str, str]:
    """Basic liveness probe - checks if the service is running."""
    return {
        "status": "ok",
        "version": settings.APP_VERSION,
    }


@router.get("/health/ready")
async def health_readiness(
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Readiness probe - checks if the service can handle requests.

    Verifies database and Redis connections are available.
    Returns 503 if any dependency is unhealthy.
    """
    db_connected = False
    redis_connected = False
    uptime = time.time() - _start_time

    # Check database connection
    try:
        await db.execute(text("SELECT 1"))
        db_connected = True
    except Exception:
        pass

    # Check Redis connection
    try:
        result = redis.ping()
        if hasattr(result, "__await__"):
            await result
        redis_connected = True
    except Exception:
        pass

    is_healthy = db_connected and redis_connected

    if not is_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if is_healthy else "degraded",
        "version": settings.APP_VERSION,
        "db_connected": db_connected,
        "redis_connected": redis_connected,
        "uptime_seconds": round(uptime, 2),
    }
