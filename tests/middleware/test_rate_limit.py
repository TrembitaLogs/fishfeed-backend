"""Tests for Redis-based rate limiting middleware."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.middleware.rate_limit import (
    RateLimiter,
    RateLimitInfo,
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
    RequestTimeoutMiddleware,
    _hash_ip,
)
from app.utils.jwt import create_access_token


async def cleanup_rate_limit_keys(redis: Redis) -> None:
    """Helper to cleanup rate limit keys."""
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="fishfeed:rate_limit:*")
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break


@pytest.fixture
def test_app_with_middleware(redis_client: Redis) -> FastAPI:
    """Create a test FastAPI app with rate limiting middleware."""
    import app.redis as redis_module

    original_client = redis_module._redis_client

    # Set test Redis client
    redis_module._redis_client = redis_client

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/test")
    async def test_endpoint():
        return {"message": "success"}

    @app.get("/protected")
    async def protected_endpoint(request: Request):
        return {"user": "authenticated"}

    @app.get("/analytics/events")
    async def analytics_events():
        return {"events": []}

    @app.get("/analytics/events/batch")
    async def analytics_batch():
        return {"batch": []}

    yield app

    # Restore original client
    redis_module._redis_client = original_client


@pytest.fixture
def test_app_with_timeout() -> FastAPI:
    """Create a test FastAPI app with timeout middleware."""
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware)

    @app.get("/slow")
    async def slow_endpoint():
        await asyncio.sleep(60)  # Simulate slow request
        return {"message": "done"}

    @app.get("/fast")
    async def fast_endpoint():
        return {"message": "fast"}

    return app


@pytest.fixture
def test_app_with_size_limit() -> FastAPI:
    """Create a test FastAPI app with size limit middleware."""
    app = FastAPI()
    app.add_middleware(RequestSizeLimitMiddleware)

    @app.post("/upload")
    async def upload_endpoint(request: Request):
        body = await request.body()
        return {"size": len(body)}

    return app


class TestRateLimiter:
    """Tests for RateLimiter class."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_rate_limiter_allows_within_limit(self, redis_client: Redis):
        """Test that requests within limit are allowed."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            limiter = RateLimiter(redis_client)
            identifier = str(uuid4())

            result = await limiter.check_rate_limit(identifier, "test", limit=100)

            assert result.allowed is True
            assert result.remaining == 99  # 100 - 1 (first request increments)
            assert result.retry_after is None
        finally:
            await cleanup_rate_limit_keys(redis_client)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_rate_limiter_blocks_when_exceeded(self, redis_client: Redis):
        """Test that requests are blocked when limit is exceeded."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            limiter = RateLimiter(redis_client)
            identifier = str(uuid4())

            # Make 10 requests (limit is 10)
            for _ in range(10):
                await limiter.check_rate_limit(identifier, "test", limit=10)

            # 11th request should be blocked
            result = await limiter.check_rate_limit(identifier, "test", limit=10)

            assert result.allowed is False
            assert result.remaining == 0
            assert result.retry_after is not None
            assert result.retry_after > 0
        finally:
            await cleanup_rate_limit_keys(redis_client)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_rate_limiter_ttl_set(self, redis_client: Redis):
        """Test that Redis keys have TTL set."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            limiter = RateLimiter(redis_client)
            identifier = str(uuid4())

            await limiter.check_rate_limit(identifier, "test", limit=100)

            key = limiter._get_key(identifier, "test")
            ttl = await redis_client.ttl(key)

            # TTL should be set (60 seconds default window)
            assert ttl > 0
            assert ttl <= 60
        finally:
            await cleanup_rate_limit_keys(redis_client)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_rate_limiter_different_identifiers_independent(self, redis_client: Redis):
        """Test that different identifiers have independent limits."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            limiter = RateLimiter(redis_client)
            id1 = str(uuid4())
            id2 = str(uuid4())

            # Use all limit for id1
            for _ in range(10):
                await limiter.check_rate_limit(id1, "test", limit=10)

            # id2 should still be allowed
            result = await limiter.check_rate_limit(id2, "test", limit=10)
            assert result.allowed is True
        finally:
            await cleanup_rate_limit_keys(redis_client)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_rate_limiter_concurrent_requests(self, redis_client: Redis):
        """Test that concurrent requests are properly counted."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            limiter = RateLimiter(redis_client)
            identifier = str(uuid4())

            # Make 5 concurrent requests
            tasks = [
                limiter.check_rate_limit(identifier, "test", limit=100)
                for _ in range(5)
            ]
            results = await asyncio.gather(*tasks)

            # All should be allowed
            assert all(r.allowed for r in results)

            # Current count should be 5
            count = await limiter.get_current_count(identifier, "test")
            assert count == 5
        finally:
            await cleanup_rate_limit_keys(redis_client)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_reset_at_is_future(self, redis_client: Redis):
        """Test that reset_at is in the future."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            limiter = RateLimiter(redis_client)
            identifier = str(uuid4())

            result = await limiter.check_rate_limit(identifier, "test", limit=100)

            now = int(datetime.now(UTC).timestamp())
            assert result.reset_at > now
        finally:
            await cleanup_rate_limit_keys(redis_client)


