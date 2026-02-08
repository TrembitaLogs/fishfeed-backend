"""Redis-based rate limiting middleware with per-user and per-IP limits.

Implements sliding window rate limiting using Redis INCR with EXPIRE.
Provides global rate limiting, analytics-specific limits, and Slowloris protection.
"""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Request, Response
from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import get_settings
from app.logging import get_logger
from app.redis import get_redis_client
from app.utils.jwt import decode_token

logger = get_logger(__name__)

# Endpoints that bypass rate limiting
BYPASS_PATHS = frozenset({
    "/health",
    "/health/ready",
    "/health/live",
    "/docs",
    "/redoc",
    "/openapi.json",
})

# Endpoint-specific rate limits (path prefix -> requests per minute)
ENDPOINT_LIMITS: dict[str, int] = {}


@dataclass
class RateLimitInfo:
    """Rate limit check result with metadata for headers."""

    allowed: bool
    limit: int
    remaining: int
    reset_at: int  # Unix timestamp
    retry_after: int | None = None  # Seconds until reset


class RateLimiter:
    """Redis-based rate limiter using sliding window algorithm.

    Uses an atomic Lua script for INCR+EXPIRE to avoid race conditions.
    Keys automatically expire after the window period.
    """

    # Lua script: atomically increment and set TTL on first creation.
    # Returns the new count after increment.
    _INCR_WITH_EXPIRE_SCRIPT = """
    local current = redis.call('INCR', KEYS[1])
    if current == 1 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
    return current
    """

    def __init__(self, redis_client: Redis) -> None:
        """Initialize rate limiter with Redis client.

        Args:
            redis_client: Async Redis client instance.
        """
        self._redis = redis_client
        self._settings = get_settings()
        self._script = self._redis.register_script(self._INCR_WITH_EXPIRE_SCRIPT)

    def _get_key(self, identifier: str, key_type: str) -> str:
        """Generate Redis key for rate limiting.

        Args:
            identifier: User ID or IP hash.
            key_type: Type of limit (user, ip, endpoint).

        Returns:
            Redis key string.
        """
        prefix = self._settings.REDIS_KEY_PREFIX
        window = self._get_current_window()
        return f"{prefix}rate_limit:{key_type}:{identifier}:{window}"

    def _get_current_window(self) -> str:
        """Get current time window identifier.

        Returns:
            Window identifier based on current minute.
        """
        now = datetime.now(UTC)
        return now.strftime("%Y%m%d%H%M")

    def _get_window_reset_timestamp(self) -> int:
        """Calculate when the current rate limit window resets.

        Returns:
            Unix timestamp of next minute boundary.
        """
        now = datetime.now(UTC)
        next_minute = now.replace(second=0, microsecond=0)
        # Add window seconds
        window_seconds = self._settings.RATE_LIMIT_WINDOW_SECONDS
        reset_time = next_minute.timestamp() + window_seconds
        return int(reset_time)

    async def check_rate_limit(
        self,
        identifier: str,
        key_type: str,
        limit: int,
    ) -> RateLimitInfo:
        """Check and increment rate limit counter.

        Uses Redis INCR which is atomic and handles race conditions.

        Args:
            identifier: User ID or IP hash.
            key_type: Type of limit (user, ip, endpoint).
            limit: Maximum requests allowed per window.

        Returns:
            RateLimitInfo with allowed status and metadata.
        """
        key = self._get_key(identifier, key_type)
        window_seconds = self._settings.RATE_LIMIT_WINDOW_SECONDS
        reset_at = self._get_window_reset_timestamp()

        # Atomic increment + expire via Lua script (no race condition)
        current_count = await self._script(keys=[key], args=[window_seconds])

        remaining = max(0, limit - current_count)

        if current_count > limit:
            retry_after = reset_at - int(datetime.now(UTC).timestamp())
            return RateLimitInfo(
                allowed=False,
                limit=limit,
                remaining=0,
                reset_at=reset_at,
                retry_after=max(1, retry_after),
            )

        return RateLimitInfo(
            allowed=True,
            limit=limit,
            remaining=remaining,
            reset_at=reset_at,
        )

    async def get_current_count(self, identifier: str, key_type: str) -> int:
        """Get current request count without incrementing.

        Args:
            identifier: User ID or IP hash.
            key_type: Type of limit.

        Returns:
            Current count, 0 if no requests yet.
        """
        key = self._get_key(identifier, key_type)
        count_str = await self._redis.get(key)
        return int(count_str) if count_str else 0


def _hash_ip(ip: str, salt: str) -> str:
    """Hash IP address for privacy-preserving rate limiting.

    Args:
        ip: Client IP address.
        salt: Salt for hashing.

    Returns:
        Hashed IP string.
    """
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:16]


