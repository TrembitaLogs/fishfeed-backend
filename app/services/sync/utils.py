"""Sync utility functions: token generation, conflict resolution, entity conversion."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.models.aquarium import Aquarium
from app.models.feeding import FeedingLog, FeedingSchedule
from app.models.fish import Fish
from app.models.gamification import Achievement, Streak, UserProgress
from app.models.user import User
from app.schemas.sync import ChangeItem, EntityType


def _generate_sync_token() -> str:
    """Generate a unique sync token.

    Uses combination of timestamp and UUID for uniqueness.

    Returns:
        Sync token string.
    """
    now = datetime.now(UTC)
    timestamp = now.strftime("%Y%m%d%H%M%S%f")
    unique_id = uuid4().hex[:8]
    return f"{timestamp}-{unique_id}"


def resolve_conflict(
    server_updated_at: datetime,
    client_updated_at: datetime,
) -> str:
    """Determine winner based on timestamp comparison (last-write-wins).

    Args:
        server_updated_at: Server entity's updated_at timestamp.
        client_updated_at: Client's updated_at timestamp.

    Returns:
        'client' if client timestamp is newer, 'server' otherwise.
        When timestamps are equal, server wins for determinism.
    """
    # Normalize both datetimes to UTC for comparison
    # Handle both timezone-aware and timezone-naive datetimes
    server_ts = server_updated_at
    client_ts = client_updated_at

    if server_ts.tzinfo is None:
        server_ts = server_ts.replace(tzinfo=UTC)
    if client_ts.tzinfo is None:
        client_ts = client_ts.replace(tzinfo=UTC)

    if client_ts > server_ts:
        return "client"
    return "server"


def _entity_to_dict(
    entity: Aquarium | Fish | FeedingLog | FeedingSchedule | Streak | Achievement | UserProgress | User,
) -> dict[str, Any]:
    """Convert entity to dictionary for conflict reporting.

    Args:
        entity: Database entity.

    Returns:
        Dictionary representation of entity.
    """
    result: dict[str, Any] = {}

    if isinstance(entity, Aquarium):
        result = {
            "id": str(entity.id),
            "owner_id": str(entity.owner_id),
            "name": entity.name,
            "photo_key": entity.photo_key,
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
            "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
            "deleted_at": entity.deleted_at.isoformat() if entity.deleted_at else None,
        }
    elif isinstance(entity, Fish):
        result = {
            "id": str(entity.id),
            "aquarium_id": str(entity.aquarium_id),
            "species_id": entity.species_id,
            "quantity": entity.quantity,
            "custom_name": entity.custom_name,
            "added_via": entity.added_via,
            "photo_key": entity.photo_key,
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
            "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
            "deleted_at": entity.deleted_at.isoformat() if entity.deleted_at else None,
        }
    elif isinstance(entity, FeedingLog):
        result = {
            "id": str(entity.id),
            "schedule_id": str(entity.schedule_id),
            "fish_id": str(entity.fish_id),
            "aquarium_id": str(entity.aquarium_id),
            "scheduled_for": entity.scheduled_for.isoformat() if entity.scheduled_for else None,
            "action": entity.action,
            "acted_at": entity.acted_at.isoformat() if entity.acted_at else None,
            "acted_by_user_id": str(entity.acted_by_user_id),
            "device_id": str(entity.device_id),
            "notes": entity.notes,
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
        }
    elif isinstance(entity, FeedingSchedule):
        result = {
            "id": str(entity.id),
            "aquarium_id": str(entity.aquarium_id),
            "fish_id": str(entity.fish_id),
            "time": entity.time.strftime("%H:%M") if entity.time else None,
            "interval_days": entity.interval_days,
            "anchor_date": entity.anchor_date.isoformat() if entity.anchor_date else None,
            "food_type": entity.food_type,
            "portion_hint": entity.portion_hint,
            "active": entity.active,
            "created_by_user_id": str(entity.created_by_user_id) if entity.created_by_user_id else None,
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
            "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
        }
    elif isinstance(entity, Streak):
        result = {
            "id": str(entity.user_id),  # user_id is the primary key for streaks
            "user_id": str(entity.user_id),
            "current_streak": entity.current_streak,
            "best_streak": entity.best_streak,
            "freeze_available": entity.freeze_available,
            "freeze_used_this_period": entity.freeze_used_this_period,
            "period_start": entity.period_start.isoformat() if entity.period_start else None,
            "last_feed_date": entity.last_feed_date.isoformat() if entity.last_feed_date else None,
            "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
        }
    elif isinstance(entity, Achievement):
        result = {
            "id": str(entity.id),
            "user_id": str(entity.user_id),
            "achievement_type": entity.achievement_type,
            "unlocked_at": entity.unlocked_at.isoformat() if entity.unlocked_at else None,
            "shared_at": entity.shared_at.isoformat() if entity.shared_at else None,
        }
    elif isinstance(entity, UserProgress):
        result = {
            "id": str(entity.user_id),  # user_id is the primary key for progress
            "user_id": str(entity.user_id),
            "total_xp": entity.total_xp,
            "level": entity.level,
            "last_xp_awarded_at": (entity.last_xp_awarded_at.isoformat() if entity.last_xp_awarded_at else None),
            "last_level_up_at": (entity.last_level_up_at.isoformat() if entity.last_level_up_at else None),
            "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
        }
    elif isinstance(entity, User):
        result = {
            "id": str(entity.id),
            "email": entity.email,
            "nickname": entity.nickname,
            "avatar_key": entity.avatar_key,
            "subscription_status": entity.subscription_status,
            "subscription_expires_at": (
                entity.subscription_expires_at.isoformat() if entity.subscription_expires_at else None
            ),
            "free_ai_scans_remaining": entity.free_ai_scans_remaining,
            "settings": entity.settings,
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
            "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
        }

    return result


def _group_changes_by_entity_type(
    changes: list[ChangeItem],
) -> dict[EntityType, list[ChangeItem]]:
    """Group changes by entity type for batch processing.

    Args:
        changes: List of change items.

    Returns:
        Dictionary mapping entity types to their changes.
    """
    grouped: dict[EntityType, list[ChangeItem]] = {
        "aquarium": [],
        "fish": [],
        "feeding_log": [],
        "schedule": [],
        "streak": [],
        "achievement": [],
        "progress": [],
        "user_profile": [],
    }
    for change in changes:
        grouped[change.entity_type].append(change)
    return grouped
