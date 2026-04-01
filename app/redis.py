"""Redis connection management with connection pooling for async operations."""

from collections.abc import AsyncGenerator

from redis.asyncio import ConnectionPool, Redis

from app.config import get_settings

_redis_client: Redis | None = None
_connection_pool: ConnectionPool | None = None


async def init_redis() -> None:
    """Initialize Redis connection pool and client on startup."""
    global _redis_client, _connection_pool
    settings = get_settings()
    _connection_pool = ConnectionPool.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        max_connections=settings.REDIS_POOL_MAX_SIZE,
        socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
        socket_connect_timeout=settings.REDIS_SOCKET_CONNECT_TIMEOUT,
        retry_on_timeout=settings.REDIS_RETRY_ON_TIMEOUT,
        health_check_interval=settings.REDIS_HEALTH_CHECK_INTERVAL,
    )
    _redis_client = Redis(connection_pool=_connection_pool)


async def close_redis() -> None:
    """Close Redis connection pool on shutdown."""
    global _redis_client, _connection_pool
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
    if _connection_pool is not None:
        await _connection_pool.aclose()
        _connection_pool = None


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
