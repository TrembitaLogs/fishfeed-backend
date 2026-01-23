"""Mobile sync service for processing mobile app sync requests."""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feeding import FeedingEvent
from app.schemas.sync import (
    MobileFeedingEvent,
    MobileSyncRequest,
    MobileSyncResponse,
)
from app.services.sync import (
    SyncAccessDeniedError,
    SyncValidationError,
    _get_user_aquarium_ids,
    resolve_conflict,
)

logger = logging.getLogger(__name__)


def _feeding_event_to_mobile_dict(event: FeedingEvent) -> dict[str, Any]:
    """Convert FeedingEvent to mobile app format dictionary.

    Args:
        event: FeedingEvent database entity.

    Returns:
        Dictionary with fields expected by mobile app.
    """
    return {
        "id": str(event.id),
        "aquarium_id": str(event.aquarium_id),
        "fish_id": str(event.fish_id) if event.fish_id else None,
        "species_id": event.species_id,
        "feeding_time": event.scheduled_at.isoformat() if event.scheduled_at else None,
        "status": event.status,
        "completed_at": (
            event.completed_at.isoformat() if event.completed_at else None
        ),
        "completed_by": str(event.completed_by) if event.completed_by else None,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "updated_at": event.updated_at.isoformat() if event.updated_at else None,
        "schedule_id": str(event.schedule_id) if event.schedule_id else None,
    }


def _map_mobile_event_to_feeding_event_data(
    event: MobileFeedingEvent,
    user_id: UUID,
) -> dict[str, Any]:
    """Map MobileFeedingEvent fields to FeedingEvent data dictionary.

    Args:
        event: Mobile feeding event from request.
        user_id: User ID performing the sync.

    Returns:
        Dictionary with FeedingEvent field values.
    """
    data: dict[str, Any] = {
        "scheduled_at": event.feeding_time,
        "status": "completed" if event.completed_by else "pending",
    }

    if event.aquarium_id:
        data["aquarium_id"] = UUID(event.aquarium_id)

    if event.completed_by:
        data["completed_by"] = UUID(event.completed_by)
        # Set completed_at to feeding_time when completed_by is present
        data["completed_at"] = event.feeding_time
        data["status"] = "completed"

    # Handle fish_id - try as UUID first, otherwise store as species_id
    if event.fish_id:
        try:
            data["fish_id"] = UUID(event.fish_id)
        except (ValueError, TypeError):
            # Not a valid UUID, store as species identifier string
            data["species_id"] = event.fish_id

    return data


async def _get_server_events_since(
    db: AsyncSession,
    user_aquarium_ids: set[UUID],
    since: datetime,
) -> list[dict[str, Any]]:
    """Get all feeding events updated after the given timestamp.

    Args:
        db: Database session.
        user_aquarium_ids: Set of aquarium IDs accessible to user.
        since: Timestamp to filter events (return events updated after this).

    Returns:
        List of feeding events in mobile format.
    """
    if not user_aquarium_ids:
        return []

    stmt = select(FeedingEvent).where(
        FeedingEvent.aquarium_id.in_(user_aquarium_ids),
        FeedingEvent.updated_at > since,
    )

    result = await db.execute(stmt)
    events = result.scalars().all()

    return [_feeding_event_to_mobile_dict(event) for event in events]


