from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import sentry_sdk
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.middleware.sessions import SessionMiddleware

from app.admin.setup import setup_admin
from app.api import (
    admin_router,
    ai_router,
    aquariums_router,
    auth_router,
    family_router,
    feeding_router,
    fish_router,
    gamification_router,
    health_router,
    images_router,
    purchase_router,
    push_router,
    releases_router,
    species_admin_router,
    species_router,
    sync_router,
    users_router,
)
from app.config import get_settings
from app.core.errors import register_error_handlers
from app.database import close_db, init_db
from app.logging import configure_logging, get_logger
from app.middleware import (
    RateLimitMiddleware,
    RequestIdMiddleware,
    RequestSizeLimitMiddleware,
    RequestTimeoutMiddleware,
)
from app.redis import close_redis, init_redis
from app.workers.feeding_worker import start_scheduler, stop_scheduler

settings = get_settings()

# Configure structured logging
configure_logging()
logger = get_logger(__name__)

# Initialize Sentry for error tracking and performance monitoring
if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=False,
        enable_tracing=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Startup
    logger.info("app_starting", app_name=settings.APP_NAME, version=settings.APP_VERSION)
    await init_db()
    await init_redis()
    if settings.WORKER_ENABLED:
        await start_scheduler()
    yield
    # Shutdown
    if settings.WORKER_ENABLED:
        await stop_scheduler()
    await close_redis()
    await close_db()
    logger.info("app_shutdown", app_name=settings.APP_NAME)


_is_production = settings.ENVIRONMENT == "production"

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Backend API for FishFeed aquarium management system",
    lifespan=lifespan,
    docs_url=None if _is_production else "/docs",
    redoc_url=None if _is_production else "/redoc",
    openapi_url=None if _is_production else "/openapi.json",
)

# Standardized error responses with stable error_code (consumed by mobile l10n)
register_error_handlers(app)

# SQLAdmin panel (must be mounted before middleware/routers)
setup_admin(app)

# Middleware registration order matters - first registered = outermost wrapper
# Execution order for incoming requests: RequestId -> SizeLimit -> RateLimit -> Timeout -> CORS
app.add_middleware(RequestIdMiddleware)  # Outermost - adds correlation ID for logging
app.add_middleware(RequestSizeLimitMiddleware)  # Reject large requests early
app.add_middleware(RateLimitMiddleware)  # Check rate limits before processing
app.add_middleware(RequestTimeoutMiddleware)  # Timeout protection for request processing
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)
app.add_middleware(SessionMiddleware, secret_key=settings.SESSION_SECRET_KEY, https_only=_is_production)

# Prometheus request metrics (latency, status codes, throughput)
if settings.METRICS_ENABLED:
    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/health", "/metrics"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


# Health and releases are not versioned
app.include_router(health_router)
app.include_router(releases_router)

# Well-known endpoints (App Links / Universal Links)
from app.api.well_known import router as well_known_router  # noqa: E402

app.include_router(well_known_router)

# Web endpoints (deep link landing pages)
from app.api.web import router as web_router  # noqa: E402

app.include_router(web_router)

# All API routes under /api/v1
api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(auth_router)
api_v1.include_router(users_router)
api_v1.include_router(species_router)
api_v1.include_router(species_admin_router)
api_v1.include_router(aquariums_router)
api_v1.include_router(fish_router)
api_v1.include_router(feeding_router)
api_v1.include_router(sync_router)
api_v1.include_router(family_router)
api_v1.include_router(push_router)
api_v1.include_router(ai_router)
api_v1.include_router(gamification_router)
api_v1.include_router(images_router)
api_v1.include_router(purchase_router)
api_v1.include_router(admin_router)
app.include_router(api_v1)


