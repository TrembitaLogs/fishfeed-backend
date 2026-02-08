"""Push notification API endpoints for token management and preferences."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.models.notification import NotificationPreference, PushToken
from app.schemas.notification import (
    NotificationPreferencesResponse,
    NotificationPreferencesUpdate,
    PushTokenRequest,
    PushTokenResponse,
)

router = APIRouter(tags=["Push Notifications"])


@router.post(
    "/push/token",
    response_model=PushTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register push token",
    responses={
        201: {"description": "Token registered successfully"},
        401: {"description": "Not authenticated"},
        422: {"description": "Validation error"},
    },
)
async def register_push_token(
    data: PushTokenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> PushTokenResponse:
    """Register a device push notification token.

    If the token already exists for this user, updates the record.
    """
    stmt = (
        insert(PushToken)
        .values(
            user_id=current_user.id,
            token=data.token,
            platform=data.platform,
        )
        .on_conflict_do_update(
            constraint="uq_user_push_token",
            set_={
                "platform": data.platform,
                "updated_at": func.now(),
            },
        )
        .returning(PushToken)
    )

    result = await db.execute(stmt)
    push_token = result.scalar_one()
    await db.flush()
    await db.refresh(push_token)

    return PushTokenResponse.model_validate(push_token)


@router.delete(
    "/push/token",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unregister push token",
    responses={
        204: {"description": "Token removed successfully"},
        401: {"description": "Not authenticated"},
        404: {"description": "Token not found"},
    },
)
async def unregister_push_token(
    data: PushTokenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> None:
    """Remove a device push notification token.

    Typically called during logout or when user disables notifications.
    """
    stmt = delete(PushToken).where(
        PushToken.user_id == current_user.id,
        PushToken.token == data.token,
    )

    result = await db.execute(stmt)
    await db.flush()

    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Push token not found",
        )


@router.get(
    "/users/me/notifications",
    response_model=NotificationPreferencesResponse,
    summary="Get notification preferences",
    responses={
        200: {"description": "Current notification preferences"},
        401: {"description": "Not authenticated"},
    },
)
async def get_notification_preferences(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> NotificationPreferencesResponse:
    """Get current user's notification preferences.

    Returns default values if no preferences have been set.
    """
    stmt = select(NotificationPreference).where(
        NotificationPreference.user_id == current_user.id
    )
    result = await db.execute(stmt)
    preferences = result.scalar_one_or_none()

    if preferences is None:
        # Return default preferences
        return NotificationPreferencesResponse(
            global_opt_out=False,
            timezone=None,
            feeding_reminders=True,
            overdue_alerts=True,
            streak_protection=True,
            weekly_summary=True,
            family_updates=True,
            marketing=False,
            updated_at=None,
        )

    return NotificationPreferencesResponse.model_validate(preferences)


@router.put(
    "/users/me/notifications",
    response_model=NotificationPreferencesResponse,
    summary="Update notification preferences",
    responses={
        200: {"description": "Preferences updated successfully"},
        401: {"description": "Not authenticated"},
        422: {"description": "Validation error"},
    },
)
async def update_notification_preferences(
    data: NotificationPreferencesUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> NotificationPreferencesResponse:
    """Update user's notification preferences.

    Only provided fields will be updated. Missing fields keep their current values.
    Creates preferences record if it doesn't exist.
    """
    # Get current preferences or create new
    stmt = select(NotificationPreference).where(
        NotificationPreference.user_id == current_user.id
    )
    result = await db.execute(stmt)
    preferences = result.scalar_one_or_none()

    if preferences is None:
        # Create new preferences with defaults + provided values
        preferences = NotificationPreference(
            user_id=current_user.id,
            feeding_reminders=data.feeding_reminders
            if data.feeding_reminders is not None
            else True,
            overdue_alerts=data.overdue_alerts
            if data.overdue_alerts is not None
            else True,
            streak_protection=data.streak_protection
            if data.streak_protection is not None
            else True,
            weekly_summary=data.weekly_summary
            if data.weekly_summary is not None
            else True,
            family_updates=data.family_updates
            if data.family_updates is not None
            else True,
            marketing=data.marketing if data.marketing is not None else False,
        )
        db.add(preferences)
    else:
        # Update existing preferences with provided values
        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if value is not None:
                setattr(preferences, field, value)

    await db.flush()
    await db.refresh(preferences)

    return NotificationPreferencesResponse.model_validate(preferences)
