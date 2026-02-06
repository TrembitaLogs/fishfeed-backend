"""Admin views for FeedingSchedule and FeedingLog models."""

from sqladmin import ModelView

from app.models.feeding import FeedingLog, FeedingSchedule


class FeedingScheduleAdmin(ModelView, model=FeedingSchedule):
    """FeedingSchedule admin view — read + edit only (no create/delete)."""

    column_list = [
        FeedingSchedule.id,
        FeedingSchedule.aquarium_id,
        FeedingSchedule.fish_id,
        FeedingSchedule.time,
        FeedingSchedule.interval_days,
        FeedingSchedule.food_type,
        FeedingSchedule.active,
        FeedingSchedule.created_at,
    ]
    column_searchable_list = [FeedingSchedule.food_type]
    column_sortable_list = [FeedingSchedule.time, FeedingSchedule.created_at]

    can_create = False
    can_delete = False
    name = "Feeding Schedule"
    name_plural = "Feeding Schedules"
    icon = "fa-solid fa-clock"


class FeedingLogAdmin(ModelView, model=FeedingLog):
    """FeedingLog admin view — read-only."""

    column_list = [
        FeedingLog.id,
        FeedingLog.aquarium_id,
        FeedingLog.fish_id,
        FeedingLog.schedule_id,
        FeedingLog.action,
        FeedingLog.scheduled_for,
        FeedingLog.acted_at,
        FeedingLog.acted_by_user_id,
        FeedingLog.created_at,
    ]
    column_sortable_list = [FeedingLog.scheduled_for, FeedingLog.acted_at, FeedingLog.created_at]

    can_create = False
    can_edit = False
    can_delete = False
    name = "Feeding Log"
    name_plural = "Feeding Logs"
    icon = "fa-solid fa-list-check"
