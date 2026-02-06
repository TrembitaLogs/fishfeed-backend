from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    purchase_router,
    push_router,
    releases_router,
    species_admin_router,
    species_router,
    sync_router,
    users_router,
)
from app.config import get_settings
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


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Backend API for FishFeed aquarium management system",
    lifespan=lifespan,
)

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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health and releases are not versioned
app.include_router(health_router)
app.include_router(releases_router)

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
api_v1.include_router(purchase_router)
api_v1.include_router(admin_router)
app.include_router(api_v1)
