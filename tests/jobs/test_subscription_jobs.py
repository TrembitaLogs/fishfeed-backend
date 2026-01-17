"""Tests for subscription background jobs."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.fish import Fish
from app.models.species import Species
from app.models.user import User
from app.schemas.purchase import FREE_USER_LIMITS


async def cleanup_subscription_data(session: AsyncSession) -> None:
    """Helper to cleanup subscription-related data."""
    await session.execute(text("DELETE FROM notification_logs"))
    await session.execute(text("DELETE FROM push_tokens"))
    await session.execute(text("DELETE FROM fish"))
    await session.execute(text("DELETE FROM aquarium_members"))
    await session.execute(text("DELETE FROM aquariums"))
    await session.execute(text("DELETE FROM users"))
    await session.execute(text("DELETE FROM species WHERE id = 'test-guppy'"))
    await session.commit()


async def create_test_user(
    session: AsyncSession,
    email: str | None = None,
    subscription_status: str = "free",
    subscription_expires_at: datetime | None = None,
) -> User:
    """Helper to create a test user."""
    user = User(
        email=email or f"test-{uuid.uuid4()}@example.com",
        password_hash="hashed_password",
        subscription_status=subscription_status,
        subscription_expires_at=subscription_expires_at,
        free_ai_scans_remaining=5,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def create_test_aquarium(
    session: AsyncSession,
    owner: User,
    name: str = "Test Aquarium",
) -> Aquarium:
    """Helper to create a test aquarium with owner as member."""
    aquarium = Aquarium(
        owner_id=owner.id,
        name=name,
    )
    session.add(aquarium)
    await session.flush()

    member = AquariumMember(
        aquarium_id=aquarium.id,
        user_id=owner.id,
        role="owner",
    )
    session.add(member)
    await session.commit()
    await session.refresh(aquarium)
    return aquarium


async def ensure_test_species(session: AsyncSession) -> Species:
    """Helper to ensure the test species exists."""
    from sqlalchemy import select

    stmt = select(Species).where(Species.id == "test-guppy")
    result = await session.execute(stmt)
    species = result.scalar_one_or_none()

    if species is None:
        species = Species(
            id="test-guppy",
            common_name="Test Guppy",
            scientific_name="Poecilia reticulata",
            food_types=["flakes", "pellets"],
            feeding_frequency=2,
            care_level="beginner",
            water_type="freshwater",
        )
        session.add(species)
        await session.commit()
        await session.refresh(species)

    return species


async def create_test_fish(
    session: AsyncSession,
    aquarium: Aquarium,
    custom_name: str = "Test Fish",
) -> Fish:
    """Helper to create a test fish."""
    await ensure_test_species(session)

    fish = Fish(
        aquarium_id=aquarium.id,
        custom_name=custom_name,
        species_id="test-guppy",
    )
    session.add(fish)
    await session.commit()
    await session.refresh(fish)
    return fish


async def add_family_member(
    session: AsyncSession,
    aquarium: Aquarium,
    user: User,
) -> AquariumMember:
    """Helper to add a family member to an aquarium."""
    member = AquariumMember(
        aquarium_id=aquarium.id,
        user_id=user.id,
        role="member",
    )
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member


# check_expired_subscriptions_job tests


@pytest.mark.asyncio(loop_scope="session")
async def test_check_expired_subscriptions_finds_expired_users(
    async_session: AsyncSession,
):
    """Test that the job finds and processes expired premium users."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import check_expired_subscriptions_job

        now = datetime.now(UTC)
        expired_at = now - timedelta(hours=1)

        # Create expired premium user
        expired_user = await create_test_user(
            async_session,
            email="expired@example.com",
            subscription_status="premium",
            subscription_expires_at=expired_at,
        )

        # Create active premium user (not expired)
        active_user = await create_test_user(
            async_session,
            email="active@example.com",
            subscription_status="premium",
            subscription_expires_at=now + timedelta(days=30),
        )

        # Create free user (should be ignored)
        free_user = await create_test_user(
            async_session,
            email="free@example.com",
            subscription_status="free",
        )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.subscription_jobs.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            with patch(
                "app.jobs.subscription_jobs.NotificationService"
            ) as mock_service_class:
                mock_service = AsyncMock()
                mock_service.send_push = AsyncMock(return_value=True)
                mock_service_class.return_value = mock_service

                count = await check_expired_subscriptions_job()

        assert count == 1

        # Refresh and verify the expired user was reverted
        await async_session.refresh(expired_user)
        assert expired_user.subscription_status == "free"
        assert expired_user.subscription_expires_at is None

        # Active user should still be premium
        await async_session.refresh(active_user)
        assert active_user.subscription_status == "premium"

    finally:
        await cleanup_subscription_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_expired_subscriptions_no_expired_users(
    async_session: AsyncSession,
):
    """Test that the job handles no expired users gracefully."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import check_expired_subscriptions_job

        now = datetime.now(UTC)

        # Create only active premium user
        await create_test_user(
            async_session,
            email="active@example.com",
            subscription_status="premium",
            subscription_expires_at=now + timedelta(days=30),
        )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.subscription_jobs.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            count = await check_expired_subscriptions_job()

        assert count == 0

    finally:
        await cleanup_subscription_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_expired_subscriptions_sends_notification(
    async_session: AsyncSession,
):
    """Test that the job sends push notification on subscription expiry."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import check_expired_subscriptions_job

        now = datetime.now(UTC)
        expired_at = now - timedelta(hours=1)

        await create_test_user(
            async_session,
            email="expired@example.com",
            subscription_status="premium",
            subscription_expires_at=expired_at,
        )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.subscription_jobs.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            with patch(
                "app.jobs.subscription_jobs.NotificationService"
            ) as mock_service_class:
                mock_service = AsyncMock()
                mock_service.send_push = AsyncMock(return_value=True)
                mock_service_class.return_value = mock_service

                await check_expired_subscriptions_job()

        mock_service.send_push.assert_called_once()
        call_kwargs = mock_service.send_push.call_args.kwargs
        assert "Premium subscription expired" in call_kwargs["title"]
        assert call_kwargs["bypass_throttle"] is True

    finally:
        await cleanup_subscription_data(async_session)


