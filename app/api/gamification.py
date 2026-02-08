"""Gamification API endpoints for streaks, achievements, and user stats."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.schemas.gamification import (
    AchievementResponse,
    AchievementType,
    StreakResponse,
    UserStatsResponse,
)
from app.services.gamification import (
    StreakNotFoundError,
    get_achievement_by_id,
    get_achievements,
    get_or_create_streak,
    get_user_stats,
    share_achievement,
    use_freeze,
)

router = APIRouter(tags=["Gamification"])


@router.get(
    "/users/me/stats",
    response_model=UserStatsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get user gamification stats",
    responses={
        200: {"description": "User stats retrieved successfully"},
        401: {"description": "Not authenticated"},
    },
)
async def get_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> UserStatsResponse:
    """Get aggregated gamification statistics for the current user.

    Returns streak data, achievements list, total feedings count, and fish count.
    """
    stats = await get_user_stats(db, current_user.id)

    return UserStatsResponse(
        streak=StreakResponse(
            current_streak=stats.streak.current_streak,
            best_streak=stats.streak.best_streak,
            freeze_available=stats.streak.freeze_available,
            last_feed_date=stats.streak.last_feed_date,
        ),
        achievements=[
            AchievementResponse(
                id=a.id,
                achievement_type=AchievementType(a.achievement_type),
                unlocked_at=a.unlocked_at,
                shared_at=a.shared_at,
            )
            for a in stats.achievements
        ],
        total_feedings=stats.total_feedings,
        fish_count=stats.fish_count,
    )


@router.get(
    "/users/me/achievements",
    response_model=list[AchievementResponse],
    status_code=status.HTTP_200_OK,
    summary="Get user achievements",
    responses={
        200: {"description": "Achievements retrieved successfully"},
        401: {"description": "Not authenticated"},
    },
)
async def get_user_achievements(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> list[AchievementResponse]:
    """Get all unlocked achievements for the current user.

    Returns achievements sorted by unlocked_at in descending order (newest first).
    """
    achievements = await get_achievements(db, current_user.id)

    # Sort by unlocked_at descending
    sorted_achievements = sorted(
        achievements, key=lambda a: a.unlocked_at, reverse=True
    )

    return [
        AchievementResponse(
            id=a.id,
            achievement_type=AchievementType(a.achievement_type),
            unlocked_at=a.unlocked_at,
            shared_at=a.shared_at,
        )
        for a in sorted_achievements
    ]


@router.post(
    "/users/me/streak/freeze",
    response_model=StreakResponse,
    status_code=status.HTTP_200_OK,
    summary="Use streak freeze",
    responses={
        200: {"description": "Freeze used successfully"},
        400: {"description": "No freeze days available"},
        401: {"description": "Not authenticated"},
    },
)
async def use_streak_freeze(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> StreakResponse:
    """Use a freeze day to preserve the current streak.

    Decrements the available freeze count by 1. Returns 400 if no freeze days
    are available or if user has no streak to freeze.
    """
    try:
        success = await use_freeze(db, current_user.id)
    except StreakNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No freeze days available",
        ) from None

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No freeze days available",
        )

    streak = await get_or_create_streak(db, current_user.id)

    return StreakResponse(
        current_streak=streak.current_streak,
        best_streak=streak.best_streak,
        freeze_available=streak.freeze_available,
        last_feed_date=streak.last_feed_date,
    )


@router.post(
    "/achievements/{achievement_id}/share",
    response_model=AchievementResponse,
    status_code=status.HTTP_200_OK,
    summary="Share an achievement",
    responses={
        200: {"description": "Achievement shared successfully"},
        401: {"description": "Not authenticated"},
        403: {"description": "Achievement does not belong to user"},
        404: {"description": "Achievement not found"},
    },
)
async def share_user_achievement(
    achievement_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> AchievementResponse:
    """Mark an achievement as shared.

    Sets the shared_at timestamp to the current time. Returns 404 if the
    achievement does not exist, or 403 if it belongs to a different user.
    """
    # First check if achievement exists at all
    achievement = await get_achievement_by_id(db, achievement_id)

    if achievement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Achievement not found",
        )

    # Check if it belongs to the current user
    if achievement.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Achievement does not belong to user",
        )

    # Share the achievement
    shared = await share_achievement(db, current_user.id, achievement_id)

    if shared is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Achievement not found",
        )

    return AchievementResponse(
        id=shared.id,
        achievement_type=AchievementType(shared.achievement_type),
        unlocked_at=shared.unlocked_at,
        shared_at=shared.shared_at,
    )
