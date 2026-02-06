"""Admin subscription management endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentAdmin
from app.schemas.admin import SubscriptionResponse, UpdateSubscriptionRequest
from app.services.admin import update_subscription

router = APIRouter(prefix="/users", tags=["admin-subscriptions"])


@router.patch(
    "/{user_id}/subscription",
    response_model=SubscriptionResponse,
    summary="Update user subscription",
    description="Manually update a user's subscription status and expiration date.",
)
async def update_user_subscription(
    user_id: UUID,
    body: UpdateSubscriptionRequest,
    admin: CurrentAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SubscriptionResponse:
    """Update a user's subscription status."""
    user = await update_subscription(db, user_id, body.status, body.expires_at)
    return SubscriptionResponse(
        user_id=user.id,
        subscription_status=user.subscription_status,
        subscription_expires_at=user.subscription_expires_at,
    )
