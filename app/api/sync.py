"""Sync API endpoint for offline-first data synchronization."""

import time
from typing import Annotated, Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response, status
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.redis import get_redis
from app.schemas.sync import (
    FailedChange,
    SyncRequest,
    SyncResponse,
)
from app.services.sync import (
    SyncError,
    process_sync,
)
from app.utils.cache import invalidate_user_gamification_keys

router = APIRouter(prefix="/sync", tags=["Sync"])

logger = structlog.get_logger(__name__)


def _generate_correlation_id() -> str:
    """Generate unique correlation ID for request tracing."""
    return uuid4().hex[:16]


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    response_model=SyncResponse,
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
    redis: Annotated[Redis, Depends(get_redis)],
    if_none_match: Annotated[str | None, Header()] = None,
) -> SyncResponse:
    """Synchronize offline client changes with the server.

    Standard format:
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

    # Pre-split changes by entity_id parseability so that ONE bad UUID does not
    # block the entire batch. Everything else still goes through strict Pydantic.
    raw_changes = body.get("changes", [])
    valid_raw: list[dict[str, Any]] = []
    failed_items: list[FailedChange] = []
    if isinstance(raw_changes, list):
        for idx, item in enumerate(raw_changes):
            if not isinstance(item, dict):
                # Let Pydantic produce a descriptive 422 for non-dict items;
                # we only want to rescue the entity_id-malformed case.
                valid_raw.append(item)  # type: ignore[arg-type]
                continue

            raw_id = item.get("entity_id")
            try:
                if isinstance(raw_id, str):
                    UUID(raw_id)
                else:
                    raise ValueError("entity_id must be a string")
            except (ValueError, TypeError) as exc:
                failed_items.append(
                    FailedChange(
                        index=idx,
                        entity_type=str(item.get("entity_type", "")),
                        entity_id=str(raw_id) if raw_id is not None else "",
                        error_code=ErrorCode.SYNC_INVALID_ENTITY_ID.value,
                        error_message=f"entity_id is not a valid UUID: {exc}",
                    )
                )
                continue
            valid_raw.append(item)

    rebuilt = {**body, "changes": valid_raw}

    try:
        request = SyncRequest.model_validate(rebuilt)
    except ValidationError as e:
        logger.warning(
            "sync_validation_error",
            correlation_id=correlation_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=e.errors(),
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
            feeding_logs_count=len(sync_response.server_state.feeding_logs),
            duration_ms=round(duration_ms, 2),
            sync_token=sync_response.sync_token,
        )

        # Invalidate gamification caches if client sent changes
        if request.changes:
            try:
                keys = invalidate_user_gamification_keys(str(current_user.id))
                await redis.delete(*keys)
            except RedisError as e:
                logger.warning("Redis gamification cache invalidation error", error=str(e))

        # ETag cache validation
        if (
            if_none_match is not None
            and request.last_sync_at is not None
            and len(request.changes) == 0
            and len(sync_response.server_state.aquariums) == 0
            and len(sync_response.server_state.fish) == 0
            and len(sync_response.server_state.feeding_logs) == 0
            and len(sync_response.server_state.deleted.aquariums) == 0
            and len(sync_response.server_state.deleted.fish) == 0
        ):
            log.info(
                "sync_not_modified",
                if_none_match=if_none_match,
                duration_ms=round(duration_ms, 2),
            )
            # 304 Not Modified is a control-flow status, not an application error.
            # FastAPI's HTTPException is the appropriate primitive here.
            raise HTTPException(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers={"ETag": f'"{sync_response.sync_token}"'},
            )

        if failed_items:
            log.warning(
                "sync_partial_accept",
                failed_count=len(failed_items),
                failed_indices=[f.index for f in failed_items],
            )

        sync_response = sync_response.model_copy(update={"failed": failed_items})

        # Set ETag header for cache validation
        response.headers["ETag"] = f'"{sync_response.sync_token}"'
        response.headers["X-Correlation-ID"] = correlation_id

        return sync_response

    except SyncError as e:
        # SyncError is now an AppError — log and re-raise so the global
        # handler formats the standardized error_code/detail response.
        duration_ms = (time.monotonic() - start_time) * 1000
        log.warning(
            "sync_error",
            code=e.code.value,
            error=e.message,
            status_code=e.status_code,
            duration_ms=round(duration_ms, 2),
        )
        raise

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
        raise AppError(
            code=ErrorCode.SYNC_FAILED,
            message="Sync processing failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        ) from None
