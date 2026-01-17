"""Subscription background jobs for expired subscription handling.

This module provides scheduled jobs for:
- Checking and processing expired premium subscriptions
- Applying free tier limits to downgraded users
- Sending push notifications about subscription expiry
"""

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_maker
from app.models.aquarium import Aquarium, AquariumMember
from app.models.fish import Fish
from app.models.user import User
from app.schemas.purchase import FREE_USER_LIMITS
from app.services.notification import NotificationService
from app.services.purchase import revert_to_free

logger = logging.getLogger(__name__)
settings = get_settings()


async def check_expired_subscriptions_job() -> int:
    """Check and process expired premium subscriptions.

    Finds all users with premium status and expired subscription_expires_at,
    processes them in batches, and reverts them to free tier.

    Returns:
        Number of users processed.
    """
    logger.info("Starting check_expired_subscriptions_job")

    batch_size = settings.SUBSCRIPTION_BATCH_SIZE
    total_processed = 0
    now = datetime.now(UTC)

    async with async_session_maker() as db:
        while True:
            # Query expired premium users in batches
            stmt = (
                select(User)
                .where(User.subscription_status == "premium")
                .where(User.subscription_expires_at < now)
                .where(User.deleted_at.is_(None))
                .limit(batch_size)
            )
            result = await db.execute(stmt)
            expired_users = list(result.scalars().all())

            if not expired_users:
                break

            for user in expired_users:
                try:
                    await _process_expired_user(db, user)
                    total_processed += 1
                except Exception as e:
                    logger.error(
                        f"Failed to process expired subscription for user {user.id}: {e}"
                    )
                    continue

            logger.info(f"Processed batch of {len(expired_users)} expired subscriptions")

    logger.info(f"check_expired_subscriptions_job completed: {total_processed} users processed")
    return total_processed


async def _process_expired_user(db: AsyncSession, user: User) -> None:
    """Process a single expired user subscription.

    Args:
        db: Database session.
        user: User with expired subscription.
    """
    user_id = user.id
    logger.info(f"Processing expired subscription for user {user_id}")

    # Revert to free tier
    await revert_to_free(db, user_id)

    # Apply free tier limits and record excess items
    await apply_free_tier_limits(db, user_id)

    # Send push notification about subscription expiry
    await _send_subscription_expired_notification(db, user_id)

    logger.info(f"User {user_id} reverted to free tier after subscription expiry")


async def apply_free_tier_limits(db: AsyncSession, user_id: UUID) -> dict:
    """Apply free tier limits to a user after subscription downgrade.

    This function enforces limits gracefully:
    - Does NOT delete any existing data
    - Records timestamps for exceeded limits in user settings
    - Resets free AI scan counter

    Args:
        db: Database session.
        user_id: User UUID.

    Returns:
        Dict with limit status information.
    """
    logger.info(f"Applying free tier limits for user {user_id}")

    stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        logger.warning(f"User {user_id} not found when applying free tier limits")
        return {"error": "user_not_found"}

    limits_exceeded = {}
    now = datetime.now(UTC)

    # Reset AI scans to free tier limit
    user.free_ai_scans_remaining = FREE_USER_LIMITS.ai_scans_per_month

    # Check aquarium count
    aquarium_count = await _get_user_aquarium_count(db, user_id)
    if aquarium_count > FREE_USER_LIMITS.max_aquariums:
        limits_exceeded["aquariums"] = {
            "current": aquarium_count,
            "limit": FREE_USER_LIMITS.max_aquariums,
            "exceeded_at": now.isoformat(),
        }
        logger.info(
            f"User {user_id} has {aquarium_count} aquariums, "
            f"exceeds free limit of {FREE_USER_LIMITS.max_aquariums}"
        )

    # Check fish per aquarium
    aquariums_with_excess_fish = await _get_aquariums_with_excess_fish(
        db, user_id, FREE_USER_LIMITS.max_fish_per_aquarium
    )
    if aquariums_with_excess_fish:
        limits_exceeded["fish_per_aquarium"] = {
            "aquariums": aquariums_with_excess_fish,
            "limit": FREE_USER_LIMITS.max_fish_per_aquarium,
            "exceeded_at": now.isoformat(),
        }
        logger.info(
            f"User {user_id} has aquariums with excess fish: {aquariums_with_excess_fish}"
        )

    # Check family members per aquarium (free tier typically allows fewer members)
    # For now, we don't enforce strict limits on family members, just record the info
    family_info = await _get_family_member_info(db, user_id)
    if family_info:
        limits_exceeded["family_members"] = {
            "aquariums": family_info,
            "recorded_at": now.isoformat(),
        }

    # Update user settings with limits exceeded info
    if limits_exceeded:
        settings_dict = dict(user.settings)
        settings_dict["limits_exceeded"] = limits_exceeded
        settings_dict["downgraded_at"] = now.isoformat()
        user.settings = settings_dict
        logger.info(f"User {user_id} limits exceeded: {list(limits_exceeded.keys())}")

    await db.commit()

    return {
        "user_id": str(user_id),
        "ai_scans_reset": FREE_USER_LIMITS.ai_scans_per_month,
        "limits_exceeded": limits_exceeded,
    }


