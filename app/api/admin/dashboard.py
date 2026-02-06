"""Admin dashboard endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentAdmin
from app.schemas.admin import DashboardResponse
from app.services.admin import get_dashboard_stats

router = APIRouter(prefix="/dashboard", tags=["admin-dashboard"])


@router.get(
    "",
    response_model=DashboardResponse,
    summary="Get admin dashboard statistics",
    description="Returns aggregated statistics across all models for the admin dashboard.",
)
async def get_dashboard(
    admin: CurrentAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DashboardResponse:
    """Return aggregated dashboard statistics.

    Requires admin privileges. Aggregates user, aquarium, feeding,
    AI scan, and gamification statistics.
    """
    return await get_dashboard_stats(db)
