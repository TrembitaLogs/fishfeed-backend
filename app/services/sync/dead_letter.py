"""Dead-letter handling for failed sync changes."""

from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sync_dead_letter import SyncDeadLetter
from app.schemas.sync import ChangeItem

logger = structlog.get_logger(__name__)


async def record_dead_letter(
    db: AsyncSession,
    user_id: UUID,
    change: ChangeItem,
    error: Exception,
) -> None:
    """Record a failed sync change to the dead-letter table.

    Args:
        db: Database session.
        user_id: User who submitted the change.
        change: The change item that failed.
        error: The exception that occurred.
    """
    try:
        entry = SyncDeadLetter(
            user_id=user_id,
            entity_type=change.entity_type,
            entity_id=change.entity_id,
            operation=change.operation,
            payload=change.data if change.data else {},
            error_message=str(error),
            error_type=type(error).__name__,
        )
        db.add(entry)
        await db.flush()
        logger.warning(
            "Recorded sync dead letter",
            user_id=str(user_id),
            entity_type=change.entity_type,
            entity_id=str(change.entity_id),
            operation=change.operation,
            error_type=type(error).__name__,
        )
    except Exception as e:
        logger.error(
            "Failed to record sync dead letter",
            user_id=str(user_id),
            entity_type=change.entity_type,
            entity_id=str(change.entity_id),
            error=str(e),
        )