async def _get_user_aquarium_count(db: AsyncSession, user_id: UUID) -> int:
    """Get the total number of aquariums owned by a user.

    Args:
        db: Database session.
        user_id: User UUID.

    Returns:
        Number of aquariums.
    """
    stmt = (
        select(func.count())
        .select_from(Aquarium)
        .where(Aquarium.owner_id == user_id)
        .where(Aquarium.deleted_at.is_(None))
    )
    result = await db.execute(stmt)
    return result.scalar_one()


async def _get_aquariums_with_excess_fish(
    db: AsyncSession,
    user_id: UUID,
    max_fish: int,
) -> list[dict]:
    """Get aquariums that have more fish than the free tier limit.

    Args:
        db: Database session.
        user_id: User UUID.
        max_fish: Maximum fish allowed per aquarium.

    Returns:
        List of dicts with aquarium_id and fish_count for exceeding aquariums.
    """
    # Get aquariums owned by user with fish counts
    stmt = (
        select(Aquarium.id, func.count(Fish.id).label("fish_count"))
        .outerjoin(Fish, (Fish.aquarium_id == Aquarium.id) & (Fish.deleted_at.is_(None)))
        .where(Aquarium.owner_id == user_id)
        .where(Aquarium.deleted_at.is_(None))
        .group_by(Aquarium.id)
        .having(func.count(Fish.id) > max_fish)
    )
    result = await db.execute(stmt)
    rows = result.all()

    return [
        {"aquarium_id": str(row.id), "fish_count": row.fish_count}
        for row in rows
    ]


async def _get_family_member_info(
    db: AsyncSession,
    user_id: UUID,
) -> list[dict]:
    """Get family member counts for user's aquariums.

    Args:
        db: Database session.
        user_id: User UUID.

    Returns:
        List of dicts with aquarium_id and member_count.
    """
    # Get aquariums owned by user with member counts
    stmt = (
        select(Aquarium.id, func.count(AquariumMember.user_id).label("member_count"))
        .outerjoin(AquariumMember, AquariumMember.aquarium_id == Aquarium.id)
        .where(Aquarium.owner_id == user_id)
        .where(Aquarium.deleted_at.is_(None))
        .group_by(Aquarium.id)
        .having(func.count(AquariumMember.user_id) > 1)  # More than just owner
    )
    result = await db.execute(stmt)
    rows = result.all()

    return [
        {"aquarium_id": str(row.id), "member_count": row.member_count}
        for row in rows
    ]


async def _send_subscription_expired_notification(
    db: AsyncSession,
    user_id: UUID,
) -> bool:
    """Send push notification about subscription expiry.

    Args:
        db: Database session.
        user_id: User UUID.

    Returns:
        True if notification was sent successfully.
    """
    try:
        notification_service = NotificationService(db)

        success = await notification_service.send_push(
            user_id=user_id,
            title="Premium subscription expired",
            body="Your premium subscription has ended. Upgrade to continue enjoying unlimited features!",
            data={
                "type": "subscription_expired",
                "action": "open_subscription_page",
            },
            bypass_throttle=True,  # System notification, bypass throttle
        )

        if success:
            logger.info(f"Subscription expiry notification sent to user {user_id}")
        else:
            logger.info(f"Failed to send subscription expiry notification to user {user_id}")

        return success

    except Exception as e:
        logger.error(f"Error sending subscription expiry notification to {user_id}: {e}")
        return False


async def clear_limits_exceeded(db: AsyncSession, user_id: UUID) -> None:
    """Clear the limits_exceeded info from user settings.

    Should be called when user upgrades back to premium.

    Args:
        db: Database session.
        user_id: User UUID.
    """
    stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        return

    settings_dict = dict(user.settings)
    settings_dict.pop("limits_exceeded", None)
    settings_dict.pop("downgraded_at", None)
    user.settings = settings_dict

    await db.commit()
    logger.info(f"Cleared limits_exceeded for user {user_id}")
