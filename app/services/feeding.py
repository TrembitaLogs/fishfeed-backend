"""Feeding service with business logic for feeding schedules and logs."""

from datetime import date, datetime, timedelta
from datetime import time as dt_time
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.jobs.notification_jobs import family_feeding_trigger
from app.models.feeding import FeedingLog, FeedingSchedule
from app.models.fish import Fish
from app.schemas.feeding import FeedingLogCreate, ScheduleCreate, ScheduleUpdate
from app.services.aquarium import check_access
from app.services.gamification import check_achievements, update_streak

logger = structlog.get_logger(__name__)

# Predefined time distributions for common feeding frequencies
DEFAULT_TIMES: dict[int, list[str]] = {
    1: ["09:00"],
    2: ["09:00", "18:00"],
    3: ["08:00", "13:00", "18:00"],
}


class FeedingError(Exception):
    """Base exception for feeding errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ScheduleNotFoundError(FeedingError):
    """Raised when feeding schedule is not found."""

    def __init__(self, schedule_id: UUID):
        super().__init__(f"Feeding schedule '{schedule_id}' not found", status_code=404)


class FeedingLogConflictError(FeedingError):
    """Raised when a duplicate feeding log is detected."""

    def __init__(self, existing_log: FeedingLog, acted_by_user_name: str | None = None):
        self.existing_log = existing_log
        self.acted_by_user_name = acted_by_user_name
        super().__init__("Feeding log already exists for this schedule and time", status_code=409)


def _compute_even_times(frequency: int) -> list[str]:
    """Compute evenly distributed feeding times for a given frequency.

    For 1-3 uses predefined times, for 4+ distributes between 07:00 and 21:00.
    """
    if frequency in DEFAULT_TIMES:
        return DEFAULT_TIMES[frequency]

    # Distribute between 07:00 and 21:00
    start_minutes = 7 * 60
    end_minutes = 21 * 60
    step = (end_minutes - start_minutes) / (frequency - 1)

    times: list[str] = []
    for i in range(frequency):
        total_minutes = int(start_minutes + i * step)
        # Round to nearest 5 minutes
        total_minutes = round(total_minutes / 5) * 5
        hours, minutes = divmod(total_minutes, 60)
        times.append(f"{hours:02d}:{minutes:02d}")
    return times


async def generate_schedule(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
) -> list[FeedingSchedule]:
    """Generate feeding schedules based on fish species in aquarium.

    Creates per-fish schedules: for each fish, creates N schedules where N
    equals species.feeding_frequency, with evenly distributed times.

    Idempotent: skips fish that already have schedules to prevent duplicates.
    """
    await check_access(db, aquarium_id, user_id)

    # Get all active fish with their species
    fish_stmt = (
        select(Fish)
        .where(Fish.aquarium_id == aquarium_id)
        .where(Fish.deleted_at.is_(None))
        .options(selectinload(Fish.species))
    )
    fish_result = await db.execute(fish_stmt)
    fish_list = list(fish_result.scalars().all())

    if not fish_list:
        return []

    # Get existing schedules to avoid duplicates
    fish_ids = [f.id for f in fish_list]
    existing_stmt = select(FeedingSchedule.fish_id, FeedingSchedule.time).where(
        FeedingSchedule.fish_id.in_(fish_ids)
    )
    existing_result = await db.execute(existing_stmt)
    existing_schedules: set[tuple[UUID, dt_time]] = {
        (row.fish_id, row.time) for row in existing_result
    }

    created_schedules: list[FeedingSchedule] = []
    today = date.today()

    for fish in fish_list:
        frequency = (
            fish.species.feeding_frequency
            if fish.species and fish.species.feeding_frequency
            else 1
        )
        frequency = max(1, frequency)

        food_types = (
            ", ".join(sorted(fish.species.food_types))
            if fish.species and fish.species.food_types
            else "flakes"
        )
        portion_hint = fish.species.portion_hint if fish.species else None

        times = _compute_even_times(frequency)

        for time_str in times:
            time_obj = dt_time.fromisoformat(time_str)

            # Skip if schedule already exists for this fish+time combination
            if (fish.id, time_obj) in existing_schedules:
                logger.debug(f"Skipping existing schedule for fish {fish.id} at {time_str}")
                continue

            schedule = FeedingSchedule(
                aquarium_id=aquarium_id,
                fish_id=fish.id,
                time=time_obj,
                interval_days=1,
                anchor_date=today,
                food_type=food_types,
                portion_hint=portion_hint,
                active=True,
                created_by_user_id=user_id,
            )
            db.add(schedule)
            created_schedules.append(schedule)

    if created_schedules:
        await db.flush()
        for schedule in created_schedules:
            await db.refresh(schedule)

    logger.info(
        f"Generated {len(created_schedules)} new schedules for aquarium"
        f" '{aquarium_id}' (skipped {len(existing_schedules)} existing)"
    )
    return created_schedules


async def get_schedules(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
    active: bool | None = None,
) -> list[FeedingSchedule]:
    """Get feeding schedules for aquarium with optional active filter."""
    await check_access(db, aquarium_id, user_id)

    stmt = select(FeedingSchedule).where(FeedingSchedule.aquarium_id == aquarium_id)
    if active is not None:
        stmt = stmt.where(FeedingSchedule.active == active)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_schedule(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
    data: ScheduleCreate,
) -> FeedingSchedule:
    """Create a single feeding schedule with validation."""
    await check_access(db, aquarium_id, user_id)

    # Validate fish belongs to this aquarium
    fish_stmt = select(Fish).where(
        Fish.id == data.fish_id,
        Fish.aquarium_id == aquarium_id,
        Fish.deleted_at.is_(None),
    )
    fish_result = await db.execute(fish_stmt)
    fish = fish_result.scalar_one_or_none()
    if fish is None:
        raise FeedingError(
            f"Fish '{data.fish_id}' not found in aquarium '{aquarium_id}'",
            status_code=400,
        )

    # Validate anchor_date not >7 days in future
    max_anchor = date.today() + timedelta(days=7)
    if data.anchor_date > max_anchor:
        raise FeedingError(
            "anchor_date cannot be more than 7 days in the future",
            status_code=400,
        )

    schedule = FeedingSchedule(
        aquarium_id=aquarium_id,
        fish_id=data.fish_id,
        time=dt_time.fromisoformat(data.time),
        interval_days=data.interval_days,
        anchor_date=data.anchor_date,
        food_type=data.food_type,
        portion_hint=data.portion_hint,
        active=True,
        created_by_user_id=user_id,
    )
    db.add(schedule)
    await db.flush()
    await db.refresh(schedule)

    logger.info(f"Created schedule '{schedule.id}' for aquarium '{aquarium_id}'")
    return schedule


async def update_schedule(
    db: AsyncSession,
    aquarium_id: UUID,
    schedule_id: UUID,
    user_id: UUID,
    data: ScheduleUpdate,
) -> FeedingSchedule:
    """Update a single schedule by ID with partial data."""
    await check_access(db, aquarium_id, user_id)

    stmt = select(FeedingSchedule).where(
        FeedingSchedule.id == schedule_id,
        FeedingSchedule.aquarium_id == aquarium_id,
    )
    result = await db.execute(stmt)
    schedule = result.scalar_one_or_none()

    if schedule is None:
        raise ScheduleNotFoundError(schedule_id)

    update_data = data.model_dump(exclude_unset=True)

    # Convert time string to time object if provided
    if "time" in update_data and update_data["time"] is not None:
        update_data["time"] = dt_time.fromisoformat(update_data["time"])

    # Validate anchor_date if provided
    if "anchor_date" in update_data and update_data["anchor_date"] is not None:
        max_anchor = date.today() + timedelta(days=7)
        if update_data["anchor_date"] > max_anchor:
            raise FeedingError(
                "anchor_date cannot be more than 7 days in the future",
                status_code=400,
            )

    for field, value in update_data.items():
        setattr(schedule, field, value)

    await db.flush()
    await db.refresh(schedule)

    logger.info(f"Updated schedule '{schedule_id}'")
    return schedule


async def delete_schedule(
    db: AsyncSession,
    aquarium_id: UUID,
    schedule_id: UUID,
    user_id: UUID,
) -> None:
    """Hard delete a schedule by ID."""
    await check_access(db, aquarium_id, user_id)

    stmt = select(FeedingSchedule).where(
        FeedingSchedule.id == schedule_id,
        FeedingSchedule.aquarium_id == aquarium_id,
    )
    result = await db.execute(stmt)
    schedule = result.scalar_one_or_none()

    if schedule is None:
        raise ScheduleNotFoundError(schedule_id)

    await db.delete(schedule)
    await db.flush()

    logger.info(f"Deleted schedule '{schedule_id}'")


async def get_feeding_logs(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
    from_date: datetime,
    to_date: datetime,
    fish_id: UUID | None = None,
) -> list[FeedingLog]:
    """Get feeding logs for aquarium within date range."""
    await check_access(db, aquarium_id, user_id)

    stmt = (
        select(FeedingLog)
        .where(FeedingLog.aquarium_id == aquarium_id)
        .where(FeedingLog.scheduled_for >= from_date)
        .where(FeedingLog.scheduled_for <= to_date)
        .options(selectinload(FeedingLog.acted_by_user))
        .order_by(FeedingLog.scheduled_for.desc())
    )

    if fish_id is not None:
        stmt = stmt.where(FeedingLog.fish_id == fish_id)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_feeding_log(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
    data: FeedingLogCreate,
) -> FeedingLog:
    """Create a feeding log with duplicate detection.

    On UNIQUE(schedule_id, scheduled_for) conflict, raises FeedingLogConflictError
    with the existing log details.
    """
    await check_access(db, aquarium_id, user_id)

    log = FeedingLog(
        schedule_id=data.schedule_id,
        fish_id=data.fish_id,
        aquarium_id=aquarium_id,
        scheduled_for=data.scheduled_for,
        action=data.action.value,
        acted_by_user_id=user_id,
        device_id=data.device_id,
        notes=data.notes,
    )
    try:
        # Use savepoint so IntegrityError only rolls back this insert,
        # not the entire transaction (other data created earlier stays intact).
        async with db.begin_nested():
            db.add(log)
            await db.flush()
    except IntegrityError:
        # Fetch existing log with user info for conflict response
        existing_stmt = (
            select(FeedingLog)
            .where(
                FeedingLog.schedule_id == data.schedule_id,
                FeedingLog.scheduled_for == data.scheduled_for,
            )
            .options(selectinload(FeedingLog.acted_by_user))
        )
        existing_result = await db.execute(existing_stmt)
        existing_log = existing_result.scalar_one()

        user_name = (
            existing_log.acted_by_user.nickname
            if existing_log.acted_by_user
            else None
        )
        raise FeedingLogConflictError(existing_log, user_name) from None

    await db.flush()
    await db.refresh(log, ["acted_by_user"])

    # Post-create side effects for 'fed' action
    if data.action.value == "fed":
        try:
            await update_streak(db, user_id)
            await check_achievements(db, user_id)
        except Exception as e:
            logger.error(f"Failed to update streak/achievements for user '{user_id}': {e}")

        try:
            await family_feeding_trigger(db, log.id, user_id)
        except Exception as e:
            logger.error(f"Failed to send family feeding notification: {e}")

    logger.info(f"Created feeding log '{log.id}' for aquarium '{aquarium_id}'")
    return log
