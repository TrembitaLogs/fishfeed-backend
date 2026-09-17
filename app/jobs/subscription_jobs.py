"""Subscription background jobs for expired subscription handling.

This module provides scheduled jobs for:
- Checking and processing expired premium subscriptions
- Applying free tier limits to downgraded users
- Sending push notifications about subscription expiry
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_maker
from app.models.aquarium import Aquarium, AquariumMember
from app.models.fish import Fish
from app.models.user import User
from app.schemas.purchase import FREE_USER_LIMITS
from app.services.notification import NotificationService
from app.services.premium import invalidate_premium_cache
from app.services.purchase import (
    PurchaseError,
    RevenueCatAPIError,
    RevenueCatNotConfiguredError,
    UserNotFoundError,
    read_reconciliation,
    reconcile_user,
)

logger = structlog.get_logger(__name__)
settings = get_settings()


def _is_fatal_reconciliation_error(error: Exception, *, dry_run: bool) -> bool:
    """Whether one provider failure makes the whole fixed pass unsafe to continue."""
    return isinstance(error, RevenueCatNotConfiguredError) or (
        isinstance(error, RevenueCatAPIError)
        and (
            error.failure_kind == "configuration"
            or error.upstream_status in {401, 403, 429}
            or (dry_run and error.upstream_status == 201)
        )
    )


async def _due_subscription_user_ids(now: datetime, user_ids: tuple[UUID, ...]) -> list[UUID]:
    """Capture one deterministic recovery snapshot before any account I/O."""
    if user_ids:
        return list(dict.fromkeys(user_ids))
    due = or_(
        User.subscription_verified_at.is_(None),
        User.subscription_verified_at < now - timedelta(days=1),
        and_(
            User.subscription_status == "premium",
            User.subscription_expires_at.is_not(None),
            User.subscription_expires_at <= now + timedelta(hours=24),
            User.subscription_verified_at < now - timedelta(hours=1),
        ),
    )
    async with async_session_maker() as db:
        result = await db.execute(
            select(User.id)
            .where(User.deleted_at.is_(None), due)
            .order_by(User.subscription_verified_at.nullsfirst(), User.id)
        )
        return list(result.scalars())


async def check_expired_subscriptions_job(*, dry_run: bool = False, user_ids: tuple[UUID, ...] = ()) -> int:
    """Reconcile one fixed, cadence-selected set of RevenueCat accounts."""
    from app.redis import get_redis_client

    now = datetime.now(UTC)
    ids = await _due_subscription_user_ids(now, user_ids)
    redis: Redis = get_redis_client()
    counts = {"changed": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    fatal_error: Exception | None = None

    for offset in range(0, len(ids), settings.SUBSCRIPTION_BATCH_SIZE):
        for user_id in ids[offset : offset + settings.SUBSCRIPTION_BATCH_SIZE]:
            try:
                async with async_session_maker() as db:
                    proposal = (
                        await read_reconciliation(db, redis, user_id, dry_run=True)
                        if dry_run
                        else await reconcile_user(db, redis, user_id)
                    )
                    if dry_run:
                        counts[proposal.outcome] += 1
                        logger.info(
                            "Subscription reconciliation dry-run result",
                            user_id=user_id,
                            before_status=proposal.before_status,
                            before_expires_at=proposal.before_expires_at,
                            proposed_status=proposal.snapshot.status,
                            proposed_expires_at=proposal.snapshot.expires_at,
                            outcome=proposal.outcome,
                        )
                        continue

                    await db.commit()
                    counts[proposal.outcome] += 1
                    await invalidate_premium_cache(str(user_id), redis)
                    if proposal.before_status == "premium" and proposal.snapshot.status == "free":
                        try:
                            await _send_subscription_expired_notification(db, user_id)
                            await db.commit()
                        except Exception:
                            await db.rollback()
                            logger.exception("Failed to persist subscription expiry notification", user_id=user_id)
            except UserNotFoundError:
                counts["skipped"] += 1
                logger.info("Subscription reconciliation skipped unknown or deleted user", user_id=user_id)
            except Exception as error:
                counts["errors"] += 1
                logger.error("Subscription reconciliation failed", user_id=user_id, error=str(error))
                if _is_fatal_reconciliation_error(error, dry_run=dry_run):
                    if dry_run and isinstance(error, RevenueCatAPIError) and error.upstream_status == 201:
                        logger.error("Dry-run stopped: upstream customer may have been created", user_id=user_id)
                    fatal_error = error
                    break
        if fatal_error is not None:
            break

    logger.info("Subscription reconciliation completed", dry_run=dry_run, total=len(ids), **counts)
    if fatal_error is not None:
        raise fatal_error
    if counts["errors"]:
        raise PurchaseError("Subscription reconciliation incomplete", status_code=503)
    return counts["changed"] + counts["unchanged"]


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
    logger.info("Applying free tier limits for user", user_id=user_id)

    stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        logger.warning("User not found when applying free tier limits", user_id=user_id)
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
            "User has aquariums exceeding free limit",
            user_id=user_id,
            aquarium_count=aquarium_count,
            free_limit=FREE_USER_LIMITS.max_aquariums,
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
            "User has aquariums with excess fish",
            user_id=user_id,
            aquariums_with_excess_fish=aquariums_with_excess_fish,
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
        logger.info("User limits exceeded", user_id=user_id, exceeded_limits=list(limits_exceeded.keys()))

    await db.flush()

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

    return [{"aquarium_id": str(row.id), "fish_count": row.fish_count} for row in rows]


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

    return [{"aquarium_id": str(row.id), "member_count": row.member_count} for row in rows]


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
            logger.info("Subscription expiry notification sent to user", user_id=user_id)
        else:
            logger.info("Failed to send subscription expiry notification to user", user_id=user_id)

        return success

    except Exception as e:
        logger.error("Error sending subscription expiry notification", user_id=user_id, error=str(e))
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
    logger.info("Cleared limits_exceeded for user", user_id=user_id)
