"""Tests for subscription background jobs."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.aquarium import Aquarium, AquariumMember
from app.models.fish import Fish
from app.models.notification import NotificationLog, PushToken
from app.models.species import Species
from app.models.user import User
from app.schemas.purchase import FREE_USER_LIMITS
from app.services.purchase import (
    PurchaseError,
    Reconciliation,
    RevenueCatAPIError,
    SubscriptionSnapshot,
    UserNotFoundError,
)


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


def reconciliation_for(user: User, *, outcome: str = "unchanged") -> Reconciliation:
    """Small proposal factory for worker orchestration tests."""
    return Reconciliation(
        user_id=user.id,
        before_status=user.subscription_status,
        before_expires_at=user.subscription_expires_at,
        before_verified_at=user.subscription_verified_at,
        before_subscription={},
        snapshot=SubscriptionSnapshot("free", None, None, False, False),
        verified_at=datetime.now(UTC),
        outcome=outcome,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_due_snapshot_uses_cadence_boundaries(async_session: AsyncSession):
    """Daily and hourly boundaries are strict, with the 24-hour expiry edge included."""
    from app.jobs import subscription_jobs

    await cleanup_subscription_data(async_session)
    now = datetime.now(UTC)
    daily_due = await create_test_user(async_session, email="daily-due@example.com")
    daily_due.subscription_verified_at = now - timedelta(days=1, microseconds=1)
    daily_edge = await create_test_user(async_session, email="daily-edge@example.com")
    daily_edge.subscription_verified_at = now - timedelta(days=1)
    near_due = await create_test_user(
        async_session,
        email="near-due@example.com",
        subscription_status="premium",
        subscription_expires_at=now + timedelta(hours=24),
    )
    near_due.subscription_verified_at = now - timedelta(hours=1, microseconds=1)
    near_edge = await create_test_user(
        async_session,
        email="near-edge@example.com",
        subscription_status="premium",
        subscription_expires_at=now,
    )
    near_edge.subscription_verified_at = now - timedelta(hours=1)
    far_future = await create_test_user(
        async_session,
        email="far-future@example.com",
        subscription_status="premium",
        subscription_expires_at=now + timedelta(hours=24, microseconds=1),
    )
    far_future.subscription_verified_at = now - timedelta(hours=2)
    await async_session.commit()

    with patch("app.jobs.subscription_jobs.async_session_maker", return_value=async_session):
        ids = await subscription_jobs._due_subscription_user_ids(now, ())

    assert set(ids) == {daily_due.id, near_due.id}


@pytest.mark.asyncio(loop_scope="session")
async def test_due_snapshot_orders_null_then_oldest_then_uuid_and_covers_daily_rows(async_session: AsyncSession):
    """Cadence keeps unverified/free/lifetime/far-future rows daily and orders deterministically."""
    from app.jobs import subscription_jobs

    await cleanup_subscription_data(async_session)
    now = datetime.now(UTC)
    null_low = User(id=uuid.UUID(int=1), email="null-low@example.com", password_hash="x")
    null_high = User(id=uuid.UUID(int=2), email="null-high@example.com", password_hash="x")
    daily_free = User(id=uuid.UUID(int=3), email="daily-free@example.com", password_hash="x")
    daily_free.subscription_verified_at = now - timedelta(days=1, microseconds=1)
    lifetime = User(id=uuid.UUID(int=4), email="lifetime@example.com", password_hash="x", subscription_status="premium")
    lifetime.subscription_verified_at = now - timedelta(days=1, microseconds=1)
    far = User(
        id=uuid.UUID(int=5),
        email="far@example.com",
        password_hash="x",
        subscription_status="premium",
        subscription_expires_at=now + timedelta(days=10),
    )
    far.subscription_verified_at = now - timedelta(days=1, microseconds=1)
    past = User(
        id=uuid.UUID(int=6),
        email="past@example.com",
        password_hash="x",
        subscription_status="premium",
        subscription_expires_at=now - timedelta(seconds=1),
    )
    past.subscription_verified_at = now - timedelta(hours=1, microseconds=1)
    async_session.add_all([null_high, far, daily_free, null_low, lifetime, past])
    await async_session.commit()
    with patch("app.jobs.subscription_jobs.async_session_maker", return_value=async_session):
        ids = await subscription_jobs._due_subscription_user_ids(now, ())

    assert ids == [null_low.id, null_high.id, daily_free.id, lifetime.id, far.id, past.id]


@pytest.mark.asyncio(loop_scope="session")
async def test_explicit_user_ids_bypass_cadence_and_deduplicate():
    """A scoped recovery never falls back to a sweep."""
    from app.jobs.subscription_jobs import _due_subscription_user_ids

    first, second = uuid.uuid4(), uuid.uuid4()
    assert await _due_subscription_user_ids(datetime.now(UTC), (first, second, first)) == [first, second]


@pytest.mark.asyncio(loop_scope="session")
async def test_empty_due_snapshot_runs_no_accounts(async_session: AsyncSession):
    """A fully current account set produces no recovery work."""
    from app.jobs.subscription_jobs import _due_subscription_user_ids

    await cleanup_subscription_data(async_session)
    now = datetime.now(UTC)
    user = await create_test_user(async_session, email="current@example.com")
    user.subscription_verified_at = now
    await async_session.commit()
    with patch("app.jobs.subscription_jobs.async_session_maker", return_value=async_session):
        assert await _due_subscription_user_ids(now, ()) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_reconciliation_continues_after_account_failure_then_exits_nonzero(async_engine):
    """A later successful account commits even though the fixed pass remains incomplete."""
    from app.jobs import subscription_jobs

    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as setup:
        await cleanup_subscription_data(setup)
        first = await create_test_user(setup, email="failure-first@example.com")
        second = await create_test_user(setup, email="success-second@example.com")
    calls: list[uuid.UUID] = []

    async def reconcile(db: AsyncSession, redis, user_id: uuid.UUID):
        del redis
        calls.append(user_id)
        if user_id == first.id:
            raise PurchaseError("account failure", status_code=503)
        user = await db.get(User, user_id)
        assert user is not None
        user.subscription_status = "premium"
        return reconciliation_for(user, outcome="changed")

    with (
        patch("app.jobs.subscription_jobs.async_session_maker", sessions),
        patch("app.redis.get_redis_client", return_value=MagicMock()),
        patch("app.jobs.subscription_jobs.reconcile_user", side_effect=reconcile),
        patch("app.jobs.subscription_jobs.invalidate_premium_cache", new=AsyncMock()),
    ):
        with pytest.raises(PurchaseError, match="incomplete"):
            await subscription_jobs.check_expired_subscriptions_job(user_ids=(first.id, second.id))

    assert calls == [first.id, second.id]
    async with sessions() as check:
        second_after = await check.get(User, second.id)
        assert second_after is not None and second_after.subscription_status == "premium"


@pytest.mark.asyncio(loop_scope="session")
async def test_all_failing_fixed_pass_attempts_each_explicit_id_once():
    """Account failures do not loop/reselect and the one-shot remains nonzero."""
    from app.jobs import subscription_jobs

    ids = (uuid.uuid4(), uuid.uuid4())
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    reconcile = AsyncMock(side_effect=PurchaseError("account failure", status_code=503))
    with (
        patch("app.jobs.subscription_jobs.async_session_maker", return_value=db),
        patch("app.redis.get_redis_client", return_value=MagicMock()),
        patch("app.jobs.subscription_jobs.reconcile_user", reconcile),
    ):
        with pytest.raises(PurchaseError, match="incomplete"):
            await subscription_jobs.check_expired_subscriptions_job(user_ids=ids)

    assert [call.args[2] for call in reconcile.await_args_list] == list(ids)


@pytest.mark.asyncio(loop_scope="session")
async def test_explicit_unknown_or_deleted_ids_are_skipped_by_worker():
    """Explicit recovery never falls back to a sweep when local identities are absent."""
    from app.jobs import subscription_jobs

    unknown, deleted = uuid.uuid4(), uuid.uuid4()
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    reconcile = AsyncMock(side_effect=[UserNotFoundError(unknown), UserNotFoundError(deleted)])
    with (
        patch("app.jobs.subscription_jobs.async_session_maker", return_value=db),
        patch("app.redis.get_redis_client", return_value=MagicMock()),
        patch("app.jobs.subscription_jobs.reconcile_user", reconcile),
    ):
        assert await subscription_jobs.check_expired_subscriptions_job(user_ids=(unknown, deleted)) == 0

    assert [call.args[2] for call in reconcile.await_args_list] == [unknown, deleted]


@pytest.mark.asyncio(loop_scope="session")
async def test_configuration_failure_aborts_fixed_pass(async_engine):
    """One global configuration failure does not repeat for every selected account."""
    from app.jobs import subscription_jobs

    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    first, second = uuid.uuid4(), uuid.uuid4()
    reconcile = AsyncMock(
        side_effect=RevenueCatAPIError("bad environment", failure_kind="configuration"),
    )
    with (
        patch("app.jobs.subscription_jobs.async_session_maker", sessions),
        patch("app.redis.get_redis_client", return_value=MagicMock()),
        patch("app.jobs.subscription_jobs.reconcile_user", reconcile),
    ):
        with pytest.raises(RevenueCatAPIError, match="bad environment"):
            await subscription_jobs.check_expired_subscriptions_job(user_ids=(first, second))

    reconcile.assert_awaited_once()


@pytest.mark.parametrize(
    "error",
    [
        RevenueCatAPIError("missing configuration", failure_kind="configuration"),
        RevenueCatAPIError("unauthorized", upstream_status=401),
        RevenueCatAPIError("forbidden", upstream_status=403),
        RevenueCatAPIError("cooldown", upstream_status=429, retry_after_seconds=60),
    ],
)
def test_fatal_provider_classification_is_metadata_based(error):
    """Global failures use structured provider metadata rather than message text."""
    from app.jobs.subscription_jobs import _is_fatal_reconciliation_error

    assert _is_fatal_reconciliation_error(error, dry_run=False)


@pytest.mark.asyncio(loop_scope="session")
async def test_fixed_snapshot_batches_every_id_once_when_eligibility_changes():
    """Batch processing slices one captured list and never reselects newly due rows."""
    from app.jobs import subscription_jobs

    ids = tuple(uuid.uuid4() for _ in range(subscription_jobs.settings.SUBSCRIPTION_BATCH_SIZE + 1))
    seen: list[uuid.UUID] = []
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    db.commit = AsyncMock()

    async def reconcile(_db, _redis, user_id):
        seen.append(user_id)
        return Reconciliation(
            user_id=user_id,
            before_status="free",
            before_expires_at=None,
            before_verified_at=None,
            before_subscription={},
            snapshot=SubscriptionSnapshot("free", None, None, False, False),
            verified_at=datetime.now(UTC),
            outcome="unchanged",
        )

    with (
        patch("app.jobs.subscription_jobs._due_subscription_user_ids", new=AsyncMock(return_value=list(ids))),
        patch("app.jobs.subscription_jobs.async_session_maker", return_value=db),
        patch("app.redis.get_redis_client", return_value=MagicMock()),
        patch("app.jobs.subscription_jobs.reconcile_user", side_effect=reconcile),
        patch("app.jobs.subscription_jobs.invalidate_premium_cache", new=AsyncMock()),
    ):
        assert await subscription_jobs.check_expired_subscriptions_job() == len(ids)

    assert seen == list(ids)


@pytest.mark.asyncio(loop_scope="session")
async def test_dry_run_reads_only_and_aborts_on_unexpected_creation(capsys):
    """Dry-run never reaches apply, commit, cache invalidation, or notification I/O."""
    from app.jobs import subscription_jobs

    first, second = uuid.uuid4(), uuid.uuid4()
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    db.commit = AsyncMock()
    read = AsyncMock(side_effect=RevenueCatAPIError("customer may have been created", upstream_status=201))
    with (
        patch("app.jobs.subscription_jobs._due_subscription_user_ids", new=AsyncMock(return_value=[first, second])),
        patch("app.jobs.subscription_jobs.async_session_maker", return_value=db),
        patch("app.redis.get_redis_client", return_value=MagicMock()),
        patch("app.jobs.subscription_jobs.read_reconciliation", read),
        patch("app.jobs.subscription_jobs.reconcile_user", new=AsyncMock()) as reconcile,
        patch("app.jobs.subscription_jobs.invalidate_premium_cache", new=AsyncMock()) as invalidate,
        patch("app.jobs.subscription_jobs._send_subscription_expired_notification", new=AsyncMock()) as notify,
    ):
        with pytest.raises(RevenueCatAPIError, match="may have been created"):
            await subscription_jobs.check_expired_subscriptions_job(dry_run=True, user_ids=(first, second))

    read.assert_awaited_once_with(db, ANY, first, dry_run=True)
    reconcile.assert_not_awaited()
    invalidate.assert_not_awaited()
    notify.assert_not_awaited()
    db.commit.assert_not_awaited()
    output = capsys.readouterr().out
    assert "outcome=error reason=reconciliation_error upstream_status=201" in output
    assert "upstream_customer_may_have_been_created" in output


@pytest.mark.asyncio(loop_scope="session")
async def test_notification_partial_flush_rolls_back_after_projection_commit(async_engine):
    """A notification failure cannot persist half its post-commit transaction."""
    from app.jobs import subscription_jobs

    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as setup:
        await cleanup_subscription_data(setup)
        user = await create_test_user(
            setup,
            email="notification-rollback@example.com",
            subscription_status="premium",
            subscription_expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        setup.add(PushToken(user_id=user.id, token="rollback-token", platform="android"))
        await setup.commit()
        user_id = user.id

    async def reconcile(db: AsyncSession, redis, requested_id: uuid.UUID) -> Reconciliation:
        del redis
        current = await db.get(User, requested_id)
        assert current is not None
        proposal = reconciliation_for(current, outcome="changed")
        current.subscription_status = "free"
        current.subscription_expires_at = None
        return proposal

    class PartialNotificationService:
        def __init__(self, db: AsyncSession):
            self.db = db

        async def send_push(self, **kwargs):
            self.db.add(
                NotificationLog(
                    user_id=kwargs["user_id"],
                    notification_type="subscription_expired",
                    title="partial",
                    body="partial",
                    platform="android",
                    success=False,
                )
            )
            await self.db.execute(text("DELETE FROM push_tokens WHERE token = 'rollback-token'"))
            await self.db.flush()
            raise RuntimeError("second delivery failed")

    with (
        patch("app.jobs.subscription_jobs.async_session_maker", sessions),
        patch("app.redis.get_redis_client", return_value=MagicMock()),
        patch("app.jobs.subscription_jobs.reconcile_user", side_effect=reconcile),
        patch("app.jobs.subscription_jobs.invalidate_premium_cache", new=AsyncMock()),
        patch("app.jobs.subscription_jobs.NotificationService", PartialNotificationService),
    ):
        assert (
            await asyncio.wait_for(subscription_jobs.check_expired_subscriptions_job(user_ids=(user_id,)), timeout=2)
            == 1
        )

    async with sessions() as observer:
        current = await observer.get(User, user_id)
        assert current is not None and current.subscription_status == "free"
        assert await observer.scalar(select(PushToken).where(PushToken.user_id == user_id)) is not None
        assert await observer.scalar(select(NotificationLog).where(NotificationLog.user_id == user_id)) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_dry_run_reports_bounded_reason_and_summary(capsys):
    """Operator output states the safe reason/counts without leaking provider details."""
    from app.jobs import subscription_jobs

    first, missing, failed = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    changed = Reconciliation(
        user_id=first,
        before_status="free",
        before_expires_at=None,
        before_verified_at=None,
        before_subscription={},
        snapshot=SubscriptionSnapshot("premium", None, "premium", False, False),
        verified_at=datetime.now(UTC),
        outcome="changed",
    )
    read = AsyncMock(
        side_effect=[
            changed,
            UserNotFoundError(missing),
            RuntimeError("key=top-secret email=private@example.com"),
        ]
    )
    with (
        patch(
            "app.jobs.subscription_jobs._due_subscription_user_ids",
            new=AsyncMock(return_value=[first, missing, failed]),
        ),
        patch("app.jobs.subscription_jobs.async_session_maker", return_value=db),
        patch("app.redis.get_redis_client", return_value=MagicMock()),
        patch("app.jobs.subscription_jobs.read_reconciliation", read),
    ):
        with pytest.raises(PurchaseError, match="incomplete"):
            await subscription_jobs.check_expired_subscriptions_job(dry_run=True, user_ids=(first, missing, failed))

    output = capsys.readouterr().out
    assert "verified snapshot changes status" in output
    assert "unknown_or_deleted_local_user" in output
    assert "outcome=skipped reason=unknown_or_deleted_local_user" in output
    assert "outcome=error reason=reconciliation_error" in output
    assert "changed=1" in output and "skipped=1" in output and "errors=1" in output
    assert "Subscription reconciliation dry-run result" in output
    assert "Subscription reconciliation completed" in output
    assert "top-secret" not in output and "private@example.com" not in output


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

        with patch("app.jobs.subscription_jobs.NotificationService") as mock_service_class:
            mock_service = AsyncMock()
            mock_service.send_push = AsyncMock(return_value=True)
            mock_service_class.return_value = mock_service

            result = await _send_subscription_expired_notification(async_session, user.id)

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
async def test_send_subscription_expired_notification_propagates_unexpected_error(
    async_session: AsyncSession,
):
    """Test that notification sending handles errors gracefully."""
    await cleanup_subscription_data(async_session)
    try:
        from app.jobs.subscription_jobs import _send_subscription_expired_notification

        user = await create_test_user(async_session)

        with patch("app.jobs.subscription_jobs.NotificationService") as mock_service_class:
            mock_service = AsyncMock()
            mock_service.send_push = AsyncMock(side_effect=Exception("Network error"))
            mock_service_class.return_value = mock_service

            with pytest.raises(Exception, match="Network error"):
                await _send_subscription_expired_notification(async_session, user.id)

    finally:
        await cleanup_subscription_data(async_session)


# Batch processing tests


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
