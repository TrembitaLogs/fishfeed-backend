"""Tests for health check endpoints."""

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio(loop_scope="session")
class TestHealthLiveness:
    """Tests for GET /health endpoint (liveness probe)."""

    async def test_health_returns_ok(self, client: AsyncClient):
        """Test that /health returns status ok."""
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data

    async def test_health_returns_version(self, client: AsyncClient):
        """Test that /health includes app version."""
        response = await client.get("/health")
        data = response.json()
        assert "version" in data
        assert isinstance(data["version"], str)


@pytest.mark.asyncio(loop_scope="session")
class TestHealthReadiness:
    """Tests for GET /health/ready endpoint (readiness probe)."""

    async def test_health_ready_returns_ok_when_healthy(self, client: AsyncClient):
        """Test that /health/ready returns 200 when all services are healthy."""
        response = await client.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["db_connected"] is True
        assert data["redis_connected"] is True
        assert "version" in data
        assert "uptime_seconds" in data

    async def test_health_ready_returns_uptime(self, client: AsyncClient):
        """Test that /health/ready includes uptime."""
        response = await client.get("/health/ready")
        data = response.json()
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], (int, float))
        assert data["uptime_seconds"] >= 0

    async def test_health_ready_returns_503_when_db_down(self, client: AsyncClient, app):
        """Test that /health/ready returns 503 when database is unavailable."""
        from app.database import get_db

        async def failing_db():
            mock_session = AsyncMock()
            mock_session.execute.side_effect = Exception("Database connection failed")
            yield mock_session

        original_override = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = failing_db

        try:
            response = await client.get("/health/ready")
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "degraded"
            assert data["db_connected"] is False
        finally:
            if original_override:
                app.dependency_overrides[get_db] = original_override
            else:
                app.dependency_overrides.pop(get_db, None)

    async def test_health_ready_returns_503_when_redis_down(
        self, client: AsyncClient, app
    ):
        """Test that /health/ready returns 503 when Redis is unavailable."""
        from app.redis import get_redis

        async def failing_redis():
            mock_redis = AsyncMock()
            mock_redis.ping.side_effect = Exception("Redis connection failed")
            yield mock_redis

        original_override = app.dependency_overrides.get(get_redis)
        app.dependency_overrides[get_redis] = failing_redis

        try:
            response = await client.get("/health/ready")
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "degraded"
            assert data["redis_connected"] is False
        finally:
            if original_override:
                app.dependency_overrides[get_redis] = original_override
            else:
                app.dependency_overrides.pop(get_redis, None)