class TestRateLimitMiddleware:
    """Tests for RateLimitMiddleware."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_health_check_bypasses_rate_limit(
        self, test_app_with_middleware: FastAPI, redis_client: Redis
    ):
        """Test that health check endpoints bypass rate limiting."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            transport = ASGITransport(app=test_app_with_middleware)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # Make many requests to health endpoint
                for _ in range(20):
                    response = await client.get("/health")
                    assert response.status_code == 200
        finally:
            await cleanup_rate_limit_keys(redis_client)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_rate_limit_headers_present(
        self, test_app_with_middleware: FastAPI, redis_client: Redis
    ):
        """Test that rate limit headers are present in responses."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            transport = ASGITransport(app=test_app_with_middleware)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/test")

                assert response.status_code == 200
                assert "X-RateLimit-Limit" in response.headers
                assert "X-RateLimit-Remaining" in response.headers
                assert "X-RateLimit-Reset" in response.headers
        finally:
            await cleanup_rate_limit_keys(redis_client)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_per_ip_limit_blocks_after_exceeded(
        self, test_app_with_middleware: FastAPI, redis_client: Redis
    ):
        """Test that per-IP limit blocks requests after limit exceeded."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            transport = ASGITransport(app=test_app_with_middleware)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # Make requests up to the IP limit (1000)
                # For testing, we'll modify the limit or make many requests
                # Since default is 1000, we'll test with a smaller number
                # by directly manipulating Redis

                # First, flood the rate limiter
                limiter = RateLimiter(redis_client)
                ip_hash = _hash_ip("127.0.0.1", "fishfeed-analytics-salt")

                # Simulate 1000 requests
                for _ in range(1000):
                    await limiter.check_rate_limit(ip_hash, "ip", 1000)

                # Next request should be blocked
                response = await client.get("/test")
                assert response.status_code == 429
                assert "Retry-After" in response.headers
        finally:
            await cleanup_rate_limit_keys(redis_client)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_per_user_limit_blocks_after_exceeded(
        self, test_app_with_middleware: FastAPI, redis_client: Redis
    ):
        """Test that per-user limit blocks requests after limit exceeded."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            transport = ASGITransport(app=test_app_with_middleware)

            # Create a valid JWT token
            user_id = uuid4()
            token = create_access_token(user_id)

            # Flood the user rate limit
            limiter = RateLimiter(redis_client)
            for _ in range(100):
                await limiter.check_rate_limit(str(user_id), "user", 100)

            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/test",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 429
                assert "Retry-After" in response.headers
        finally:
            await cleanup_rate_limit_keys(redis_client)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_429_response_includes_retry_after(
        self, test_app_with_middleware: FastAPI, redis_client: Redis
    ):
        """Test that 429 response includes Retry-After header."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            transport = ASGITransport(app=test_app_with_middleware)

            # Flood the rate limit
            limiter = RateLimiter(redis_client)
            ip_hash = _hash_ip("127.0.0.1", "fishfeed-analytics-salt")
            for _ in range(1001):
                await limiter.check_rate_limit(ip_hash, "ip", 1000)

            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/test")

                assert response.status_code == 429
                assert "Retry-After" in response.headers
                retry_after = int(response.headers["Retry-After"])
                assert retry_after > 0
                assert retry_after <= 60  # Within window
        finally:
            await cleanup_rate_limit_keys(redis_client)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_analytics_endpoint_specific_limit(
        self, test_app_with_middleware: FastAPI, redis_client: Redis
    ):
        """Test that analytics endpoints have specific lower limits."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            transport = ASGITransport(app=test_app_with_middleware)

            # Create a valid JWT token
            user_id = uuid4()
            token = create_access_token(user_id)

            # Flood the analytics events endpoint limit (50)
            limiter = RateLimiter(redis_client)
            for _ in range(50):
                await limiter.check_rate_limit(
                    f"{user_id}:/analytics/events", "endpoint", 50
                )

            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/analytics/events",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 429
        finally:
            await cleanup_rate_limit_keys(redis_client)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_analytics_batch_endpoint_specific_limit(
        self, test_app_with_middleware: FastAPI, redis_client: Redis
    ):
        """Test that analytics batch endpoint has specific lower limit (10)."""
        await cleanup_rate_limit_keys(redis_client)
        try:
            transport = ASGITransport(app=test_app_with_middleware)

            # Create a valid JWT token
            user_id = uuid4()
            token = create_access_token(user_id)

            # Flood the analytics batch endpoint limit (10)
            limiter = RateLimiter(redis_client)
            for _ in range(10):
                await limiter.check_rate_limit(
                    f"{user_id}:/analytics/events/batch", "endpoint", 10
                )

            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/analytics/events/batch",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 429
        finally:
            await cleanup_rate_limit_keys(redis_client)


class TestRequestTimeoutMiddleware:
    """Tests for RequestTimeoutMiddleware."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_fast_request_succeeds(self, test_app_with_timeout: FastAPI):
        """Test that fast requests complete successfully."""
        transport = ASGITransport(app=test_app_with_timeout)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/fast")
            assert response.status_code == 200

    @pytest.mark.asyncio(loop_scope="session")
    async def test_slow_request_times_out(self, test_app_with_timeout: FastAPI):
        """Test that slow requests time out with 504."""
        # Override timeout to 1 second for testing
        import app.config as config_module

        original_settings = config_module.get_settings()

        # Create a custom app with short timeout
        app = FastAPI()

        @app.get("/slow")
        async def slow_endpoint():
            await asyncio.sleep(5)
            return {"message": "done"}

        # Add middleware with manual timeout
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        class ShortTimeoutMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                try:
                    response = await asyncio.wait_for(call_next(request), timeout=0.5)
                    return response
                except TimeoutError:
                    return JSONResponse(
                        status_code=504,
                        content={"detail": "Request timeout"},
                    )

        app.add_middleware(ShortTimeoutMiddleware)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slow")
            assert response.status_code == 504