def _extract_user_id_from_request(request: Request) -> str | None:
    """Extract user ID from JWT token in Authorization header.

    Args:
        request: FastAPI request object.

    Returns:
        User ID string or None if not authenticated.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:]  # Remove "Bearer " prefix
    try:
        payload = decode_token(token)
        if payload is None:
            return None
        return payload.get("sub")
    except Exception:
        return None


def _get_client_ip(request: Request) -> str:
    """Extract client IP, only trusting proxy headers from trusted proxies.

    Only uses X-Forwarded-For / X-Real-IP if the direct connecting IP
    is in TRUSTED_PROXIES. When trusted, uses the last IP in X-Forwarded-For
    (the one appended by the trusted reverse proxy closest to the client).

    Args:
        request: FastAPI request object.

    Returns:
        Client IP address.
    """
    settings = get_settings()
    direct_ip = request.client.host if request.client else "unknown"

    # Only trust proxy headers if direct client is a trusted proxy
    if direct_ip not in settings.TRUSTED_PROXIES:
        return direct_ip

    # Check X-Forwarded-For for proxied requests
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Use the rightmost IP that is NOT a trusted proxy.
        # The rightmost entry is appended by the closest trusted proxy.
        ips = [ip.strip() for ip in forwarded_for.split(",")]
        for ip in reversed(ips):
            if ip not in settings.TRUSTED_PROXIES:
                return ip

    # Check X-Real-IP
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip

    # Fall back to direct connection
    return direct_ip


def _get_endpoint_limit(path: str) -> int | None:
    """Get endpoint-specific rate limit if configured.

    Args:
        path: Request path.

    Returns:
        Rate limit for endpoint or None for default.
    """
    settings = get_settings()

    # Analytics endpoints have specific limits
    if path == "/analytics/events":
        return settings.RATE_LIMIT_ANALYTICS_EVENTS_PER_MIN
    if path == "/analytics/events/batch":
        return settings.RATE_LIMIT_ANALYTICS_BATCH_PER_MIN

    # Check configured endpoint limits
    for prefix, limit in ENDPOINT_LIMITS.items():
        if path.startswith(prefix):
            return limit

    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limiting middleware with per-user and per-IP limits.

    Applies global rate limits and endpoint-specific limits.
    Adds rate limit headers to all responses.
    Returns 429 Too Many Requests when limits exceeded.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        settings = get_settings()

        # Skip if rate limiting disabled
        if not settings.RATE_LIMIT_ENABLED:
            return await call_next(request)

        # Bypass rate limiting for health checks and docs
        if request.url.path in BYPASS_PATHS:
            return await call_next(request)

        try:
            redis = get_redis_client()
        except RuntimeError:
            # Redis not initialized, skip rate limiting
            logger.warning("rate_limit_skipped", reason="redis_not_initialized")
            return await call_next(request)

        limiter = RateLimiter(redis)
        salt = settings.ANALYTICS_IP_SALT

        # Get client identifiers
        client_ip = _get_client_ip(request)
        ip_hash = _hash_ip(client_ip, salt)
        user_id = _extract_user_id_from_request(request)

        # Check per-IP limit first (applies to all requests)
        ip_limit = settings.RATE_LIMIT_IP_PER_MIN
        ip_result = await limiter.check_rate_limit(ip_hash, "ip", ip_limit)

        if not ip_result.allowed:
            logger.warning(
                "rate_limit_exceeded",
                limit_type="ip",
                ip_hash=ip_hash,
                path=request.url.path,
            )
            return _create_rate_limit_response(ip_result)

        # Check per-user limit for authenticated requests
        user_result: RateLimitInfo | None = None
        if user_id:
            user_limit = settings.RATE_LIMIT_USER_PER_MIN

            # Check endpoint-specific limit
            endpoint_limit = _get_endpoint_limit(request.url.path)
            if endpoint_limit is not None:
                endpoint_result = await limiter.check_rate_limit(
                    f"{user_id}:{request.url.path}",
                    "endpoint",
                    endpoint_limit,
                )
                if not endpoint_result.allowed:
                    logger.warning(
                        "rate_limit_exceeded",
                        limit_type="endpoint",
                        user_id=user_id,
                        path=request.url.path,
                    )
                    return _create_rate_limit_response(endpoint_result)

            # Global per-user limit
            user_result = await limiter.check_rate_limit(user_id, "user", user_limit)
            if not user_result.allowed:
                logger.warning(
                    "rate_limit_exceeded",
                    limit_type="user",
                    user_id=user_id,
                    path=request.url.path,
                )
                return _create_rate_limit_response(user_result)

        # Process request
        response = await call_next(request)

        # Add rate limit headers (prefer user limit info if available)
        rate_info = user_result if user_result else ip_result
        response.headers["X-RateLimit-Limit"] = str(rate_info.limit)
        response.headers["X-RateLimit-Remaining"] = str(rate_info.remaining)
        response.headers["X-RateLimit-Reset"] = str(rate_info.reset_at)

        return response


def _create_rate_limit_response(rate_info: RateLimitInfo) -> JSONResponse:
    """Create 429 Too Many Requests response.

    Args:
        rate_info: Rate limit information.

    Returns:
        JSONResponse with 429 status and appropriate headers.
    """
    headers = {
        "X-RateLimit-Limit": str(rate_info.limit),
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": str(rate_info.reset_at),
        "Retry-After": str(rate_info.retry_after or 60),
    }

    return JSONResponse(
        status_code=429,
        content={
            "detail": "Too many requests. Please try again later.",
            "retry_after": rate_info.retry_after,
        },
        headers=headers,
    )


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    """Middleware for request timeout protection (Slowloris mitigation).

    Cancels request processing if it exceeds the configured timeout.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        settings = get_settings()
        timeout = settings.REQUEST_TIMEOUT_SECONDS

        try:
            response = await asyncio.wait_for(
                call_next(request),
                timeout=timeout,
            )
            return response
        except TimeoutError:
            logger.warning(
                "request_timeout",
                path=request.url.path,
                timeout_seconds=timeout,
            )
            return JSONResponse(
                status_code=504,
                content={"detail": "Request timeout"},
            )


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Middleware for request body size limiting (Slowloris mitigation).

    Rejects requests with body size exceeding the configured limit.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        settings = get_settings()
        max_size = settings.MAX_REQUEST_BODY_SIZE_MB * 1024 * 1024  # Convert to bytes

        # Check Content-Length header
        content_length = request.headers.get("Content-Length")
        if content_length:
            if int(content_length) > max_size:
                logger.warning(
                    "request_too_large",
                    path=request.url.path,
                    content_length=content_length,
                    max_size=max_size,
                )
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large"},
                )

        return await call_next(request)
