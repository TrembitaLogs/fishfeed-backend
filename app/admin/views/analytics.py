"""Admin view for AnalyticsEvent model."""

from sqladmin import ModelView

from app.models.analytics import AnalyticsEvent


class AnalyticsEventAdmin(ModelView, model=AnalyticsEvent):
    """AnalyticsEvent admin view — read-only."""

    column_list = [
        AnalyticsEvent.id,
        AnalyticsEvent.user_id,
        AnalyticsEvent.event_type,
        AnalyticsEvent.ip_hash,
        AnalyticsEvent.created_at,
        AnalyticsEvent.anonymized_at,
    ]
    column_searchable_list = [AnalyticsEvent.event_type]
    column_sortable_list = [AnalyticsEvent.event_type, AnalyticsEvent.created_at]

    can_create = False
    can_edit = False
    can_delete = False
    name = "Analytics Event"
    name_plural = "Analytics Events"
    icon = "fa-solid fa-chart-bar"