# apply_free_tier_limits tests


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_free_tier_limits_resets_ai_scans(async_session: AsyncSession):
    """Test that apply_free_tier_limits resets AI scans to free tier limit."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import apply_free_tier_limits

        user = await create_test_user(
            async_session,
            subscription_status="free",
        )
        user.free_ai_scans_remaining = 100  # Premium had unlimited
        await async_session.commit()

        result = await apply_free_tier_limits(async_session, user.id)

        await async_session.refresh(user)
        assert user.free_ai_scans_remaining == FREE_USER_LIMITS.ai_scans_per_month
        assert result["ai_scans_reset"] == FREE_USER_LIMITS.ai_scans_per_month

    finally:
        await cleanup_subscription_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_free_tier_limits_records_excess_aquariums(
    async_session: AsyncSession,
):
    """Test that apply_free_tier_limits records excess aquariums without deleting."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import apply_free_tier_limits

        user = await create_test_user(async_session)

        # Create more aquariums than free tier allows
        for i in range(FREE_USER_LIMITS.max_aquariums + 2):
            await create_test_aquarium(async_session, user, f"Aquarium {i}")

        result = await apply_free_tier_limits(async_session, user.id)

        await async_session.refresh(user)

        # Aquariums should not be deleted
        from sqlalchemy import func, select

        from app.models.aquarium import Aquarium

        stmt = (
            select(func.count())
            .select_from(Aquarium)
            .where(Aquarium.owner_id == user.id)
            .where(Aquarium.deleted_at.is_(None))
        )
        result_count = await async_session.execute(stmt)
        aquarium_count = result_count.scalar_one()
        assert aquarium_count == FREE_USER_LIMITS.max_aquariums + 2

        # But limits exceeded should be recorded
        assert "aquariums" in result["limits_exceeded"]
        assert result["limits_exceeded"]["aquariums"]["current"] == FREE_USER_LIMITS.max_aquariums + 2
        assert result["limits_exceeded"]["aquariums"]["limit"] == FREE_USER_LIMITS.max_aquariums

        # Verify it's saved in user settings
        assert "limits_exceeded" in user.settings
        assert "aquariums" in user.settings["limits_exceeded"]

    finally:
        await cleanup_subscription_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_free_tier_limits_records_excess_fish(async_session: AsyncSession):
    """Test that apply_free_tier_limits records excess fish without deleting."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import apply_free_tier_limits

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create more fish than free tier allows
        for i in range(FREE_USER_LIMITS.max_fish_per_aquarium + 5):
            await create_test_fish(async_session, aquarium, f"Fish {i}")

        result = await apply_free_tier_limits(async_session, user.id)

        await async_session.refresh(user)

        # Fish should not be deleted
        from sqlalchemy import func, select

        stmt = (
            select(func.count())
            .select_from(Fish)
            .where(Fish.aquarium_id == aquarium.id)
            .where(Fish.deleted_at.is_(None))
        )
        result_count = await async_session.execute(stmt)
        fish_count = result_count.scalar_one()
        assert fish_count == FREE_USER_LIMITS.max_fish_per_aquarium + 5

        # But limits exceeded should be recorded
        assert "fish_per_aquarium" in result["limits_exceeded"]

    finally:
        await cleanup_subscription_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_free_tier_limits_no_excess(async_session: AsyncSession):
    """Test that apply_free_tier_limits works when user is within limits."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import apply_free_tier_limits

        user = await create_test_user(async_session)

        # Create only 1 aquarium (within free tier limit of 2)
        await create_test_aquarium(async_session, user)

        result = await apply_free_tier_limits(async_session, user.id)

        await async_session.refresh(user)

        # No limits exceeded
        assert result["limits_exceeded"] == {}
        assert "limits_exceeded" not in user.settings

    finally:
        await cleanup_subscription_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_free_tier_limits_user_not_found(async_session: AsyncSession):
    """Test that apply_free_tier_limits handles non-existent user."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import apply_free_tier_limits

        fake_user_id = uuid.uuid4()
        result = await apply_free_tier_limits(async_session, fake_user_id)

        assert result == {"error": "user_not_found"}

    finally:
        await cleanup_subscription_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_free_tier_limits_records_family_members(
    async_session: AsyncSession,
):
    """Test that apply_free_tier_limits records family member info."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import apply_free_tier_limits

        owner = await create_test_user(async_session, email="owner@example.com")
        aquarium = await create_test_aquarium(async_session, owner)

        # Add family members
        member1 = await create_test_user(async_session, email="member1@example.com")
        member2 = await create_test_user(async_session, email="member2@example.com")
        await add_family_member(async_session, aquarium, member1)
        await add_family_member(async_session, aquarium, member2)

        result = await apply_free_tier_limits(async_session, owner.id)

        # Family members info should be recorded
        assert "family_members" in result["limits_exceeded"]
        family_info = result["limits_exceeded"]["family_members"]["aquariums"]
        assert len(family_info) == 1
        # 3 members: owner + 2 family members
        assert family_info[0]["member_count"] == 3

    finally:
        await cleanup_subscription_data(async_session)


