"""User API endpoints for profile and GDPR compliance."""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.schemas.analytics import DataExportResponse
from app.schemas.gamification import StreakResponse
from app.schemas.user import UserProfileResponse, UserProfileUpdateRequest
from app.services.analytics import delete_user_data, export_user_data
from app.services.gamification import get_achievements, get_or_create_streak

router = APIRouter(prefix="/users", tags=["Users"])


@router.get(
    "/me",
    response_model=UserProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current user profile",
    responses={
        200: {"description": "User profile retrieved successfully"},
        401: {"description": "Not authenticated"},
    },
)
async def get_current_user_profile(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> UserProfileResponse:
    """Get the profile of the currently authenticated user.

    Returns user profile data including streak information and achievement count.
    """
    streak = await get_or_create_streak(db, current_user.id)
    achievements = await get_achievements(db, current_user.id)

    streak_response = StreakResponse.model_validate(streak)

    return UserProfileResponse(
        id=current_user.id,
        email=current_user.email,
        display_name=current_user.nickname,
        avatar_key=current_user.avatar_key,
        created_at=current_user.created_at,
        subscription_status=current_user.subscription_status,
        subscription_expires_at=current_user.subscription_expires_at,
        streak=streak_response,
        achievements_count=len(achievements),
    )


@router.put(
    "/me",
    response_model=UserProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Update current user profile",
    responses={
        200: {"description": "User profile updated successfully"},
        401: {"description": "Not authenticated"},
        422: {"description": "Validation error"},
    },
)
async def update_current_user_profile(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
    request: UserProfileUpdateRequest,
) -> UserProfileResponse:
    """Update the profile of the currently authenticated user.

    Only provided fields will be updated. Empty request body results in no changes.
    """
    update_data = request.model_dump(exclude_unset=True)

    if "display_name" in update_data:
        current_user.nickname = update_data["display_name"]

    if "avatar_key" in update_data:
        current_user.avatar_key = update_data["avatar_key"]

    if update_data:
        await db.flush()
        await db.refresh(current_user)

    streak = await get_or_create_streak(db, current_user.id)
    achievements = await get_achievements(db, current_user.id)

    streak_response = StreakResponse.model_validate(streak)

    return UserProfileResponse(
        id=current_user.id,
        email=current_user.email,
        display_name=current_user.nickname,
        avatar_key=current_user.avatar_key,
        created_at=current_user.created_at,
        subscription_status=current_user.subscription_status,
        subscription_expires_at=current_user.subscription_expires_at,
        streak=streak_response,
        achievements_count=len(achievements),
    )


@router.get(
    "/me/data-export",
    response_model=DataExportResponse,
    status_code=status.HTTP_200_OK,
    summary="Export user data (GDPR)",
    responses={
        200: {"description": "Data export URL generated successfully"},
        401: {"description": "Not authenticated"},
        503: {"description": "Storage service not configured"},
    },
)
async def get_data_export(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> DataExportResponse:
    """Export all user data as JSON for GDPR compliance.

    Collects all data associated with the authenticated user from all tables,
    generates a JSON file, uploads it to S3, and returns a presigned download URL.

    The download URL is valid for 24 hours.
    """
    return await export_user_data(db, current_user.id)


@router.delete(
    "/me/data",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete all user data (GDPR)",
    responses={
        204: {"description": "All user data deleted successfully"},
        401: {"description": "Not authenticated"},
        404: {"description": "User not found"},
    },
)
async def delete_all_data(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> None:
    """Permanently delete all user data for GDPR compliance.

    This is a hard delete operation that removes all data associated
    with the authenticated user from all database tables. This action
    is irreversible.

    After deletion, the user will be logged out and unable to access
    the service with their current credentials.
    """
    await delete_user_data(db, current_user.id)
