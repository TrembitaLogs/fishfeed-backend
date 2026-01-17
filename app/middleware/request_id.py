"""Middleware for request ID correlation."""

import uuid
from collections.abc import Awaitable, Callable

import sentry_sdk
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.logging import get_logger, request_id_var

logger = get_logger(__name__)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Middleware to add request_id to each request for correlation."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Generate or use existing request_id from header
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        # Set in context variable for logging
        request_id_var.set(request_id)

        # Set Sentry context
        with sentry_sdk.configure_scope() as scope:
            scope.set_tag("request_id", request_id)

        # Log request start
        logger.info(
            "request_started",
            method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else None,
        )

        response = await call_next(request)

        # Add request_id to response headers
        response.headers["X-Request-ID"] = request_id

        # Log request completion
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
        )

        return response