# clear_limits_exceeded tests


@pytest.mark.asyncio(loop_scope="session")
async def test_clear_limits_exceeded(async_session: AsyncSession):
    """Test that clear_limits_exceeded removes downgrade info."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import clear_limits_exceeded

        user = await create_test_user(async_session)

        # Set some limits exceeded info
        user.settings = {
            "limits_exceeded": {"aquariums": {"current": 5, "limit": 2}},
            "downgraded_at": "2024-01-01T00:00:00",
            "other_setting": "value",
        }
        await async_session.commit()

        await clear_limits_exceeded(async_session, user.id)

        await async_session.refresh(user)
        assert "limits_exceeded" not in user.settings
        assert "downgraded_at" not in user.settings
        assert user.settings["other_setting"] == "value"

    finally:
        await cleanup_subscription_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_clear_limits_exceeded_no_limits_set(async_session: AsyncSession):
    """Test that clear_limits_exceeded works when no limits were set."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import clear_limits_exceeded

        user = await create_test_user(async_session)
        user.settings = {"other_setting": "value"}
        await async_session.commit()

        # Should not raise an error
        await clear_limits_exceeded(async_session, user.id)

        await async_session.refresh(user)
        assert user.settings == {"other_setting": "value"}

    finally:
        await cleanup_subscription_data(async_session)


# _send_subscription_expired_notification tests


