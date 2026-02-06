"""Admin views for PushToken and NotificationPreference models."""

from sqladmin import ModelView

from app.models.notification import NotificationPreference, PushToken


class PushTokenAdmin(ModelView, model=PushToken):
    """PushToken admin view — read-only."""

    column_list = [
        PushToken.id,
        PushToken.user_id,
        PushToken.platform,
        PushToken.created_at,
        PushToken.updated_at,
    ]
    column_sortable_list = [PushToken.platform, PushToken.created_at]

    can_create = False
    can_edit = False
    can_delete = False
    name = "Push Token"
    name_plural = "Push Tokens"
    icon = "fa-solid fa-bell"


class NotificationPreferenceAdmin(ModelView, model=NotificationPreference):
    """NotificationPreference admin view — read-only."""

    column_list = [
        NotificationPreference.user_id,
        NotificationPreference.global_opt_out,
        NotificationPreference.feeding_reminders,
        NotificationPreference.overdue_alerts,
        NotificationPreference.streak_protection,
        NotificationPreference.weekly_summary,
        NotificationPreference.family_updates,
        NotificationPreference.marketing,
        NotificationPreference.updated_at,
    ]
    column_sortable_list = [NotificationPreference.updated_at]

    can_create = False
    can_edit = False
    can_delete = False
    name = "Notification Preference"
    name_plural = "Notification Preferences"
    icon = "fa-solid fa-sliders"
