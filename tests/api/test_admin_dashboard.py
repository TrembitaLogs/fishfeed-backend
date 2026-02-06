"""Tests for GET /admin/dashboard endpoint."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIScan
from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingLog, FeedingSchedule
from app.models.fish import Fish
from app.models.gamification import Achievement, Streak
from app.models.user import User
from app.utils.jwt import create_access_token
from app.utils.password import hash_password


async def _cleanup(session: AsyncSession) -> None:
    """Truncate all relevant tables in dependency order."""
    await session.execute(text("TRUNCATE TABLE feeding_logs CASCADE"))
    await session.execute(text("TRUNCATE TABLE feeding_schedules CASCADE"))
    await session.execute(text("TRUNCATE TABLE ai_scans CASCADE"))
    await session.execute(text("TRUNCATE TABLE achievements CASCADE"))
    await session.execute(text("TRUNCATE TABLE streaks CASCADE"))
    await session.execute(text("TRUNCATE TABLE aquarium_members CASCADE"))
    await session.execute(text("TRUNCATE TABLE fish CASCADE"))
    await session.execute(text("TRUNCATE TABLE aquariums CASCADE"))
    await session.execute(text("TRUNCATE TABLE users CASCADE"))
    await session.commit()


async def _create_user(
    session: AsyncSession,
    *,
    email: str,
    is_admin: bool = False,
) -> tuple[User, str]:
    """Create a test user and return (user, access_token)."""
    user = User(
        email=email,
        password_hash=hash_password("TestPass123"),
        is_admin=is_admin,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    token = create_access_token(str(user.id))
    return user, token


DASHBOARD_URL = "/api/v1/admin/dashboard"


@pytest.mark.asyncio(loop_scope="session")
class TestDashboardRequiresAdmin:
    """Test that GET /admin/dashboard enforces authentication and admin role."""

    async def test_dashboard_returns_401_without_token(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Request without Authorization header should get 401."""
        response = await client.get(DASHBOARD_URL)
        assert response.status_code == 401

    async def test_dashboard_returns_403_for_non_admin(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Non-admin user should get 403."""
        await _cleanup(async_session)
        try:
            _, token = await _create_user(
                async_session, email="regular@dashboard-test.com", is_admin=False
            )
            response = await client.get(
                DASHBOARD_URL,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 403
            assert "Admin privileges required" in response.json()["detail"]
        finally:
            await _cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestDashboardReturnsStats:
    """Test that GET /admin/dashboard returns correct DashboardResponse."""

    async def test_dashboard_returns_stats_empty_db(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Dashboard should return zeroed stats when no data exists."""
        await _cleanup(async_session)
        try:
            _, token = await _create_user(
                async_session, email="admin@dashboard-test.com", is_admin=True
            )
            response = await client.get(
                DASHBOARD_URL,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            data = response.json()

            # Verify response structure matches DashboardResponse schema
            assert "users" in data
            assert "aquariums" in data
            assert "feeding" in data
            assert "ai_scans" in data
            assert "gamification" in data

            # Admin user we just created counts as 1
            assert data["users"]["total"] == 1
            assert data["users"]["active_last_7d"] == 0
            assert data["users"]["premium"] == 0

            assert data["aquariums"]["total"] == 0
            assert data["aquariums"]["with_family_members"] == 0

            assert data["feeding"]["logs_today"] == 0
            assert data["feeding"]["schedules_active"] == 0

            assert data["ai_scans"]["total"] == 0
            assert data["ai_scans"]["today"] == 0

            assert data["gamification"]["avg_streak"] == 0.0
            assert data["gamification"]["max_streak"] == 0
            assert data["gamification"]["achievements_unlocked_today"] == 0
        finally:
            await _cleanup(async_session)

    async def test_dashboard_returns_stats_with_data(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Dashboard should return correct aggregated stats when data exists."""
        await _cleanup(async_session)
        try:
            # Create admin user
            admin_user, token = await _create_user(
                async_session, email="admin@dashboard-test.com", is_admin=True
            )

            # Create a regular premium user
            premium_user = User(
                email="premium@dashboard-test.com",
                password_hash=hash_password("TestPass123"),
                subscription_status="premium",
            )
            async_session.add(premium_user)
            await async_session.commit()
            await async_session.refresh(premium_user)

            # Create aquarium with a family member
            aquarium = Aquarium(owner_id=admin_user.id, name="Test Aquarium")
            async_session.add(aquarium)
            await async_session.flush()
            await async_session.refresh(aquarium)

            member = AquariumMember(
                aquarium_id=aquarium.id, user_id=premium_user.id, role="member"
            )
            async_session.add(member)
            await async_session.flush()

            # Create fish and active schedule
            fish = Fish(
                aquarium_id=aquarium.id,
                species_id="test-guppy",
                custom_name="Dashboard Fish",
                quantity=1,
                added_via="manual",
            )
            async_session.add(fish)
            await async_session.flush()
            await async_session.refresh(fish)

            schedule = FeedingSchedule(
                aquarium_id=aquarium.id,
                fish_id=fish.id,
                food_type="flakes",
                active=True,
            )
            async_session.add(schedule)
            await async_session.flush()
            await async_session.refresh(schedule)

            # Create feeding log (today)
            now = datetime.now(UTC)
            device_id = uuid4()
            log = FeedingLog(
                schedule_id=schedule.id,
                fish_id=fish.id,
                aquarium_id=aquarium.id,
                scheduled_for=now.replace(tzinfo=None),
                action="fed",
                acted_at=now,
                acted_by_user_id=admin_user.id,
                device_id=device_id,
            )
            async_session.add(log)
            await async_session.flush()

            # Create AI scan
            scan = AIScan(user_id=admin_user.id, created_at=now)
            async_session.add(scan)
            await async_session.flush()

            # Create streak and achievement
            streak = Streak(user_id=admin_user.id, current_streak=5, best_streak=10)
            async_session.add(streak)
            await async_session.flush()

            achievement = Achievement(
                user_id=admin_user.id, achievement_type="first_feed", unlocked_at=now
            )
            async_session.add(achievement)
            await async_session.commit()

            # Execute
            response = await client.get(
                DASHBOARD_URL,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            data = response.json()

            # 2 users (admin + premium)
            assert data["users"]["total"] == 2
            assert data["users"]["premium"] == 1
            # admin_user has a feeding log today -> active
            assert data["users"]["active_last_7d"] >= 1

            assert data["aquariums"]["total"] == 1
            assert data["aquariums"]["with_family_members"] == 1

            assert data["feeding"]["logs_today"] >= 1
            assert data["feeding"]["schedules_active"] == 1

            assert data["ai_scans"]["total"] >= 1
            assert data["ai_scans"]["today"] >= 1

            assert data["gamification"]["avg_streak"] == 5.0
            assert data["gamification"]["max_streak"] == 5
            assert data["gamification"]["achievements_unlocked_today"] >= 1
        finally:
            await _cleanup(async_session)