# ── OpenAPI post-processing ──────────────────────────────────────────
# FastAPI's generated schema omits responses the handlers and middleware really
# return, which makes the Schemathesis conformance suite (tests/conformance)
# flag them as undocumented. We enrich the generated schema so it matches
# runtime reality — no handler behavior changes:
#   * Versioned /api/v1 operations document the standard error envelope codes
#     they can return: 400 (business validation), 401/403 (auth), 404 (missing
#     resource), 409 (conflict) and 429 (rate limiting — RateLimitMiddleware is
#     global). These come from app/core/errors.py and app/middleware/.
#   * Non-versioned operations get 401/404 only where applicable (auth-issuing
#     endpoints, templated paths, the releases lookup/static file routes).
#   * 422 bodies use the app's standardized error envelope (app/core/errors.py),
#     not FastAPI's default HTTPValidationError shape.
#   * password fields carry their real complexity pattern so generated example
#     data is accepted by the field validators (app/schemas/auth.py).
_OPENAPI_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_API_PATH_PREFIX = "/api/v1/"
_AUTH_PATH_PREFIX = "/api/v1/auth/"
_RELEASES_PATH_PREFIX = "/mobile/releases"
# Public lookups that can 404 despite having no path template parameter.
_RESOURCE_LOOKUP_PATHS = {"/mobile/releases/index.json"}
# Standard error responses any versioned API operation may return. Description
# only (no schema) so we never assert a body shape the handler doesn't promise.
_STANDARD_API_ERRORS = {
    "400": "Bad Request",
    "401": "Unauthorized",
    "403": "Forbidden",
    "404": "Not Found",
    "409": "Conflict",
    "429": "Too Many Requests",
}
# Mirror of PASSWORD_PATTERN in app/schemas/auth.py (one uppercase, one
# lowercase, one digit). The rule lives in a field_validator, so FastAPI does
# not emit it; publish it here so example generators produce accepted values.
_PASSWORD_PATTERN = r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).+$"
_PASSWORD_SCHEMA_FIELDS = {
    "RegisterRequest": ("password",),
    "PasswordResetConfirmRequest": ("new_password",),
    "PasswordChangeRequest": ("new_password",),
}
_ERROR_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "ErrorResponse",
    "description": "Standardized error envelope returned by the API exception handlers.",
    "properties": {
        "error_code": {"type": "string", "description": "Stable machine-readable error code."},
        "detail": {"type": "string", "description": "Human-readable error message."},
        "errors": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Field-level validation errors (present on 422 validation failures).",
        },
    },
    "required": ["error_code", "detail"],
}

_default_openapi = app.openapi


def custom_openapi() -> dict[str, Any]:
    """Enrich the generated OpenAPI so it matches the handlers' real responses.

    The schema is the contract the Schemathesis conformance suite validates
    against; see tests/conformance/test_openapi_conformance.py.
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = _default_openapi()

    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    schemas["ErrorResponse"] = _ERROR_RESPONSE_SCHEMA

    # Document the real password complexity constraint.
    for schema_name, fields in _PASSWORD_SCHEMA_FIELDS.items():
        properties = schemas.get(schema_name, {}).get("properties", {})
        for field in fields:
            if field in properties:
                properties[field]["pattern"] = _PASSWORD_PATTERN

    error_content = {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}

    for path, path_item in schema.get("paths", {}).items():
        is_api_path = path.startswith(_API_PATH_PREFIX)
        is_auth_path = path.startswith(_AUTH_PATH_PREFIX)
        is_releases_path = path.startswith(_RELEASES_PATH_PREFIX)
        needs_404 = "{" in path or path in _RESOURCE_LOOKUP_PATHS
        for method, operation in path_item.items():
            if method.lower() not in _OPENAPI_HTTP_METHODS:
                continue
            responses = operation.setdefault("responses", {})

            if is_api_path:
                # Every versioned API operation may surface the standard error
                # codes (auth, validation, conflict, rate limit). setdefault
                # keeps any richer per-operation declaration (e.g. a 409 model).
                for code, description in _STANDARD_API_ERRORS.items():
                    responses.setdefault(code, {"description": description})
            else:
                # Auth-protected operations and the credential-issuing auth
                # endpoints return 401 on missing/invalid credentials or tokens.
                if ("security" in operation or is_auth_path) and "401" not in responses:
                    responses["401"] = {"description": "Unauthorized"}
                # The releases static-file routes guard against path traversal
                # (403) and missing files (404).
                if is_releases_path:
                    responses.setdefault("403", {"description": "Forbidden"})
                # Operations addressing a specific resource return 404 when it
                # does not exist or is inaccessible to the caller.
                if needs_404 and "404" not in responses:
                    responses["404"] = {"description": "Not Found"}

            # The validation handler returns the standardized error envelope,
            # not FastAPI's default HTTPValidationError shape.
            if "422" in responses:
                responses["422"].setdefault("description", "Validation Error")
                responses["422"]["content"] = error_content

    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi  # type: ignore[method-assign]
