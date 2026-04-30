"""Feeding schedule and feeding log API endpoints."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.schemas.feeding import (
    FeedingLogConflictResponse,
    FeedingLogCreate,
    FeedingLogResponse,
    ScheduleCreate,
    ScheduleResponse,
    ScheduleUpdate,
)
from app.services.feeding import (
    FeedingLogConflictError,
    create_feeding_log,
    create_schedule,
    delete_schedule,
    generate_schedule,
    get_feeding_logs,
    get_schedules,
    update_schedule,
)

router = APIRouter(tags=["Feeding"])


# ── Schedule endpoints ──────────────────────────────────────────────


@router.get(
    "/aquariums/{aquarium_id}/schedules",
    response_model=list[ScheduleResponse],
    summary="List feeding schedules",
    responses={
        200: {"description": "List of feeding schedules"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium not found"},
    },
)
async def list_schedules(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
    active: bool | None = Query(default=None),
) -> list[ScheduleResponse]:
    """Get feeding schedules for an aquarium. Filter by active status optionally."""
    schedules = await get_schedules(db, aquarium_id, current_user.id, active=active)
    return [ScheduleResponse.model_validate(s) for s in schedules]


@router.post(
    "/aquariums/{aquarium_id}/schedules",
    response_model=ScheduleResponse,
    status_code=201,
    summary="Create feeding schedule",
    responses={
        201: {"description": "Schedule created"},
        400: {"description": "Validation error"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium not found"},
    },
)
async def create_aquarium_schedule(
    aquarium_id: UUID,
    data: ScheduleCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> ScheduleResponse:
    """Create a new feeding schedule for an aquarium."""
    schedule = await create_schedule(db, aquarium_id, current_user.id, data)
    return ScheduleResponse.model_validate(schedule)


@router.patch(
    "/aquariums/{aquarium_id}/schedules/{schedule_id}",
    response_model=ScheduleResponse,
    summary="Update feeding schedule",
    responses={
        200: {"description": "Schedule updated"},
        400: {"description": "Validation error"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Schedule not found"},
    },
)
async def update_aquarium_schedule(
    aquarium_id: UUID,
    schedule_id: UUID,
    data: ScheduleUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> ScheduleResponse:
    """Partially update a feeding schedule."""
    schedule = await update_schedule(db, aquarium_id, schedule_id, current_user.id, data)
    return ScheduleResponse.model_validate(schedule)


@router.delete(
    "/aquariums/{aquarium_id}/schedules/{schedule_id}",
    status_code=204,
    summary="Delete feeding schedule",
    responses={
        204: {"description": "Schedule deleted"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Schedule not found"},
    },
)
async def delete_aquarium_schedule(
    aquarium_id: UUID,
    schedule_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> None:
    """Delete a feeding schedule."""
    await delete_schedule(db, aquarium_id, schedule_id, current_user.id)


@router.post(
    "/aquariums/{aquarium_id}/schedules/generate",
    response_model=list[ScheduleResponse],
    summary="Auto-generate feeding schedules",
    responses={
        200: {"description": "Schedules generated"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium not found"},
    },
)
async def generate_aquarium_schedules(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> list[ScheduleResponse]:
    """Auto-generate feeding schedules based on fish species in aquarium."""
    schedules = await generate_schedule(db, aquarium_id, current_user.id)
    return [ScheduleResponse.model_validate(s) for s in schedules]


# ── FeedingLog endpoints ────────────────────────────────────────────


@router.get(
    "/aquariums/{aquarium_id}/feeding-logs",
    response_model=list[FeedingLogResponse],
    summary="List feeding logs",
    responses={
        200: {"description": "List of feeding logs"},
        400: {"description": "Invalid date range"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium not found"},
    },
)
async def list_feeding_logs(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
    from_date: datetime = Query(..., alias="from"),
    to_date: datetime = Query(..., alias="to"),
    fish_id: UUID | None = Query(default=None),
) -> list[FeedingLogResponse]:
    """Get feeding logs for an aquarium within a date range."""
    # Validate max date range
    delta = to_date - from_date
    if delta.days > 366:
        raise AppError(
            code=ErrorCode.FEEDING_DATE_RANGE_TOO_LARGE,
            message="Date range cannot exceed 366 days",
            status_code=400,
        )

    logs = await get_feeding_logs(db, aquarium_id, current_user.id, from_date, to_date, fish_id)
    responses = []
    for log in logs:
        resp = FeedingLogResponse.model_validate(log)
        resp.acted_by_user_name = log.acted_by_user.nickname if log.acted_by_user else None
        responses.append(resp)
    return responses


@router.post(
    "/aquariums/{aquarium_id}/feeding-logs",
    response_model=FeedingLogResponse,
    status_code=201,
    summary="Create feeding log",
    responses={
        201: {"description": "Feeding log created"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium not found"},
        409: {"description": "Duplicate feeding log", "model": FeedingLogConflictResponse},
    },
)
async def create_aquarium_feeding_log(
    aquarium_id: UUID,
    data: FeedingLogCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> FeedingLogResponse | JSONResponse:
    """Create a feeding log entry. Returns 409 on duplicate (schedule_id, scheduled_for)."""
    try:
        log = await create_feeding_log(db, aquarium_id, current_user.id, data)
    except FeedingLogConflictError as e:
        # Conflict response carries the existing log so the client can resolve.
        # This payload diverges from the standard error_code/detail contract by design.
        existing_resp = FeedingLogResponse.model_validate(e.existing_log)
        existing_resp.acted_by_user_name = e.acted_by_user_name
        conflict = FeedingLogConflictResponse(
            error="conflict",
            message=e.message,
            existing_log=existing_resp,
        )
        return JSONResponse(
            status_code=409,
            content=conflict.model_dump(mode="json"),
        )

    resp = FeedingLogResponse.model_validate(log)
    resp.acted_by_user_name = log.acted_by_user.nickname if log.acted_by_user else None
    return resp
