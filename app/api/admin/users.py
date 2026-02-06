"""Admin user management endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentAdmin
from app.redis import get_redis
from app.schemas.admin import GrantPremiumRequest, UserActionResponse
from app.services.admin import ban_user, grant_premium, reset_ai_scans, unban_user

router = APIRouter(prefix="/users", tags=["admin-users"])


@router.post(
    "/{user_id}/ban",
    response_model=UserActionResponse,
    summary="Ban a user",
    description="Soft-delete the user and revoke all refresh tokens.",
)
async def ban(
    user_id: UUID,
    admin: CurrentAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> UserActionResponse:
    """Ban a user by setting deleted_at and removing refresh tokens."""
    await ban_user(db, redis, user_id)
    return UserActionResponse(
        user_id=user_id,
        action="ban",
        success=True,
        message="User has been banned",
    )


@router.post(
    "/{user_id}/unban",
    response_model=UserActionResponse,
    summary="Unban a user",
    description="Clear the soft-delete timestamp to reactivate the user account.",
)
async def unban(
    user_id: UUID,
    admin: CurrentAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserActionResponse:
    """Unban a user by clearing deleted_at."""
    await unban_user(db, user_id)
    return UserActionResponse(
        user_id=user_id,
        action="unban",
        success=True,
        message="User has been unbanned",
    )


@router.post(
    "/{user_id}/reset-ai-scans",
    response_model=UserActionResponse,
    summary="Reset AI scan quota",
    description="Restore the user's free AI scan quota to the default value.",
)
async def reset_scans(
    user_id: UUID,
    admin: CurrentAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserActionResponse:
    """Reset a user's free AI scan quota."""
    await reset_ai_scans(db, user_id)
    return UserActionResponse(
        user_id=user_id,
        action="reset-ai-scans",
        success=True,
        message="AI scan quota has been reset",
    )


@router.post(
    "/{user_id}/grant-premium",
    response_model=UserActionResponse,
    summary="Grant premium subscription",
    description="Grant premium subscription to a user for a specified number of days.",
)
async def grant_premium_endpoint(
    user_id: UUID,
    body: GrantPremiumRequest,
    admin: CurrentAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserActionResponse:
    """Grant premium subscription to a user."""
    await grant_premium(db, user_id, body.days)
    return UserActionResponse(
        user_id=user_id,
        action="grant-premium",
        success=True,
        message=f"Premium granted for {body.days} days",
    )
