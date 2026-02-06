"""Admin panel setup and view registration."""

from fastapi import FastAPI
from sqladmin import Admin

from app.admin.auth import AdminAuth
from app.admin.views import (
    AchievementAdmin,
    AIScanAdmin,
    AnalyticsEventAdmin,
    AquariumAdmin,
    AquariumMemberAdmin,
    FamilyInviteAdmin,
    FeedingLogAdmin,
    FeedingScheduleAdmin,
    FishAdmin,
    NotificationPreferenceAdmin,
    PushTokenAdmin,
    SpeciesAdmin,
    StreakAdmin,
    UserAdmin,
    UserProgressAdmin,
    WebhookTransactionAdmin,
)


def setup_admin(app: FastAPI) -> Admin:
    """Initialize SQLAdmin with authentication and register all model views."""
    from app.config import get_settings
    from app.database import engine

    settings = get_settings()
    authentication_backend = AdminAuth(secret_key=settings.JWT_SECRET_KEY)

    admin = Admin(
        app=app,
        engine=engine,
        authentication_backend=authentication_backend,
        title="FishFeed Admin",
    )

    admin.add_view(UserAdmin)
    admin.add_view(AquariumAdmin)
    admin.add_view(AquariumMemberAdmin)
    admin.add_view(FamilyInviteAdmin)
    admin.add_view(SpeciesAdmin)
    admin.add_view(FishAdmin)
    admin.add_view(FeedingScheduleAdmin)
    admin.add_view(FeedingLogAdmin)
    admin.add_view(StreakAdmin)
    admin.add_view(AchievementAdmin)
    admin.add_view(UserProgressAdmin)
    admin.add_view(AIScanAdmin)
    admin.add_view(PushTokenAdmin)
    admin.add_view(NotificationPreferenceAdmin)
    admin.add_view(AnalyticsEventAdmin)
    admin.add_view(WebhookTransactionAdmin)

    return admin
