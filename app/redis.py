"""Redis connection management for async operations."""

from collections.abc import AsyncGenerator

from redis.asyncio import Redis

from app.config import get_settings

_redis_client: Redis | None = None


async def init_redis() -> None:
    """Initialize Redis connection on startup."""
    global _redis_client
    settings = get_settings()
    _redis_client = Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
    )


async def close_redis() -> None:
    """Close Redis connection on shutdown."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


def get_redis_client() -> Redis:
    """Get the Redis client instance.

    Returns:
        Redis client instance.

    Raises:
        RuntimeError: If Redis is not initialized.
    """
    if _redis_client is None:
        raise RuntimeError("Redis client is not initialized")
    return _redis_client


async def get_redis() -> AsyncGenerator[Redis]:
    """FastAPI dependency for Redis client."""
    yield get_redis_client()