@pytest.mark.asyncio(loop_scope="session")
async def test_send_subscription_expired_notification_success(
    async_session: AsyncSession,
):
    """Test sending subscription expired notification successfully."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import _send_subscription_expired_notification

        user = await create_test_user(async_session)

        with patch(
            "app.jobs.subscription_jobs.NotificationService"
        ) as mock_service_class:
            mock_service = AsyncMock()
            mock_service.send_push = AsyncMock(return_value=True)
            mock_service_class.return_value = mock_service

            result = await _send_subscription_expired_notification(
                async_session, user.id
            )

        assert result is True
        mock_service.send_push.assert_called_once()
        call_kwargs = mock_service.send_push.call_args.kwargs
        assert call_kwargs["user_id"] == user.id
        assert "subscription" in call_kwargs["title"].lower()
        assert call_kwargs["data"]["type"] == "subscription_expired"
        assert call_kwargs["bypass_throttle"] is True

    finally:
        await cleanup_subscription_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_send_subscription_expired_notification_handles_error(
    async_session: AsyncSession,
):
    """Test that notification sending handles errors gracefully."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import _send_subscription_expired_notification

        user = await create_test_user(async_session)

        with patch(
            "app.jobs.subscription_jobs.NotificationService"
        ) as mock_service_class:
            mock_service = AsyncMock()
            mock_service.send_push = AsyncMock(side_effect=Exception("Network error"))
            mock_service_class.return_value = mock_service

            result = await _send_subscription_expired_notification(
                async_session, user.id
            )

        assert result is False

    finally:
        await cleanup_subscription_data(async_session)


# Batch processing tests


@pytest.mark.asyncio(loop_scope="session")
async def test_check_expired_subscriptions_batch_processing(
    async_session: AsyncSession,
):
    """Test that the job processes users in batches."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import check_expired_subscriptions_job

        now = datetime.now(UTC)
        expired_at = now - timedelta(hours=1)

        # Create multiple expired users
        for i in range(5):
            await create_test_user(
                async_session,
                email=f"expired{i}@example.com",
                subscription_status="premium",
                subscription_expires_at=expired_at,
            )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.subscription_jobs.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            with patch(
                "app.jobs.subscription_jobs.NotificationService"
            ) as mock_service_class:
                mock_service = AsyncMock()
                mock_service.send_push = AsyncMock(return_value=True)
                mock_service_class.return_value = mock_service

                count = await check_expired_subscriptions_job()

        assert count == 5
        assert mock_service.send_push.call_count == 5

    finally:
        await cleanup_subscription_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_expired_subscriptions_continues_on_error(
    async_session: AsyncSession,
):
    """Test that the job continues processing even if one user fails."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import check_expired_subscriptions_job

        now = datetime.now(UTC)
        expired_at = now - timedelta(hours=1)

        # Create multiple expired users
        for i in range(3):
            await create_test_user(
                async_session,
                email=f"expired{i}@example.com",
                subscription_status="premium",
                subscription_expires_at=expired_at,
            )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        call_count = 0

        async def mock_send(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("Network error")
            return True

        with patch(
            "app.jobs.subscription_jobs.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            with patch(
                "app.jobs.subscription_jobs.NotificationService"
            ) as mock_service_class:
                mock_service = AsyncMock()
                mock_service.send_push = mock_send
                mock_service_class.return_value = mock_service

                count = await check_expired_subscriptions_job()

        # Should have processed all 3, even though one notification failed
        assert count == 3

    finally:
        await cleanup_subscription_data(async_session)


# Integration with purchase service tests


@pytest.mark.asyncio(loop_scope="session")
async def test_upgrade_clears_limits_exceeded(async_session: AsyncSession):
    """Test that upgrading to premium clears limits_exceeded from settings."""
    await cleanup_subscription_data(async_session)
    try:
        from app.services.purchase import _clear_downgrade_info

        user = await create_test_user(async_session)

        # Set limits exceeded info (simulating previous downgrade)
        user.settings = {
            "limits_exceeded": {"aquariums": {"current": 5, "limit": 2}},
            "downgraded_at": "2024-01-01T00:00:00",
        }
        await async_session.commit()

        await _clear_downgrade_info(async_session, user.id)

        await async_session.refresh(user)
        assert "limits_exceeded" not in user.settings
        assert "downgraded_at" not in user.settings

    finally:
        await cleanup_subscription_data(async_session)
