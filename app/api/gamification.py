"""Gamification API endpoints for streaks, achievements, and user stats."""

import json
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.redis import get_redis
from app.schemas.gamification import (
    AchievementResponse,
    AchievementType,
    StreakResponse,
    UserStatsResponse,
)
from app.services.gamification import (
    get_achievement_by_id,
    get_achievements,
    get_user_stats,
    share_achievement,
)
from app.utils.cache import (
    TTL_USER_ACHIEVEMENTS,
    TTL_USER_STATS,
    invalidate_user_gamification_keys,
    user_achievements_key,
    user_stats_key,
)

logger = structlog.get_logger(__name__)

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
    redis: Annotated[Redis, Depends(get_redis)],
) -> UserStatsResponse:
    """Get aggregated gamification statistics for the current user.

    Returns streak data, achievements list, total feedings count, and fish count.
    Results are cached for 60 seconds in Redis.
    """
    cache_key = user_stats_key(str(current_user.id))
    try:
        cached = await redis.get(cache_key)
        if cached:
            logger.debug("Cache hit", cache_key=cache_key)
            return UserStatsResponse.model_validate_json(cached)
    except RedisError as e:
        logger.warning("Redis error on get", error=str(e))

    stats = await get_user_stats(db, current_user.id)

    response = UserStatsResponse(
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

    try:
        await redis.set(cache_key, response.model_dump_json(), ex=TTL_USER_STATS)
    except RedisError as e:
        logger.warning("Redis error on set", error=str(e))

    return response


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
    redis: Annotated[Redis, Depends(get_redis)],
) -> list[AchievementResponse]:
    """Get all unlocked achievements for the current user.

    Returns achievements sorted by unlocked_at in descending order (newest first).
    Results are cached for 2 minutes in Redis.
    """
    cache_key = user_achievements_key(str(current_user.id))
    try:
        cached = await redis.get(cache_key)
        if cached:
            logger.debug("Cache hit", cache_key=cache_key)
            return [AchievementResponse.model_validate(item) for item in json.loads(cached)]
    except RedisError as e:
        logger.warning("Redis error on get", error=str(e))

    achievements = await get_achievements(db, current_user.id)

    # Sort by unlocked_at descending
    sorted_achievements = sorted(
        achievements, key=lambda a: a.unlocked_at, reverse=True
    )

    response = [
        AchievementResponse(
            id=a.id,
            achievement_type=AchievementType(a.achievement_type),
            unlocked_at=a.unlocked_at,
            shared_at=a.shared_at,
        )
        for a in sorted_achievements
    ]

    try:
        cache_data = json.dumps([r.model_dump(mode="json") for r in response])
        await redis.set(cache_key, cache_data, ex=TTL_USER_ACHIEVEMENTS)
    except RedisError as e:
        logger.warning("Redis error on set", error=str(e))

    return response


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
    redis: Annotated[Redis, Depends(get_redis)],
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

    # Invalidate gamification caches
    try:
        keys = invalidate_user_gamification_keys(str(current_user.id))
        await redis.delete(*keys)
    except RedisError as e:
        logger.warning("Redis error on invalidation", error=str(e))

    return AchievementResponse(
        id=shared.id,
        achievement_type=AchievementType(shared.achievement_type),
        unlocked_at=shared.unlocked_at,
        shared_at=shared.shared_at,
    )
