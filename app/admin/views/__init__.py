"""Admin ModelView classes for all application models."""

from app.admin.views.ai import AIScanAdmin
from app.admin.views.analytics import AnalyticsEventAdmin
from app.admin.views.aquarium import AquariumAdmin, AquariumMemberAdmin, FamilyInviteAdmin
from app.admin.views.backup import (
    BackupsDashboardView,
    BackupSettingsAdmin,
    DatabaseBackupAdmin,
)
from app.admin.views.feeding import FeedingLogAdmin, FeedingScheduleAdmin
from app.admin.views.fish import FishAdmin
from app.admin.views.gamification import AchievementAdmin, StreakAdmin, UserProgressAdmin
from app.admin.views.notification import NotificationPreferenceAdmin, PushTokenAdmin
from app.admin.views.purchase import WebhookTransactionAdmin
from app.admin.views.releases import ReleasesView
from app.admin.views.species import SpeciesAdmin
from app.admin.views.user import UserAdmin

__all__ = [
    "UserAdmin",
    "AquariumAdmin",
    "AquariumMemberAdmin",
    "FamilyInviteAdmin",
    "SpeciesAdmin",
    "FishAdmin",
    "FeedingScheduleAdmin",
    "FeedingLogAdmin",
    "StreakAdmin",
    "AchievementAdmin",
    "UserProgressAdmin",
    "AIScanAdmin",
    "PushTokenAdmin",
    "NotificationPreferenceAdmin",
    "AnalyticsEventAdmin",
    "WebhookTransactionAdmin",
    "ReleasesView",
    "BackupsDashboardView",
    "DatabaseBackupAdmin",
    "BackupSettingsAdmin",
]
