"""Tests for Prometheus metrics middleware."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_fastapi_instrumentator import Instrumentator


@pytest.fixture
def metrics_app():
    """Create a minimal FastAPI app with Prometheus instrumentation."""
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/test")
    async def api_test():
        return {"ok": True}

    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/health", "/metrics"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    return app


@pytest.mark.asyncio
class TestMetricsEndpoint:
    """Tests for GET /metrics endpoint (Prometheus scraping)."""

    async def test_metrics_endpoint_returns_prometheus_format(self, metrics_app: FastAPI):
        """Test that /metrics returns Prometheus text format when enabled."""
        transport = ASGITransport(app=metrics_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/api/test")

            response = await client.get("/metrics")
            assert response.status_code == 200
            assert "text/plain" in response.headers["content-type"]

            body = response.text
            assert "http_request_duration_seconds" in body
            assert "http_requests_total" in body

    async def test_metrics_excludes_health_endpoint(self, metrics_app: FastAPI):
        """Test that /health requests are excluded from metrics."""
        transport = ASGITransport(app=metrics_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/health")
            await client.get("/api/test")

            response = await client.get("/metrics")
            body = response.text
            assert "/api/test" in body
            assert '"/health"' not in body

    async def test_metrics_not_in_openapi_schema(self, metrics_app: FastAPI):
        """Test that /metrics endpoint is excluded from OpenAPI schema."""
        transport = ASGITransport(app=metrics_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/openapi.json")
            schema = response.json()
            assert "/metrics" not in schema.get("paths", {})
