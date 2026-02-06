"""Admin panel setup and view registration."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
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
    ReleasesView,
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

    templates_dir = str(Path(__file__).resolve().parent / "templates")

    admin = Admin(
        app=app,
        engine=engine,
        authentication_backend=authentication_backend,
        title="FishFeed Admin",
        templates_dir=templates_dir,
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
    admin.add_view(ReleasesView)

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/admin/")

    return admin
