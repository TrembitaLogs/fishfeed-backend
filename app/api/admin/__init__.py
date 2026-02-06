"""Admin API package — aggregates sub-routers for admin endpoints."""

from fastapi import APIRouter

from app.api.admin.analytics import router as analytics_router
from app.api.admin.dashboard import router as dashboard_router
from app.api.admin.subscriptions import router as subscriptions_router
from app.api.admin.users import router as users_router

router = APIRouter(prefix="/admin", tags=["admin"])
router.include_router(analytics_router)
router.include_router(dashboard_router)
router.include_router(users_router)
router.include_router(subscriptions_router)
