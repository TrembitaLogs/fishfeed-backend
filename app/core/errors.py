"""Error code contract for API responses.

All application-level errors carry a stable string ``error_code`` that clients
(particularly the mobile app) can use to render localized messages. The English
``detail`` text remains for logs and as a fallback.

Response format::

    {"error_code": "auth.invalid_credentials", "detail": "Invalid email or password"}
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ErrorCode(str, Enum):
    """Stable, machine-readable error codes returned to clients.

    Values use dotted ``namespace.identifier`` form. Never rename a value once
    shipped — clients depend on it for localization mapping.
    """

    # Generic / framework
    VALIDATION_ERROR = "validation.error"
    INTERNAL_ERROR = "server.internal_error"
    RATE_LIMITED = "rate_limited"

    # Auth namespace
    AUTH_EMAIL_EXISTS = "auth.email_already_exists"
    AUTH_INVALID_CREDENTIALS = "auth.invalid_credentials"
    AUTH_INVALID_REFRESH_TOKEN = "auth.invalid_refresh_token"
    AUTH_INVALID_OAUTH_TOKEN = "auth.invalid_oauth_token"
    AUTH_OAUTH_NOT_CONFIGURED = "auth.oauth_not_configured"
    AUTH_INVALID_RESET_TOKEN = "auth.invalid_reset_token"
    AUTH_OAUTH_PASSWORD_CHANGE_DISALLOWED = "auth.oauth_password_change_disallowed"
    AUTH_INVALID_OLD_PASSWORD = "auth.invalid_old_password"


class AppError(Exception):
    """Base class for errors that map to HTTP responses with stable error codes."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.headers = headers
        super().__init__(message)


def _error_payload(code: str | None, detail: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"error_code": code, "detail": detail}
    payload.update(extra)
    return payload


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handler for :class:`AppError` — returns standardized error_code/detail JSON."""
    assert isinstance(exc, AppError)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(exc.code.value, exc.message),
        headers=exc.headers,
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handler for :class:`RequestValidationError` — adds error_code to Pydantic 422."""
    assert isinstance(exc, RequestValidationError)
    return JSONResponse(
        status_code=422,
        content=_error_payload(
            ErrorCode.VALIDATION_ERROR.value,
            "Validation failed",
            errors=jsonable_encoder(exc.errors()),
        ),
    )


def register_error_handlers(app: FastAPI) -> None:
    """Wire the standardized error handlers onto a FastAPI app."""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
