"""Tests for SQLAdmin ModelView classes (tasks 17.4 & 17.5).

Verifies column configuration, permission flags, and search/sort capabilities
for all 16 admin views.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.models.aquarium import Aquarium
from app.models.fish import Fish
from app.models.user import User
from app.utils.password import hash_password

TEST_PASSWORD = "Admin$ecure123"


async def _create_user(session: AsyncSession, *, is_admin: bool = True) -> User:
    """Insert a user and return it."""
    user = User(
        id=uuid.uuid4(),
        email=f"viewtest-{uuid.uuid4().hex[:8]}@test.com",
        password_hash=hash_password(TEST_PASSWORD),
        is_admin=is_admin,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _cleanup(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM fish WHERE custom_name LIKE 'viewtest-%'"))
    await session.execute(text("DELETE FROM aquariums WHERE name LIKE 'viewtest-%'"))
    await session.execute(text("DELETE FROM users WHERE email LIKE 'viewtest-%@test.com'"))
    await session.commit()


# ─── Unit tests: ModelView configuration ───────────────────────────────


class TestUserAdminConfig:
    """Verify UserAdmin class attributes are set correctly."""

    def test_password_hash_not_in_column_list(self):
        column_names = [c.key if hasattr(c, "key") else str(c) for c in UserAdmin.column_list]
        assert "password_hash" not in column_names

    def test_password_hash_excluded_from_details(self):
        excluded = [c.key if hasattr(c, "key") else str(c) for c in UserAdmin.column_details_exclude_list]
        assert "password_hash" in excluded

    def test_password_hash_excluded_from_form(self):
        excluded = [c.key if hasattr(c, "key") else str(c) for c in UserAdmin.form_excluded_columns]
        assert "password_hash" in excluded

    def test_can_delete_is_false(self):
        assert UserAdmin.can_delete is False

    def test_searchable_columns(self):
        searchable = [c.key if hasattr(c, "key") else str(c) for c in UserAdmin.column_searchable_list]
        assert "email" in searchable
        assert "nickname" in searchable

    def test_sortable_columns(self):
        sortable = [c.key if hasattr(c, "key") else str(c) for c in UserAdmin.column_sortable_list]
        assert "email" in sortable
        assert "created_at" in sortable
        assert "subscription_status" in sortable

    def test_icon(self):
        assert UserAdmin.icon == "fa-solid fa-user"


class TestAquariumAdminConfig:
    """Verify Aquarium-related admin view attributes."""

    def test_aquarium_full_crud(self):
        assert AquariumAdmin.can_create is True
        assert AquariumAdmin.can_edit is True
        assert AquariumAdmin.can_delete is True

    def test_aquarium_shows_owner_email(self):
        column_refs = [c.key if hasattr(c, "key") else str(c) for c in AquariumAdmin.column_list]
        assert "owner.email" in column_refs

    def test_member_read_delete_only(self):
        assert AquariumMemberAdmin.can_create is False
        assert AquariumMemberAdmin.can_edit is False
        assert AquariumMemberAdmin.can_delete is True

    def test_family_invite_read_only(self):
        assert FamilyInviteAdmin.can_create is False
        assert FamilyInviteAdmin.can_edit is False
        assert FamilyInviteAdmin.can_delete is False


class TestSpeciesAdminConfig:
    """Verify SpeciesAdmin attributes."""

    def test_full_crud(self):
        assert SpeciesAdmin.can_create is True
        assert SpeciesAdmin.can_edit is True
        assert SpeciesAdmin.can_delete is True

    def test_searchable_columns(self):
        searchable = [c.key if hasattr(c, "key") else str(c) for c in SpeciesAdmin.column_searchable_list]
        assert "common_name" in searchable
        assert "scientific_name" in searchable


class TestFishAdminConfig:
    """Verify FishAdmin attributes."""

    def test_full_crud(self):
        assert FishAdmin.can_create is True
        assert FishAdmin.can_edit is True
        assert FishAdmin.can_delete is True

    def test_shows_aquarium_name(self):
        column_refs = [c.key if hasattr(c, "key") else str(c) for c in FishAdmin.column_list]
        assert "aquarium.name" in column_refs

    def test_shows_species_name(self):
        column_refs = [c.key if hasattr(c, "key") else str(c) for c in FishAdmin.column_list]
        assert "species.common_name" in column_refs


# ─── Integration tests: admin UI pages ─────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestUserAdminUI:
    """Integration tests for User list page in admin panel."""

    async def test_user_list_excludes_password_hash(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/user/list should not expose password_hash column."""
        user = await _create_user(async_session)
        try:
            response = await authed_admin_client.get("/admin/user/list")

            assert response.status_code == 200
            assert "password_hash" not in response.text
        finally:
            await _cleanup(async_session)

    async def test_user_list_shows_expected_columns(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """User list should include email column with actual data."""
        user = await _create_user(async_session)
        try:
            response = await authed_admin_client.get("/admin/user/list")

            assert response.status_code == 200
            assert user.email in response.text
        finally:
            await _cleanup(async_session)

    async def test_user_delete_button_not_present(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """User detail page should not have a Delete button (can_delete=False)."""
        user = await _create_user(async_session)
        try:
            response = await authed_admin_client.get(f"/admin/user/details/{user.id}")

            assert response.status_code == 200
            # SQLAdmin hides the delete button when can_delete is False
            assert ">Delete<" not in response.text
        finally:
            await _cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestSpeciesAdminUI:
    """Integration tests for Species CRUD in admin panel."""

    async def test_species_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/species/list should render the species list page."""
        response = await authed_admin_client.get("/admin/species/list")

        assert response.status_code == 200
        # Test species seeded in conftest should appear
        assert "Test Guppy" in response.text


@pytest.mark.asyncio(loop_scope="session")
class TestFishAdminUI:
    """Integration tests for Fish admin view."""

    async def test_fish_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/fish/list should render with relationship columns."""
        user = await _create_user(async_session)
        try:
            aquarium = Aquarium(
                id=uuid.uuid4(),
                owner_id=user.id,
                name="viewtest-aquarium",
            )
            async_session.add(aquarium)
            await async_session.commit()
            await async_session.refresh(aquarium)

            fish = Fish(
                id=uuid.uuid4(),
                aquarium_id=aquarium.id,
                species_id="test-guppy",
                custom_name="viewtest-nemo",
                quantity=3,
            )
            async_session.add(fish)
            await async_session.commit()

            response = await authed_admin_client.get("/admin/fish/list")

            assert response.status_code == 200
            assert "viewtest-nemo" in response.text
        finally:
            await _cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestFamilyInviteAdminUI:
    """Integration tests for FamilyInvite read-only view."""

    async def test_family_invite_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/family-invite/list should render without errors."""
        response = await authed_admin_client.get("/admin/family-invite/list")

        assert response.status_code == 200


# ─── Unit tests: task 17.5 ModelView configuration ───────────────────


class TestFeedingScheduleAdminConfig:
    """Verify FeedingScheduleAdmin attributes — read + edit only."""

    def test_can_create_is_false(self):
        assert FeedingScheduleAdmin.can_create is False

    def test_can_edit_is_true(self):
        assert FeedingScheduleAdmin.can_edit is True

    def test_can_delete_is_false(self):
        assert FeedingScheduleAdmin.can_delete is False

    def test_icon(self):
        assert FeedingScheduleAdmin.icon == "fa-solid fa-clock"


class TestFeedingLogAdminConfig:
    """Verify FeedingLogAdmin attributes — read-only."""

    def test_read_only(self):
        assert FeedingLogAdmin.can_create is False
        assert FeedingLogAdmin.can_edit is False
        assert FeedingLogAdmin.can_delete is False

    def test_icon(self):
        assert FeedingLogAdmin.icon == "fa-solid fa-list-check"


class TestStreakAdminConfig:
    """Verify StreakAdmin attributes — read + edit only."""

    def test_can_create_is_false(self):
        assert StreakAdmin.can_create is False

    def test_can_edit_is_true(self):
        assert StreakAdmin.can_edit is True

    def test_can_delete_is_false(self):
        assert StreakAdmin.can_delete is False

    def test_icon(self):
        assert StreakAdmin.icon == "fa-solid fa-fire"


class TestAchievementAdminConfig:
    """Verify AchievementAdmin attributes — read-only."""

    def test_read_only(self):
        assert AchievementAdmin.can_create is False
        assert AchievementAdmin.can_edit is False
        assert AchievementAdmin.can_delete is False

    def test_searchable_columns(self):
        searchable = [c.key if hasattr(c, "key") else str(c) for c in AchievementAdmin.column_searchable_list]
        assert "achievement_type" in searchable

    def test_icon(self):
        assert AchievementAdmin.icon == "fa-solid fa-trophy"


class TestUserProgressAdminConfig:
    """Verify UserProgressAdmin attributes — read + edit only."""

    def test_can_create_is_false(self):
        assert UserProgressAdmin.can_create is False

    def test_can_edit_is_true(self):
        assert UserProgressAdmin.can_edit is True

    def test_can_delete_is_false(self):
        assert UserProgressAdmin.can_delete is False

    def test_icon(self):
        assert UserProgressAdmin.icon == "fa-solid fa-chart-line"


class TestAIScanAdminConfig:
    """Verify AIScanAdmin attributes — read-only."""

    def test_read_only(self):
        assert AIScanAdmin.can_create is False
        assert AIScanAdmin.can_edit is False
        assert AIScanAdmin.can_delete is False

    def test_icon(self):
        assert AIScanAdmin.icon == "fa-solid fa-robot"


class TestPushTokenAdminConfig:
    """Verify PushTokenAdmin attributes — read-only."""

    def test_read_only(self):
        assert PushTokenAdmin.can_create is False
        assert PushTokenAdmin.can_edit is False
        assert PushTokenAdmin.can_delete is False

    def test_icon(self):
        assert PushTokenAdmin.icon == "fa-solid fa-bell"


class TestNotificationPreferenceAdminConfig:
    """Verify NotificationPreferenceAdmin attributes — read-only."""

    def test_read_only(self):
        assert NotificationPreferenceAdmin.can_create is False
        assert NotificationPreferenceAdmin.can_edit is False
        assert NotificationPreferenceAdmin.can_delete is False

    def test_icon(self):
        assert NotificationPreferenceAdmin.icon == "fa-solid fa-sliders"


class TestAnalyticsEventAdminConfig:
    """Verify AnalyticsEventAdmin attributes — read-only."""

    def test_read_only(self):
        assert AnalyticsEventAdmin.can_create is False
        assert AnalyticsEventAdmin.can_edit is False
        assert AnalyticsEventAdmin.can_delete is False

    def test_searchable_columns(self):
        searchable = [c.key if hasattr(c, "key") else str(c) for c in AnalyticsEventAdmin.column_searchable_list]
        assert "event_type" in searchable

    def test_icon(self):
        assert AnalyticsEventAdmin.icon == "fa-solid fa-chart-bar"


class TestWebhookTransactionAdminConfig:
    """Verify WebhookTransactionAdmin attributes — read-only."""

    def test_read_only(self):
        assert WebhookTransactionAdmin.can_create is False
        assert WebhookTransactionAdmin.can_edit is False
        assert WebhookTransactionAdmin.can_delete is False

    def test_searchable_columns(self):
        searchable = [c.key if hasattr(c, "key") else str(c) for c in WebhookTransactionAdmin.column_searchable_list]
        assert "transaction_id" in searchable
        assert "event_type" in searchable

    def test_icon(self):
        assert WebhookTransactionAdmin.icon == "fa-solid fa-credit-card"


# ─── Integration tests: task 17.5 admin UI pages ─────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestFeedingAdminUI:
    """Integration tests for Feeding admin views."""

    async def test_feeding_schedule_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/feeding-schedule/list should render without errors."""
        response = await authed_admin_client.get("/admin/feeding-schedule/list")
        assert response.status_code == 200

    async def test_feeding_log_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/feeding-log/list should render without errors."""
        response = await authed_admin_client.get("/admin/feeding-log/list")
        assert response.status_code == 200


@pytest.mark.asyncio(loop_scope="session")
class TestGamificationAdminUI:
    """Integration tests for Gamification admin views."""

    async def test_streak_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/streak/list should render without errors."""
        response = await authed_admin_client.get("/admin/streak/list")
        assert response.status_code == 200

    async def test_achievement_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/achievement/list should render without errors."""
        response = await authed_admin_client.get("/admin/achievement/list")
        assert response.status_code == 200

    async def test_user_progress_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/user-progress/list should render without errors."""
        response = await authed_admin_client.get("/admin/user-progress/list")
        assert response.status_code == 200


@pytest.mark.asyncio(loop_scope="session")
class TestAIScanAdminUI:
    """Integration tests for AIScan admin view."""

    async def test_ai_scan_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/ai-scan/list should render without errors."""
        response = await authed_admin_client.get("/admin/ai-scan/list")
        assert response.status_code == 200


@pytest.mark.asyncio(loop_scope="session")
class TestNotificationAdminUI:
    """Integration tests for Notification admin views."""

    async def test_push_token_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/push-token/list should render without errors."""
        response = await authed_admin_client.get("/admin/push-token/list")
        assert response.status_code == 200

    async def test_notification_preference_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/notification-preference/list should render without errors."""
        response = await authed_admin_client.get("/admin/notification-preference/list")
        assert response.status_code == 200


@pytest.mark.asyncio(loop_scope="session")
class TestAnalyticsEventAdminUI:
    """Integration tests for AnalyticsEvent admin view."""

    async def test_analytics_event_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/analytics-event/list should render without errors."""
        response = await authed_admin_client.get("/admin/analytics-event/list")
        assert response.status_code == 200


@pytest.mark.asyncio(loop_scope="session")
class TestWebhookTransactionAdminUI:
    """Integration tests for WebhookTransaction admin view."""

    async def test_webhook_transaction_list_renders(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/webhook-transaction/list should render without errors."""
        response = await authed_admin_client.get("/admin/webhook-transaction/list")
        assert response.status_code == 200


@pytest.mark.asyncio(loop_scope="session")
class TestAllViewsRegistered:
    """Verify all 16 models are registered in the admin panel."""

    async def test_sidebar_contains_all_16_models(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """Admin sidebar should list all 16 model views."""
        response = await authed_admin_client.get("/admin/")
        assert response.status_code == 200

        expected_names = [
            "Users",
            "Aquariums",
            "Aquarium Members",
            "Family Invites",
            "Species",
            "Fish",
            "Feeding Schedules",
            "Feeding Logs",
            "Streaks",
            "Achievements",
            "User Progress",
            "AI Scans",
            "Push Tokens",
            "Notification Preferences",
            "Analytics Events",
            "Webhook Transactions",
        ]
        for name in expected_names:
            assert name in response.text, f"'{name}' not found in admin sidebar"