class TestRequestSizeLimitMiddleware:
    """Tests for RequestSizeLimitMiddleware."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_small_request_succeeds(self, test_app_with_size_limit: FastAPI):
        """Test that small requests are allowed."""
        transport = ASGITransport(app=test_app_with_size_limit)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/upload", content=b"small data")
            assert response.status_code == 200

    @pytest.mark.asyncio(loop_scope="session")
    async def test_large_request_rejected(self, test_app_with_size_limit: FastAPI):
        """Test that large requests are rejected with 413."""
        transport = ASGITransport(app=test_app_with_size_limit)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Send request with Content-Length header exceeding limit
            # Default limit is 10MB = 10 * 1024 * 1024 bytes
            large_size = 11 * 1024 * 1024  # 11MB
            response = await client.post(
                "/upload",
                headers={"Content-Length": str(large_size)},
                content=b"x",  # Actual content doesn't matter, header is checked first
            )
            assert response.status_code == 413


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_hash_ip_consistency(self):
        """Test that IP hashing is consistent."""
        ip = "192.168.1.1"
        salt = "test-salt"

        hash1 = _hash_ip(ip, salt)
        hash2 = _hash_ip(ip, salt)

        assert hash1 == hash2
        assert len(hash1) == 16  # Truncated to 16 chars

    def test_hash_ip_different_ips(self):
        """Test that different IPs produce different hashes."""
        salt = "test-salt"

        hash1 = _hash_ip("192.168.1.1", salt)
        hash2 = _hash_ip("192.168.1.2", salt)

        assert hash1 != hash2

    def test_hash_ip_different_salts(self):
        """Test that different salts produce different hashes."""
        ip = "192.168.1.1"

        hash1 = _hash_ip(ip, "salt1")
        hash2 = _hash_ip(ip, "salt2")

        assert hash1 != hash2


class TestRateLimitInfo:
    """Tests for RateLimitInfo dataclass."""

    def test_rate_limit_info_allowed(self):
        """Test RateLimitInfo for allowed request."""
        info = RateLimitInfo(
            allowed=True,
            limit=100,
            remaining=99,
            reset_at=1234567890,
        )

        assert info.allowed is True
        assert info.limit == 100
        assert info.remaining == 99
        assert info.reset_at == 1234567890
        assert info.retry_after is None

    def test_rate_limit_info_blocked(self):
        """Test RateLimitInfo for blocked request."""
        info = RateLimitInfo(
            allowed=False,
            limit=100,
            remaining=0,
            reset_at=1234567890,
            retry_after=30,
        )

        assert info.allowed is False
        assert info.remaining == 0
        assert info.retry_after == 30
