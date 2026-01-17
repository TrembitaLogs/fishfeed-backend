"""Tests for notification background jobs."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingEvent, FeedingSchedule
from app.models.user import User


async def cleanup_notification_data(session: AsyncSession) -> None:
    """Helper to cleanup notification-related data."""
    await session.execute(text("DELETE FROM notification_logs"))
    await session.execute(text("DELETE FROM push_tokens"))
    await session.execute(text("DELETE FROM notification_preferences"))
    await session.execute(text("DELETE FROM feeding_events"))
    await session.execute(text("DELETE FROM feeding_schedules"))
    await session.execute(text("DELETE FROM aquarium_members"))
    await session.execute(text("DELETE FROM aquariums"))
    await session.execute(text("DELETE FROM users"))
    await session.commit()


async def create_test_user(
    session: AsyncSession,
    email: str | None = None,
    nickname: str | None = None,
) -> User:
    """Helper to create a test user."""
    user = User(
        email=email or f"test-{uuid.uuid4()}@example.com",
        password_hash="hashed_password",
        nickname=nickname,
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


async def create_feeding_event(
    session: AsyncSession,
    aquarium: Aquarium,
    schedule: FeedingSchedule,
    scheduled_at: datetime,
    status: str = "pending",
    completed_by: uuid.UUID | None = None,
    completed_at: datetime | None = None,
) -> FeedingEvent:
    """Helper to create a feeding event."""
    event = FeedingEvent(
        aquarium_id=aquarium.id,
        schedule_id=schedule.id,
        scheduled_at=scheduled_at,
        status=status,
        completed_by=completed_by,
        completed_at=completed_at,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


# _build_summary_message tests


def test_build_summary_message_perfect_week():
    """Test summary message for 100% completion rate."""
    from app.jobs.notification_jobs import _build_summary_message

    stats = {"completed": 14, "missed": 0, "total_events": 14}
    message = _build_summary_message(stats)

    assert "Perfect week" in message
    assert "14" in message


def test_build_summary_message_great_week():
    """Test summary message for >80% completion rate."""
    from app.jobs.notification_jobs import _build_summary_message

    stats = {"completed": 12, "missed": 2, "total_events": 14}
    message = _build_summary_message(stats)

    assert "Great job" in message
    assert "12/14" in message
    assert "86%" in message


def test_build_summary_message_good_effort():
    """Test summary message for 50-80% completion rate."""
    from app.jobs.notification_jobs import _build_summary_message

    stats = {"completed": 8, "missed": 6, "total_events": 14}
    message = _build_summary_message(stats)

    assert "Good effort" in message
    assert "8/14" in message
    assert "6 missed" in message


def test_build_summary_message_needs_attention():
    """Test summary message for <50% completion rate."""
    from app.jobs.notification_jobs import _build_summary_message

    stats = {"completed": 4, "missed": 10, "total_events": 14}
    message = _build_summary_message(stats)

    assert "need attention" in message
    assert "4/14" in message


def test_build_summary_message_no_events():
    """Test summary message when no events."""
    from app.jobs.notification_jobs import _build_summary_message

    stats = {"completed": 0, "missed": 0, "total_events": 0}
    message = _build_summary_message(stats)

    assert "No feeding events" in message


# _get_user_weekly_stats tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_user_weekly_stats_with_events(async_session: AsyncSession):
    """Test getting weekly stats for user with events."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import _get_user_weekly_stats

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=2,
            scheduled_times=["08:00", "20:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        now = datetime.now(UTC)
        week_start = now - timedelta(days=7)

        # Create 5 completed and 2 missed events in the past week
        for i in range(5):
            await create_feeding_event(
                async_session,
                aquarium,
                schedule,
                scheduled_at=week_start + timedelta(days=i, hours=8),
                status="completed",
                completed_by=user.id,
                completed_at=week_start + timedelta(days=i, hours=8, minutes=5),
            )

        for i in range(2):
            await create_feeding_event(
                async_session,
                aquarium,
                schedule,
                scheduled_at=week_start + timedelta(days=i, hours=20),
                status="missed",
            )

        stats = await _get_user_weekly_stats(async_session, user.id, week_start, now)

        assert stats["completed"] == 5
        assert stats["missed"] == 2
        assert stats["total_events"] == 7
    finally:
        await cleanup_notification_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_user_weekly_stats_no_aquariums(async_session: AsyncSession):
    """Test getting weekly stats for user with no aquariums."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import _get_user_weekly_stats

        user = await create_test_user(async_session)

        now = datetime.now(UTC)
        week_start = now - timedelta(days=7)

        stats = await _get_user_weekly_stats(async_session, user.id, week_start, now)

        assert stats["completed"] == 0
        assert stats["missed"] == 0
        assert stats["total_events"] == 0
    finally:
        await cleanup_notification_data(async_session)


# _find_inactive_users tests


@pytest.mark.asyncio(loop_scope="session")
async def test_find_inactive_users_finds_inactive(async_session: AsyncSession):
    """Test finding users who haven't completed feedings."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import _find_inactive_users

        # Create active user (completed feeding recently)
        active_user = await create_test_user(async_session, "active@example.com")
        active_aquarium = await create_test_aquarium(async_session, active_user)

        schedule = FeedingSchedule(
            aquarium_id=active_aquarium.id,
            times_per_day=1,
            scheduled_times=["08:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        # Create recent feeding for active user
        now = datetime.now(UTC)
        await create_feeding_event(
            async_session,
            active_aquarium,
            schedule,
            scheduled_at=now - timedelta(hours=1),
            status="completed",
            completed_by=active_user.id,
            completed_at=now - timedelta(hours=1),
        )

        # Create inactive user (no recent feedings)
        inactive_user = await create_test_user(async_session, "inactive@example.com")
        inactive_aquarium = await create_test_aquarium(async_session, inactive_user)

        # Cutoff is 3 days ago
        cutoff = now - timedelta(days=3)

        inactive_users = await _find_inactive_users(async_session, cutoff)

        assert inactive_user.id in inactive_users
        assert active_user.id not in inactive_users
    finally:
        await cleanup_notification_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_find_inactive_users_no_inactive(async_session: AsyncSession):
    """Test when all users are active."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import _find_inactive_users

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=1,
            scheduled_times=["08:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        now = datetime.now(UTC)

        # Create recent feeding
        await create_feeding_event(
            async_session,
            aquarium,
            schedule,
            scheduled_at=now - timedelta(hours=1),
            status="completed",
            completed_by=user.id,
            completed_at=now - timedelta(hours=1),
        )

        cutoff = now - timedelta(days=3)
        inactive_users = await _find_inactive_users(async_session, cutoff)

        assert len(inactive_users) == 0
    finally:
        await cleanup_notification_data(async_session)


# weekly_summary_job tests


@pytest.mark.asyncio(loop_scope="session")
async def test_weekly_summary_job_sends_to_active_users(async_session: AsyncSession):
    """Test weekly summary job sends notifications to active users."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import weekly_summary_job

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=2,
            scheduled_times=["08:00", "20:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        now = datetime.now(UTC)
        week_start = now - timedelta(days=7)

        # Create some feeding events
        for i in range(3):
            await create_feeding_event(
                async_session,
                aquarium,
                schedule,
                scheduled_at=week_start + timedelta(days=i, hours=8),
                status="completed",
                completed_by=user.id,
                completed_at=week_start + timedelta(days=i, hours=8, minutes=5),
            )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.notification_jobs.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            with patch(
                "app.jobs.notification_jobs.NotificationService"
            ) as mock_service_class:
                mock_service = AsyncMock()
                mock_service.send_push = AsyncMock(return_value=True)
                mock_service_class.return_value = mock_service

                count = await weekly_summary_job()

        assert count == 1
        mock_service.send_push.assert_called_once()

        # Verify notification type
        call_kwargs = mock_service.send_push.call_args.kwargs
        assert call_kwargs["notification_type"] == "weekly_summary"
    finally:
        await cleanup_notification_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_weekly_summary_job_skips_users_without_events(
    async_session: AsyncSession,
):
    """Test weekly summary job skips users with no events in the week."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import weekly_summary_job

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # No feeding events for this user

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.notification_jobs.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            with patch(
                "app.jobs.notification_jobs.NotificationService"
            ) as mock_service_class:
                mock_service = AsyncMock()
                mock_service.send_push = AsyncMock(return_value=True)
                mock_service_class.return_value = mock_service

                count = await weekly_summary_job()

        # No notifications should be sent
        assert count == 0
        mock_service.send_push.assert_not_called()
    finally:
        await cleanup_notification_data(async_session)


# re_engagement_job tests


@pytest.mark.asyncio(loop_scope="session")
async def test_re_engagement_job_sends_to_inactive_users(async_session: AsyncSession):
    """Test re-engagement job sends notifications to inactive users."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import re_engagement_job

        # Create inactive user
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.notification_jobs.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            with patch(
                "app.jobs.notification_jobs.NotificationService"
            ) as mock_service_class:
                mock_service = AsyncMock()
                mock_service.send_push = AsyncMock(return_value=True)
                mock_service_class.return_value = mock_service

                count = await re_engagement_job()

        assert count == 1
        mock_service.send_push.assert_called_once()

        # Verify notification content
        call_kwargs = mock_service.send_push.call_args.kwargs
        assert "fish miss you" in call_kwargs["title"]
        assert call_kwargs["notification_type"] == "feeding_reminder"
    finally:
        await cleanup_notification_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_re_engagement_job_skips_active_users(async_session: AsyncSession):
    """Test re-engagement job doesn't send to active users."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import re_engagement_job

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=1,
            scheduled_times=["08:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        now = datetime.now(UTC)

        # Create recent feeding (user is active)
        await create_feeding_event(
            async_session,
            aquarium,
            schedule,
            scheduled_at=now - timedelta(hours=1),
            status="completed",
            completed_by=user.id,
            completed_at=now - timedelta(hours=1),
        )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.notification_jobs.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            with patch(
                "app.jobs.notification_jobs.NotificationService"
            ) as mock_service_class:
                mock_service = AsyncMock()
                mock_service.send_push = AsyncMock(return_value=True)
                mock_service_class.return_value = mock_service

                count = await re_engagement_job()

        # No notifications should be sent to active users
        assert count == 0
    finally:
        await cleanup_notification_data(async_session)


# family_feeding_trigger tests


@pytest.mark.asyncio(loop_scope="session")
async def test_family_feeding_trigger_sends_to_members(async_session: AsyncSession):
    """Test family feeding trigger sends to all family members except author."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import family_feeding_trigger

        # Create owner
        owner = await create_test_user(
            async_session, "owner@example.com", nickname="Owner"
        )
        aquarium = await create_test_aquarium(async_session, owner, "Family Tank")

        # Add family members
        member1 = await create_test_user(
            async_session, "member1@example.com", nickname="Member1"
        )
        member2 = await create_test_user(
            async_session, "member2@example.com", nickname="Member2"
        )
        await add_family_member(async_session, aquarium, member1)
        await add_family_member(async_session, aquarium, member2)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=2,
            scheduled_times=["08:00", "20:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        now = datetime.now(UTC)

        # Create feeding event completed by owner
        event = await create_feeding_event(
            async_session,
            aquarium,
            schedule,
            scheduled_at=now,
            status="completed",
            completed_by=owner.id,
            completed_at=now,
        )

        with patch(
            "app.jobs.notification_jobs.NotificationService"
        ) as mock_service_class:
            mock_service = AsyncMock()
            mock_service.send_push = AsyncMock(return_value=True)
            mock_service_class.return_value = mock_service

            count = await family_feeding_trigger(async_session, event.id, owner.id)

        # Should send to 2 members (not the owner)
        assert count == 2
        assert mock_service.send_push.call_count == 2

        # Verify notification content
        first_call = mock_service.send_push.call_args_list[0]
        assert "Family Tank: Fish fed!" in first_call.kwargs["title"]
        assert "Owner" in first_call.kwargs["body"]
        assert first_call.kwargs["notification_type"] == "family_update"
    finally:
        await cleanup_notification_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_family_feeding_trigger_no_other_members(async_session: AsyncSession):
    """Test family feeding trigger when no other members."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import family_feeding_trigger

        owner = await create_test_user(async_session, nickname="Solo Owner")
        aquarium = await create_test_aquarium(async_session, owner)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=1,
            scheduled_times=["08:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        now = datetime.now(UTC)

        event = await create_feeding_event(
            async_session,
            aquarium,
            schedule,
            scheduled_at=now,
            status="completed",
            completed_by=owner.id,
            completed_at=now,
        )

        with patch(
            "app.jobs.notification_jobs.NotificationService"
        ) as mock_service_class:
            mock_service = AsyncMock()
            mock_service.send_push = AsyncMock(return_value=True)
            mock_service_class.return_value = mock_service

            count = await family_feeding_trigger(async_session, event.id, owner.id)

        # No notifications (only owner, no other members)
        assert count == 0
        mock_service.send_push.assert_not_called()
    finally:
        await cleanup_notification_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_family_feeding_trigger_event_not_found(async_session: AsyncSession):
    """Test family feeding trigger with non-existent event."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import family_feeding_trigger

        user = await create_test_user(async_session)
        fake_event_id = uuid.uuid4()

        count = await family_feeding_trigger(async_session, fake_event_id, user.id)

        assert count == 0
    finally:
        await cleanup_notification_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_family_feeding_trigger_uses_user_nickname(async_session: AsyncSession):
    """Test family feeding trigger uses user nickname in notification."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import family_feeding_trigger

        owner = await create_test_user(
            async_session, "owner@example.com", nickname="FishDaddy"
        )
        aquarium = await create_test_aquarium(async_session, owner)

        member = await create_test_user(async_session, "member@example.com")
        await add_family_member(async_session, aquarium, member)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=1,
            scheduled_times=["08:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        now = datetime.now(UTC)

        event = await create_feeding_event(
            async_session,
            aquarium,
            schedule,
            scheduled_at=now,
            status="completed",
            completed_by=owner.id,
            completed_at=now,
        )

        with patch(
            "app.jobs.notification_jobs.NotificationService"
        ) as mock_service_class:
            mock_service = AsyncMock()
            mock_service.send_push = AsyncMock(return_value=True)
            mock_service_class.return_value = mock_service

            await family_feeding_trigger(async_session, event.id, owner.id)

        call_kwargs = mock_service.send_push.call_args.kwargs
        assert "FishDaddy" in call_kwargs["body"]
    finally:
        await cleanup_notification_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_family_feeding_trigger_handles_no_nickname(async_session: AsyncSession):
    """Test family feeding trigger handles user without nickname."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import family_feeding_trigger

        # Owner without nickname
        owner = await create_test_user(async_session, "owner@example.com", nickname=None)
        aquarium = await create_test_aquarium(async_session, owner)

        member = await create_test_user(async_session, "member@example.com")
        await add_family_member(async_session, aquarium, member)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=1,
            scheduled_times=["08:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        now = datetime.now(UTC)

        event = await create_feeding_event(
            async_session,
            aquarium,
            schedule,
            scheduled_at=now,
            status="completed",
            completed_by=owner.id,
            completed_at=now,
        )

        with patch(
            "app.jobs.notification_jobs.NotificationService"
        ) as mock_service_class:
            mock_service = AsyncMock()
            mock_service.send_push = AsyncMock(return_value=True)
            mock_service_class.return_value = mock_service

            await family_feeding_trigger(async_session, event.id, owner.id)

        call_kwargs = mock_service.send_push.call_args.kwargs
        # Should use "Someone" when no nickname
        assert "Someone" in call_kwargs["body"]
    finally:
        await cleanup_notification_data(async_session)


# Error handling tests


@pytest.mark.asyncio(loop_scope="session")
async def test_weekly_summary_job_handles_notification_errors(
    async_session: AsyncSession,
):
    """Test weekly summary job continues despite notification errors."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import weekly_summary_job

        # Create two users with events
        user1 = await create_test_user(async_session, "user1@example.com")
        user2 = await create_test_user(async_session, "user2@example.com")

        aquarium1 = await create_test_aquarium(async_session, user1)
        aquarium2 = await create_test_aquarium(async_session, user2)

        for aq in [aquarium1, aquarium2]:
            schedule = FeedingSchedule(
                aquarium_id=aq.id,
                times_per_day=1,
                scheduled_times=["08:00"],
                food_type="flakes",
            )
            async_session.add(schedule)
            await async_session.commit()
            await async_session.refresh(schedule)

            now = datetime.now(UTC)
            await create_feeding_event(
                async_session,
                aq,
                schedule,
                scheduled_at=now - timedelta(days=1),
                status="completed",
                completed_by=aq.owner_id,
                completed_at=now - timedelta(days=1),
            )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.notification_jobs.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            call_count = 0

            async def mock_send_push(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise Exception("Network error")
                return True

            with patch(
                "app.jobs.notification_jobs.NotificationService"
            ) as mock_service_class:
                mock_service = AsyncMock()
                mock_service.send_push = mock_send_push
                mock_service_class.return_value = mock_service

                count = await weekly_summary_job()

        # Should have succeeded for one user despite error for first
        assert count == 1
    finally:
        await cleanup_notification_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_family_feeding_trigger_handles_notification_errors(
    async_session: AsyncSession,
):
    """Test family feeding trigger continues despite notification errors."""
    await cleanup_notification_data(async_session)
    try:
        from app.jobs.notification_jobs import family_feeding_trigger

        owner = await create_test_user(async_session, "owner@example.com")
        aquarium = await create_test_aquarium(async_session, owner)

        # Add two members
        member1 = await create_test_user(async_session, "member1@example.com")
        member2 = await create_test_user(async_session, "member2@example.com")
        await add_family_member(async_session, aquarium, member1)
        await add_family_member(async_session, aquarium, member2)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=1,
            scheduled_times=["08:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        now = datetime.now(UTC)
        event = await create_feeding_event(
            async_session,
            aquarium,
            schedule,
            scheduled_at=now,
            status="completed",
            completed_by=owner.id,
            completed_at=now,
        )

        call_count = 0

        async def mock_send_push(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Network error")
            return True

        with patch(
            "app.jobs.notification_jobs.NotificationService"
        ) as mock_service_class:
            mock_service = AsyncMock()
            mock_service.send_push = mock_send_push
            mock_service_class.return_value = mock_service

            count = await family_feeding_trigger(async_session, event.id, owner.id)

        # Should have succeeded for one member despite error for first
        assert count == 1
    finally:
        await cleanup_notification_data(async_session)
