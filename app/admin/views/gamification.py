"""Admin views for Streak, Achievement, and UserProgress models."""

from sqladmin import ModelView

from app.models.gamification import Achievement, Streak, UserProgress


class StreakAdmin(ModelView, model=Streak):
    """Streak admin view — read + edit only (no create/delete)."""

    column_list = [
        Streak.user_id,
        Streak.current_streak,
        Streak.best_streak,
        Streak.freeze_available,
        Streak.freeze_used_this_period,
        Streak.last_feed_date,
        Streak.updated_at,
    ]
    column_sortable_list = [Streak.current_streak, Streak.best_streak, Streak.updated_at]

    can_create = False
    can_delete = False
    name = "Streak"
    name_plural = "Streaks"
    icon = "fa-solid fa-fire"


class AchievementAdmin(ModelView, model=Achievement):
    """Achievement admin view — read-only."""

    column_list = [
        Achievement.id,
        Achievement.user_id,
        Achievement.achievement_type,
        Achievement.unlocked_at,
        Achievement.shared_at,
    ]
    column_searchable_list = [Achievement.achievement_type]
    column_sortable_list = [Achievement.achievement_type, Achievement.unlocked_at]

    can_create = False
    can_edit = False
    can_delete = False
    name = "Achievement"
    name_plural = "Achievements"
    icon = "fa-solid fa-trophy"


class UserProgressAdmin(ModelView, model=UserProgress):
    """UserProgress admin view — read + edit only (no create/delete)."""

    column_list = [
        UserProgress.user_id,
        UserProgress.total_xp,
        UserProgress.level,
        UserProgress.last_xp_awarded_at,
        UserProgress.last_level_up_at,
        UserProgress.updated_at,
    ]
    column_sortable_list = [UserProgress.total_xp, UserProgress.level, UserProgress.updated_at]

    can_create = False
    can_delete = False
    name = "User Progress"
    name_plural = "User Progress"
    icon = "fa-solid fa-chart-line"
