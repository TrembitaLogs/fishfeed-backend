"""Tests for the error_code response contract."""

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from app.core.errors import AppError, ErrorCode, register_error_handlers


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise-app-error")
    def _raise_app_error() -> None:
        raise AppError(
            code=ErrorCode.AUTH_INVALID_CREDENTIALS,
            message="Invalid email or password",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    @app.get("/raise-with-headers")
    def _raise_with_headers() -> None:
        raise AppError(
            code=ErrorCode.RATE_LIMITED,
            message="Too many requests. Please try again later.",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "60"},
        )

    from pydantic import BaseModel

    class _Body(BaseModel):
        email: str

    @app.post("/needs-body")
    def _needs_body(body: _Body) -> dict[str, str]:
        return {"email": body.email}

    return app


def test_app_error_returns_standard_payload(app: FastAPI) -> None:
    client = TestClient(app)
    response = client.get("/raise-app-error")
    assert response.status_code == 401
    body = response.json()
    assert body == {
        "error_code": "auth.invalid_credentials",
        "detail": "Invalid email or password",
    }


def test_app_error_propagates_headers(app: FastAPI) -> None:
    client = TestClient(app)
    response = client.get("/raise-with-headers")
    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "60"
    body = response.json()
    assert body["error_code"] == "rate_limited"
    assert "Too many requests" in body["detail"]


def test_validation_error_returns_standard_payload(app: FastAPI) -> None:
    client = TestClient(app)
    # Missing required body — triggers RequestValidationError
    response = client.post("/needs-body", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "validation.error"
    assert body["detail"] == "Validation failed"
    # Pydantic field errors are preserved under "errors"
    assert isinstance(body.get("errors"), list)
    assert any("email" in str(err.get("loc", "")) for err in body["errors"])


def test_error_code_values_are_stable() -> None:
    """ErrorCode values are part of the public API contract — guard against renames."""
    # Sentinel: if any of these strings change, mobile mappings must change too.
    assert ErrorCode.AUTH_EMAIL_EXISTS.value == "auth.email_already_exists"
    assert ErrorCode.AUTH_INVALID_CREDENTIALS.value == "auth.invalid_credentials"
    assert ErrorCode.AUTH_INVALID_REFRESH_TOKEN.value == "auth.invalid_refresh_token"
    assert ErrorCode.AUTH_INVALID_OAUTH_TOKEN.value == "auth.invalid_oauth_token"
    assert ErrorCode.AUTH_OAUTH_NOT_CONFIGURED.value == "auth.oauth_not_configured"
    assert ErrorCode.AUTH_INVALID_RESET_TOKEN.value == "auth.invalid_reset_token"
    assert ErrorCode.AUTH_OAUTH_PASSWORD_CHANGE_DISALLOWED.value == "auth.oauth_password_change_disallowed"
    assert ErrorCode.AUTH_INVALID_OLD_PASSWORD.value == "auth.invalid_old_password"
    assert ErrorCode.RATE_LIMITED.value == "rate_limited"
    assert ErrorCode.VALIDATION_ERROR.value == "validation.error"
