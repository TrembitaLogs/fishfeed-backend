"""Sync API endpoint for offline-first data synchronization."""

import time
from typing import Annotated, Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.schemas.sync import (
    MobileSyncRequest,
    MobileSyncResponse,
    SyncRequest,
    SyncResponse,
)
from app.services.sync import (
    SyncAccessDeniedError,
    SyncError,
    SyncValidationError,
    process_sync,
)

router = APIRouter(prefix="/sync", tags=["Sync"])

logger = structlog.get_logger(__name__)


def _generate_correlation_id() -> str:
    """Generate unique correlation ID for request tracing."""
    return uuid4().hex[:16]


def _is_mobile_request(body: dict[str, Any]) -> bool:
    """Check if the request body is in mobile app format."""
    return "events" in body and "client_timestamp" in body


async def _handle_mobile_sync(
    body: dict[str, Any],
    db: AsyncSession,
    current_user: Any,
    correlation_id: str,
) -> MobileSyncResponse:
    """Handle sync request from mobile app.

    Mobile app sends: { events: [...], client_timestamp: "..." }
    Mobile app expects: { synced_ids: [...], server_events: [...] }
    """
    log = logger.bind(
        correlation_id=correlation_id,
        user_id=str(current_user.id),
        sync_type="mobile",
    )

    try:
        mobile_request = MobileSyncRequest.model_validate(body)
    except ValidationError as e:
        log.warning("mobile_sync_validation_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid mobile sync request: {e}",
        ) from None

    events = mobile_request.events
    log.info(
        "mobile_sync_request_received",
        events_count=len(events),
        client_timestamp=mobile_request.client_timestamp.isoformat(),
    )

    if not events:
        # No events to sync, return empty success response
        log.info("mobile_sync_no_events")
        return MobileSyncResponse(synced_ids=[], server_events=[])

    # For now, accept all events as synced
    # In a full implementation, we would:
    # 1. Validate event ownership
    # 2. Store events in database
    # 3. Detect conflicts with existing events
    # 4. Return server-side updates

    synced_ids = [event.id for event in events]

    log.info(
        "mobile_sync_completed",
        synced_count=len(synced_ids),
    )

    return MobileSyncResponse(
        synced_ids=synced_ids,
        server_events=[],
    )


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    summary="Synchronize client data with server",
    responses={
        200: {"description": "Sync completed successfully"},
        304: {"description": "Not Modified - server state unchanged (ETag match)"},
        400: {"description": "Invalid sync request"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied to one or more entities"},
        409: {"description": "Sync conflicts requiring client resolution"},
    },
)
async def sync_data(
    body: Annotated[dict[str, Any], Body()],
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
    if_none_match: Annotated[str | None, Header()] = None,
) -> SyncResponse | MobileSyncResponse:
    """Synchronize offline client changes with the server.

    Supports two request formats:

    1. Mobile app format:
       { "events": [...], "client_timestamp": "..." }
       Returns: { "synced_ids": [...], "server_events": [...] }

    2. Standard format:
       { "changes": [...], "last_sync_at": "...", ... }
       Returns: { "server_state": {...}, "conflicts": [...], ... }

    The sync process:
    1. Validates entity ownership for all client changes
    2. Applies client changes with conflict resolution
    3. Returns current server state (delta or full)
    4. Generates sync token for next request
    """
    correlation_id = _generate_correlation_id()
    start_time = time.monotonic()

    # Detect mobile app format and handle separately
    if _is_mobile_request(body):
        return await _handle_mobile_sync(body, db, current_user, correlation_id)

    # Standard sync format - parse into SyncRequest
    try:
        request = SyncRequest.model_validate(body)
    except ValidationError as e:
        logger.warning(
            "sync_validation_error",
            correlation_id=correlation_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid sync request: {e}",
        ) from None

    log = logger.bind(
        correlation_id=correlation_id,
        user_id=str(current_user.id),
        changes_count=len(request.changes),
        last_sync_at=request.last_sync_at.isoformat() if request.last_sync_at else None,
        page_size=request.page_size,
        cursor=request.cursor,
    )

    log.info("sync_request_received")

    try:
        # Process sync through service layer
        sync_response = await process_sync(db, current_user.id, request)

        duration_ms = (time.monotonic() - start_time) * 1000

        # Log conflicts for debugging
        for conflict in sync_response.conflicts:
            log.warning(
                "sync_conflict_detected",
                entity_type=conflict.entity_type,
                entity_id=str(conflict.entity_id),
                resolution=conflict.resolution,
                client_updated_at=conflict.client_updated_at.isoformat(),
                server_updated_at=conflict.server_updated_at.isoformat(),
            )

        log.info(
            "sync_completed",
            conflicts_count=len(sync_response.conflicts),
            aquariums_count=len(sync_response.server_state.aquariums),
            fish_count=len(sync_response.server_state.fish),
            events_count=len(sync_response.server_state.events),
            duration_ms=round(duration_ms, 2),
            sync_token=sync_response.sync_token,
        )

        # ETag cache validation
        # Check if client has the same state (If-None-Match matches current state)
        # For delta sync with no changes, return 304 Not Modified
        if (
            if_none_match is not None
            and request.last_sync_at is not None
            and len(request.changes) == 0
            and len(sync_response.server_state.aquariums) == 0
            and len(sync_response.server_state.fish) == 0
            and len(sync_response.server_state.events) == 0
            and len(sync_response.server_state.deleted.aquariums) == 0
            and len(sync_response.server_state.deleted.fish) == 0
        ):
            log.info(
                "sync_not_modified",
                if_none_match=if_none_match,
                duration_ms=round(duration_ms, 2),
            )
            raise HTTPException(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers={"ETag": f'"{sync_response.sync_token}"'},
            )

        # Set ETag header for cache validation
        response.headers["ETag"] = f'"{sync_response.sync_token}"'
        response.headers["X-Correlation-ID"] = correlation_id

        return sync_response

    except SyncValidationError as e:
        duration_ms = (time.monotonic() - start_time) * 1000
        log.warning(
            "sync_validation_error",
            error=e.message,
            duration_ms=round(duration_ms, 2),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=e.message,
        ) from None

    except SyncAccessDeniedError as e:
        duration_ms = (time.monotonic() - start_time) * 1000
        log.warning(
            "sync_access_denied",
            error=e.message,
            duration_ms=round(duration_ms, 2),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=e.message,
        ) from None

    except SyncError as e:
        duration_ms = (time.monotonic() - start_time) * 1000
        log.error(
            "sync_error",
            error=e.message,
            status_code=e.status_code,
            duration_ms=round(duration_ms, 2),
        )
        # Use 409 for conflicts requiring client resolution
        if e.status_code == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=e.message,
            ) from None
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Sync processing failed",
        ) from None

    except HTTPException:
        # Re-raise HTTP exceptions (like 304 Not Modified)
        raise

    except Exception as e:
        duration_ms = (time.monotonic() - start_time) * 1000
        log.exception(
            "sync_unexpected_error",
            error=str(e),
            duration_ms=round(duration_ms, 2),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Sync processing failed",
        ) from None
