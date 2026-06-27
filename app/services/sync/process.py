"""Sync orchestration: main process_sync function."""

from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorCode
from app.schemas.sync import SyncRequest, SyncResponse

from .changes import apply_changes
from .exceptions import SyncAccessDeniedError, SyncError, SyncValidationError
from .state import get_paginated_server_state
from .utils import _generate_sync_token
from .validation import _validate_entity_ownership

logger = structlog.get_logger(__name__)


async def process_sync(
    db: AsyncSession,
    user_id: UUID,
    request: SyncRequest,
) -> SyncResponse:
    """Process sync request from client.

    Orchestrates the entire sync process:
    1. Validates entity ownership
    2. Applies client changes with conflict resolution
    3. Retrieves server state (delta or full)
    4. Applies pagination to server state
    5. Generates sync token

    All changes are processed in a single transaction with rollback on error.

    Args:
        db: Database session.
        user_id: User ID performing the sync.
        request: Sync request with client changes.

    Returns:
        SyncResponse with server state, conflicts, sync token, and pagination info.

    Raises:
        SyncValidationError: If request validation fails.
        SyncAccessDeniedError: If user doesn't have access to entities.
    """
    logger.info(
        "Processing sync for user",
        user_id=user_id,
        change_count=len(request.changes),
        last_sync_at=request.last_sync_at,
        page_size=request.page_size,
        cursor=request.cursor,
    )

    try:
        # Step 1: Validate entity ownership
        await _validate_entity_ownership(db, user_id, request.changes)

        # Step 2: Apply client changes and collect conflicts
        conflicts = await apply_changes(db, user_id, request.changes)

        # NOTE: No server-side schedule generation here.
        # Schedules are created by the client (offline-first architecture).
        # Server only stores what client sends via sync.

        # Step 3+4: Get a single page of server state with DB-level pagination
        # (delta sync if last_sync_at provided). This pushes OFFSET/LIMIT down to
        # the database instead of loading the full state and slicing in Python.
        paginated_state, has_more, next_cursor = await get_paginated_server_state(
            db, user_id, request.last_sync_at, request.page_size, request.cursor
        )

        # Step 5: Compute synced_ids (accepted changes without conflicts)
        conflict_entity_ids = {c.entity_id for c in conflicts}
        synced_ids = [
            change.entity_id
            for change in request.changes
            if change.entity_id not in conflict_entity_ids
        ]

        # Step 6: Generate sync token
        sync_token = _generate_sync_token()

        # Flush remaining changes (commit handled by get_db dependency)
        await db.flush()

        logger.info(
            "Sync completed for user",
            user_id=user_id,
            synced_count=len(synced_ids),
            conflict_count=len(conflicts),
            has_more=has_more,
            token=sync_token,
        )

        return SyncResponse(
            server_state=paginated_state,
            conflicts=conflicts,
            synced_ids=synced_ids,
            sync_token=sync_token,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    except (SyncValidationError, SyncAccessDeniedError):
        # Re-raise sync-specific errors (rollback handled by get_db dependency)
        raise
    except Exception as e:
        # Re-raise as SyncError (rollback handled by get_db dependency)
        logger.error("Sync failed for user", user_id=user_id, error=str(e), exc_info=True)
        raise SyncError(
            ErrorCode.SYNC_FAILED,
            f"Sync processing failed: {e}",
            status_code=500,
        ) from e