async def process_mobile_sync(
    db: AsyncSession,
    user_id: UUID,
    request: MobileSyncRequest,
) -> MobileSyncResponse:
    """Process mobile sync request.

    Implements the full mobile sync logic:
    1. Validates ownership of aquariums
    2. For each event: create or update with last-write-wins
    3. Returns synced_ids and server_events since client_timestamp

    Args:
        db: Database session.
        user_id: User ID performing the sync.
        request: Mobile sync request with events and client_timestamp.

    Returns:
        MobileSyncResponse with synced_ids and server_events.

    Raises:
        SyncAccessDeniedError: If user doesn't have access to an aquarium.
        SyncValidationError: If validation fails (e.g., missing aquarium_id).
    """
    logger.info(
        f"Processing mobile sync for user {user_id}: "
        f"{len(request.events)} events, client_timestamp={request.client_timestamp}"
    )

    # Get user's accessible aquariums
    user_aquarium_ids = await _get_user_aquarium_ids(db, user_id)

    synced_ids: list[str] = []

    # Process each event
    for event in request.events:
        try:
            event_id = UUID(event.id)
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid event ID format: {event.id}, error: {e}")
            raise SyncValidationError(f"Invalid event ID format: {event.id}") from None

        # Validate aquarium ownership for new events
        aquarium_uuid: UUID | None = None
        if event.aquarium_id:
            try:
                aquarium_uuid = UUID(event.aquarium_id)
            except (ValueError, TypeError):
                # Skip events with non-UUID aquarium_id (e.g., "default")
                logger.warning(
                    f"Skipping event {event.id}: invalid aquarium_id '{event.aquarium_id}'"
                )
                # Still mark as synced so client doesn't retry
                synced_ids.append(event.id)
                continue

            if aquarium_uuid not in user_aquarium_ids:
                raise SyncAccessDeniedError("aquarium", aquarium_uuid)

        # Check if event exists
        stmt = select(FeedingEvent).where(FeedingEvent.id == event_id)
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is not None:
            # Validate ownership of existing event
            if existing.aquarium_id not in user_aquarium_ids:
                raise SyncAccessDeniedError("event", event_id)

            # Apply last-write-wins
            client_updated_at = event.updated_at or event.created_at
            winner = resolve_conflict(existing.updated_at, client_updated_at)

            if winner == "client":
                # Client wins - update existing event
                event_data = _map_mobile_event_to_feeding_event_data(event, user_id)

                existing.scheduled_at = event_data["scheduled_at"]
                existing.status = event_data["status"]

                if "completed_by" in event_data:
                    existing.completed_by = event_data["completed_by"]
                if "completed_at" in event_data:
                    existing.completed_at = event_data["completed_at"]
                if "fish_id" in event_data:
                    existing.fish_id = event_data["fish_id"]
                if "species_id" in event_data:
                    existing.species_id = event_data["species_id"]

                synced_ids.append(event.id)
                logger.debug(
                    f"Updated existing event {event.id}: client wins "
                    f"(client={client_updated_at}, server={existing.updated_at})"
                )
            else:
                # Server wins - don't add to synced_ids
                logger.debug(
                    f"Kept server version for event {event.id}: server wins "
                    f"(client={client_updated_at}, server={existing.updated_at})"
                )
        else:
            # Create new event
            if not event.aquarium_id:
                raise SyncValidationError(
                    f"Missing aquarium_id for new event: {event.id}"
                )

            event_data = _map_mobile_event_to_feeding_event_data(event, user_id)

            new_event = FeedingEvent(
                id=event_id,
                aquarium_id=event_data["aquarium_id"],
                scheduled_at=event_data["scheduled_at"],
                status=event_data["status"],
                completed_at=event_data.get("completed_at"),
                completed_by=event_data.get("completed_by"),
                fish_id=event_data.get("fish_id"),
                species_id=event_data.get("species_id"),
                client_created_at=event.created_at,
            )
            db.add(new_event)
            synced_ids.append(event.id)
            logger.debug(f"Created new event {event.id}")

    # Flush to ensure all changes are persisted and updated_at is set
    await db.flush()

    # Get server events updated after client_timestamp
    server_events = await _get_server_events_since(
        db, user_aquarium_ids, request.client_timestamp
    )

    # Commit the transaction
    await db.commit()

    logger.info(
        f"Mobile sync completed for user {user_id}: "
        f"{len(synced_ids)} synced, {len(server_events)} server events"
    )

    return MobileSyncResponse(
        synced_ids=synced_ids,
        server_events=server_events,
    )
