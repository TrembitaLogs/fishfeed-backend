"""Feeding schedule and events API endpoints."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.schemas.feeding import (
    EventResponse,
    ScheduleResponse,
    ScheduleUpdate,
    TodayEventsResponse,
)
from app.services.aquarium import AquariumAccessDeniedError, AquariumNotFoundError
from app.services.feeding import (
    EventAlreadyCompletedError,
    EventNotFoundError,
    ScheduleNotFoundError,
    generate_schedule,
    get_all_events,
    get_schedule,
    get_today_events,
    mark_as_fed,
    mark_as_missed_by_user,
    update_schedule,
)

router = APIRouter(tags=["Feeding"])


@router.get(
    "/aquariums/{aquarium_id}/schedule",
    response_model=ScheduleResponse | None,
    summary="Get feeding schedule",
    responses={
        200: {"description": "Feeding schedule or null if not set"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium not found"},
    },
)
async def get_aquarium_schedule(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> ScheduleResponse | None:
    """Get the current feeding schedule for an aquarium."""
    try:
        schedule = await get_schedule(db, aquarium_id, current_user.id)
        if schedule is None:
            return None
        return ScheduleResponse.model_validate(schedule)
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.put(
    "/aquariums/{aquarium_id}/schedule",
    response_model=ScheduleResponse,
    summary="Update feeding schedule",
    responses={
        200: {"description": "Schedule updated"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium or schedule not found"},
        422: {"description": "Validation error"},
    },
)
async def update_aquarium_schedule(
    aquarium_id: UUID,
    data: ScheduleUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> ScheduleResponse:
    """Manually update the feeding schedule for an aquarium."""
    try:
        schedule = await update_schedule(db, aquarium_id, current_user.id, data)
        return ScheduleResponse.model_validate(schedule)
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except ScheduleNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.post(
    "/aquariums/{aquarium_id}/schedule/generate",
    response_model=ScheduleResponse,
    summary="Generate feeding schedule",
    responses={
        200: {"description": "Schedule generated"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium not found"},
    },
)
async def generate_aquarium_schedule(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> ScheduleResponse:
    """Auto-generate feeding schedule based on fish species in aquarium."""
    try:
        schedule = await generate_schedule(db, aquarium_id, current_user.id)
        return ScheduleResponse.model_validate(schedule)
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.get(
    "/aquariums/{aquarium_id}/events",
    response_model=list[EventResponse],
    summary="List all feeding events",
    responses={
        200: {"description": "List of all feeding events"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium not found"},
    },
)
async def list_feeding_events(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> list[EventResponse]:
    """Get all feeding events for an aquarium."""
    try:
        events = await get_all_events(db, aquarium_id, current_user.id)
        return [EventResponse.model_validate(e) for e in events]
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.get(
    "/aquariums/{aquarium_id}/events/today",
    response_model=TodayEventsResponse,
    summary="Get today's feeding events",
    responses={
        200: {"description": "Today's feeding events with next feeding time"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium not found"},
    },
)
async def get_today_feeding_events(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> TodayEventsResponse:
    """Get today's feeding events with next scheduled feeding time."""
    try:
        events = await get_today_events(db, aquarium_id, current_user.id)
        event_responses = [EventResponse.model_validate(e) for e in events]

        # Find next pending feeding
        now = datetime.now(UTC)
        next_feeding = None
        for event in events:
            if event.status == "pending" and event.scheduled_at > now:
                next_feeding = event.scheduled_at
                break

        return TodayEventsResponse(events=event_responses, next_feeding=next_feeding)
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.post(
    "/aquariums/{aquarium_id}/events/{event_id}/fed",
    response_model=EventResponse,
    summary="Mark feeding as completed",
    responses={
        200: {"description": "Event marked as fed"},
        400: {"description": "Event already completed"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Event not found"},
    },
)
async def mark_event_as_fed(
    aquarium_id: UUID,
    event_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> EventResponse:
    """Mark a feeding event as completed."""
    try:
        event = await mark_as_fed(db, event_id, current_user.id)
        return EventResponse.model_validate(event)
    except EventNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except EventAlreadyCompletedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.post(
    "/aquariums/{aquarium_id}/events/{event_id}/missed",
    response_model=EventResponse,
    summary="Mark feeding as missed",
    responses={
        200: {"description": "Event marked as missed"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Event not found"},
    },
)
async def mark_event_as_missed(
    aquarium_id: UUID,
    event_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> EventResponse:
    """Mark a feeding event as missed."""
    try:
        event = await mark_as_missed_by_user(db, event_id, current_user.id)
        return EventResponse.model_validate(event)
    except EventNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
