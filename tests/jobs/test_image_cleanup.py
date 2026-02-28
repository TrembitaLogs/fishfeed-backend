"""Tests for image cleanup background jobs."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium
from app.models.fish import Fish
from app.models.orphaned_image import OrphanedImage
from app.models.user import User

# --- Constants ---

# Mock S3 settings used across all tests
_MOCK_S3_ENDPOINT = "http://minio:9000"
_MOCK_S3_ACCESS_KEY = "test-access-key"
_MOCK_S3_SECRET_KEY = "test-secret-key"
_MOCK_S3_REGION = "us-east-1"
_MOCK_S3_IMAGES_BUCKET = "fishfeed-images"


# --- Helpers ---


async def cleanup_orphaned_data(session: AsyncSession) -> None:
    """Delete all orphaned_images records."""
    await session.execute(text("DELETE FROM orphaned_images"))
    await session.commit()


async def create_orphaned_image(
    session: AsyncSession,
    old_key: str = "aquariums/test-id/abc12345.webp",
    entity_type: str = "aquarium",
    orphaned_at: datetime | None = None,
) -> OrphanedImage:
    """Create a test OrphanedImage record."""
    orphan = OrphanedImage(
        old_key=old_key,
        entity_type=entity_type,
        orphaned_at=orphaned_at or datetime.now(UTC),
    )
    session.add(orphan)
    await session.commit()
    await session.refresh(orphan)
    return orphan


class MockSessionContext:
    """Mock for async_session_maker() context manager."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        pass


def _make_mock_settings(**overrides: object) -> MagicMock:
    """Build mock settings with valid S3 config.

    Override specific fields by passing keyword arguments.
    """
    mock = MagicMock()
    mock.S3_ENDPOINT_URL = overrides.get("S3_ENDPOINT_URL", _MOCK_S3_ENDPOINT)
    mock.S3_ACCESS_KEY = overrides.get("S3_ACCESS_KEY", _MOCK_S3_ACCESS_KEY)
    mock.S3_SECRET_KEY = overrides.get("S3_SECRET_KEY", _MOCK_S3_SECRET_KEY)
    mock.S3_REGION = overrides.get("S3_REGION", _MOCK_S3_REGION)
    mock.S3_IMAGES_BUCKET_NAME = overrides.get("S3_IMAGES_BUCKET_NAME", _MOCK_S3_IMAGES_BUCKET)
    return mock


def _make_mock_s3(side_effects: dict[str, Exception] | None = None) -> AsyncMock:
    """Build mock S3 client with optional per-key failures.

    Args:
        side_effects: Map of S3 key -> exception to raise for that key.
                      Keys not in the map succeed silently.
    """
    side_effects = side_effects or {}

    async def _delete_object(*, Bucket: str, Key: str) -> dict:  # noqa: N803
        if Key in side_effects:
            raise side_effects[Key]
        return {}

    mock_s3 = AsyncMock()
    mock_s3.delete_object = AsyncMock(side_effect=_delete_object)
    return mock_s3


def _make_mock_s3_session(mock_s3: AsyncMock) -> MagicMock:
    """Build mock aioboto3.Session whose .client() yields *mock_s3*."""
    mock_client_ctx = AsyncMock()
    mock_client_ctx.__aenter__ = AsyncMock(return_value=mock_s3)
    mock_client_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.client.return_value = mock_client_ctx
    return mock_session


# --- Tests ---


@pytest.mark.asyncio(loop_scope="session")
async def test_cleanup_deletes_old_orphaned_images(async_session: AsyncSession):
    """Orphaned images older than 7 days are deleted from S3 and DB."""
    await cleanup_orphaned_data(async_session)
    try:
        from app.jobs.image_cleanup import image_cleanup_job

        # Old orphan (10 days ago) — should be deleted
        old_orphan = await create_orphaned_image(
            async_session,
            old_key="aquariums/aaa/old11111.webp",
            entity_type="aquarium",
            orphaned_at=datetime.now(UTC) - timedelta(days=10),
        )
        old_id = old_orphan.id

        mock_s3 = _make_mock_s3()
        mock_s3_session = _make_mock_s3_session(mock_s3)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch(
                "app.jobs.image_cleanup.async_session_maker",
            ) as mock_session_maker,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session,
            ),
        ):
            mock_session_maker.return_value = MockSessionContext(async_session)
            result = await image_cleanup_job()

        # S3 delete_object should have been called
        mock_s3.delete_object.assert_called_once_with(
            Bucket=_MOCK_S3_IMAGES_BUCKET,
            Key="aquariums/aaa/old11111.webp",
        )

        # Record should be removed from DB
        stmt = select(OrphanedImage).where(OrphanedImage.id == old_id)
        row = await async_session.execute(stmt)
        assert row.scalar_one_or_none() is None

        assert result["total_deleted_s3"] == 1
        assert result["total_deleted_db"] == 1
        assert result["total_failed_s3"] == 0
    finally:
        await cleanup_orphaned_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_cleanup_keeps_young_orphaned_images(async_session: AsyncSession):
    """Orphaned images younger than 7 days are NOT deleted."""
    await cleanup_orphaned_data(async_session)
    try:
        from app.jobs.image_cleanup import image_cleanup_job

        # Recent orphan (3 days ago) — within grace period
        young_orphan = await create_orphaned_image(
            async_session,
            old_key="fish/bbb/young1111.webp",
            entity_type="fish",
            orphaned_at=datetime.now(UTC) - timedelta(days=3),
        )
        young_id = young_orphan.id

        mock_s3 = _make_mock_s3()
        mock_s3_session = _make_mock_s3_session(mock_s3)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch(
                "app.jobs.image_cleanup.async_session_maker",
            ) as mock_session_maker,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session,
            ),
        ):
            mock_session_maker.return_value = MockSessionContext(async_session)
            result = await image_cleanup_job()

        # S3 should NOT be called
        mock_s3.delete_object.assert_not_called()

        # Record should still exist
        stmt = select(OrphanedImage).where(OrphanedImage.id == young_id)
        row = await async_session.execute(stmt)
        assert row.scalar_one_or_none() is not None

        assert result["total_deleted_s3"] == 0
        assert result["total_deleted_db"] == 0
    finally:
        await cleanup_orphaned_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_cleanup_empty_table(async_session: AsyncSession):
    """Job handles empty orphaned_images table without errors."""
    await cleanup_orphaned_data(async_session)
    try:
        from app.jobs.image_cleanup import image_cleanup_job

        mock_s3 = _make_mock_s3()
        mock_s3_session = _make_mock_s3_session(mock_s3)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch(
                "app.jobs.image_cleanup.async_session_maker",
            ) as mock_session_maker,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session,
            ),
        ):
            mock_session_maker.return_value = MockSessionContext(async_session)
            result = await image_cleanup_job()

        mock_s3.delete_object.assert_not_called()
        assert result["total_deleted_s3"] == 0
        assert result["total_deleted_db"] == 0
        assert result["batches_processed"] == 0
    finally:
        await cleanup_orphaned_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_cleanup_partial_s3_failure(async_session: AsyncSession):
    """When some S3 deletes fail, only successful records are removed from DB."""
    await cleanup_orphaned_data(async_session)
    try:
        from app.jobs.image_cleanup import image_cleanup_job

        old_date = datetime.now(UTC) - timedelta(days=10)

        # Two old orphans — one will succeed, one will fail
        good_orphan = await create_orphaned_image(
            async_session,
            old_key="aquariums/ccc/good1111.webp",
            entity_type="aquarium",
            orphaned_at=old_date,
        )
        good_id = good_orphan.id

        bad_orphan = await create_orphaned_image(
            async_session,
            old_key="fish/ddd/bad11111.webp",
            entity_type="fish",
            orphaned_at=old_date,
        )
        bad_id = bad_orphan.id

        # S3 fails only for the "bad" key
        mock_s3 = _make_mock_s3(
            side_effects={"fish/ddd/bad11111.webp": Exception("S3 error")},
        )
        mock_s3_session = _make_mock_s3_session(mock_s3)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch(
                "app.jobs.image_cleanup.async_session_maker",
            ) as mock_session_maker,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session,
            ),
        ):
            mock_session_maker.return_value = MockSessionContext(async_session)
            result = await image_cleanup_job()

        # Good orphan should be removed from DB
        stmt = select(OrphanedImage).where(OrphanedImage.id == good_id)
        row = await async_session.execute(stmt)
        assert row.scalar_one_or_none() is None

        # Bad orphan should still be in DB (will retry next run)
        stmt = select(OrphanedImage).where(OrphanedImage.id == bad_id)
        row = await async_session.execute(stmt)
        assert row.scalar_one_or_none() is not None

        assert result["total_deleted_s3"] == 1
        # Bad record is attempted twice: once in batch 1 (alongside good),
        # once in batch 2 (alone, triggers all-failed stop).
        assert result["total_failed_s3"] == 2
        assert result["total_deleted_db"] == 1
        assert result["batches_processed"] == 2
    finally:
        await cleanup_orphaned_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_cleanup_all_s3_failures_stops_early(async_session: AsyncSession):
    """When all S3 deletes fail in a batch, job stops processing."""
    await cleanup_orphaned_data(async_session)
    try:
        from app.jobs.image_cleanup import image_cleanup_job

        old_date = datetime.now(UTC) - timedelta(days=10)

        orphan1 = await create_orphaned_image(
            async_session,
            old_key="avatars/eee/fail1111.webp",
            entity_type="avatar",
            orphaned_at=old_date,
        )
        orphan1_id = orphan1.id

        orphan2 = await create_orphaned_image(
            async_session,
            old_key="avatars/fff/fail2222.webp",
            entity_type="avatar",
            orphaned_at=old_date,
        )
        orphan2_id = orphan2.id

        # All S3 deletes fail
        mock_s3 = _make_mock_s3(
            side_effects={
                "avatars/eee/fail1111.webp": Exception("S3 down"),
                "avatars/fff/fail2222.webp": Exception("S3 down"),
            },
        )
        mock_s3_session = _make_mock_s3_session(mock_s3)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch(
                "app.jobs.image_cleanup.async_session_maker",
            ) as mock_session_maker,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session,
            ),
        ):
            mock_session_maker.return_value = MockSessionContext(async_session)
            result = await image_cleanup_job()

        # Both records should still be in DB
        for oid in (orphan1_id, orphan2_id):
            stmt = select(OrphanedImage).where(OrphanedImage.id == oid)
            row = await async_session.execute(stmt)
            assert row.scalar_one_or_none() is not None

        assert result["total_deleted_s3"] == 0
        assert result["total_failed_s3"] == 2
        assert result["total_deleted_db"] == 0
        assert result["batches_processed"] == 1
    finally:
        await cleanup_orphaned_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_cleanup_mixed_entity_types(async_session: AsyncSession):
    """Job correctly processes orphans of different entity types."""
    await cleanup_orphaned_data(async_session)
    try:
        from app.jobs.image_cleanup import image_cleanup_job

        old_date = datetime.now(UTC) - timedelta(days=10)

        await create_orphaned_image(
            async_session,
            old_key="aquariums/111/aaaa1111.webp",
            entity_type="aquarium",
            orphaned_at=old_date,
        )
        await create_orphaned_image(
            async_session,
            old_key="fish/222/bbbb2222.webp",
            entity_type="fish",
            orphaned_at=old_date,
        )
        await create_orphaned_image(
            async_session,
            old_key="avatars/333/cccc3333.webp",
            entity_type="avatar",
            orphaned_at=old_date,
        )

        mock_s3 = _make_mock_s3()
        mock_s3_session = _make_mock_s3_session(mock_s3)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch(
                "app.jobs.image_cleanup.async_session_maker",
            ) as mock_session_maker,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session,
            ),
        ):
            mock_session_maker.return_value = MockSessionContext(async_session)
            result = await image_cleanup_job()

        assert result["total_deleted_s3"] == 3
        assert result["total_deleted_db"] == 3
        assert result["total_failed_s3"] == 0

        # All records removed from DB
        stmt = select(OrphanedImage)
        rows = await async_session.execute(stmt)
        remaining = rows.scalars().all()
        assert len(remaining) == 0
    finally:
        await cleanup_orphaned_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_cleanup_skips_when_s3_not_configured(async_session: AsyncSession):
    """Job returns early when S3 credentials are missing."""
    from app.jobs.image_cleanup import image_cleanup_job

    with patch(
        "app.jobs.image_cleanup.settings",
        _make_mock_settings(S3_ENDPOINT_URL=None),
    ):
        result = await image_cleanup_job()

    assert result["skipped"] is True
    assert result["reason"] == "S3 not configured"


# ===========================================================================
# s3_reconciliation_job tests
# ===========================================================================

# --- Reconciliation helpers ---


async def cleanup_reconciliation_data(session: AsyncSession) -> None:
    """Delete all test data for reconciliation tests.

    Deletion order respects FK constraints (child → parent).
    Begins with rollback to clear any pending transaction errors.
    """
    await session.rollback()
    await session.execute(text("DELETE FROM orphaned_images"))
    await session.execute(text("DELETE FROM aquarium_members"))
    await session.execute(text("DELETE FROM aquariums"))
    await session.execute(text("DELETE FROM users"))
    await session.commit()


async def create_test_user(
    session: AsyncSession,
    *,
    avatar_key: str | None = None,
) -> User:
    """Create a minimal User for reconciliation tests."""
    user = User(
        email=f"recon-test-{uuid.uuid4()}@test.com",
        password_hash="hashed",
        avatar_key=avatar_key,
    )
    session.add(user)
    await session.flush()
    return user


async def create_test_aquarium(
    session: AsyncSession,
    owner_id: uuid.UUID,
    *,
    photo_key: str | None = None,
) -> Aquarium:
    """Create a minimal Aquarium for reconciliation tests."""
    aquarium = Aquarium(
        owner_id=owner_id,
        name=f"recon-test-{uuid.uuid4().hex[:8]}",
        photo_key=photo_key,
    )
    session.add(aquarium)
    await session.flush()
    return aquarium


async def create_test_fish(
    session: AsyncSession,
    aquarium_id: uuid.UUID,
    *,
    photo_key: str | None = None,
) -> Fish:
    """Create a minimal Fish for reconciliation tests."""
    fish = Fish(
        aquarium_id=aquarium_id,
        species_id="test-guppy",
        photo_key=photo_key,
    )
    session.add(fish)
    await session.flush()
    return fish


def _make_mock_s3_for_listing(
    objects_by_prefix: dict[str, list[str]],
    *,
    page_size: int = 1000,
) -> AsyncMock:
    """Build mock S3 client whose list_objects_v2 returns paginated results.

    Args:
        objects_by_prefix: Map of S3 prefix → list of object keys under it.
        page_size: Max keys per page (simulates pagination).
    """

    async def _list_objects_v2(**kwargs: object) -> dict:
        prefix = kwargs.get("Prefix", "")
        max_keys = min(int(kwargs.get("MaxKeys", 1000)), page_size)
        token = kwargs.get("ContinuationToken")

        all_keys = objects_by_prefix.get(prefix, [])
        offset = int(token) if token else 0
        page_keys = all_keys[offset : offset + max_keys]
        is_truncated = offset + max_keys < len(all_keys)

        response: dict = {}
        if page_keys:
            response["Contents"] = [{"Key": k} for k in page_keys]
        if is_truncated:
            response["IsTruncated"] = True
            response["NextContinuationToken"] = str(offset + max_keys)

        return response

    mock_s3 = AsyncMock()
    mock_s3.list_objects_v2 = AsyncMock(side_effect=_list_objects_v2)
    return mock_s3


# --- Reconciliation tests ---


@pytest.mark.asyncio(loop_scope="session")
async def test_reconciliation_finds_unreferenced_s3_objects(async_session: AsyncSession):
    """Unreferenced S3 objects are added to orphaned_images."""
    await cleanup_reconciliation_data(async_session)
    try:
        from app.jobs.image_cleanup import s3_reconciliation_job

        # Create a user + aquarium with a known photo_key (referenced)
        referenced_key = "aquariums/aaa/ref11111.webp"
        user = await create_test_user(async_session)
        await create_test_aquarium(
            async_session,
            user.id,
            photo_key=referenced_key,
        )
        await async_session.commit()

        # S3 has both the referenced key AND an unreferenced one
        mock_s3 = _make_mock_s3_for_listing(
            {
                "aquariums/": [
                    referenced_key,
                    "aquariums/zzz/orphan111.webp",
                ],
                "fish/": [],
                "avatars/": [],
            },
        )
        mock_s3_session = _make_mock_s3_session(mock_s3)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch(
                "app.jobs.image_cleanup.async_session_maker",
            ) as mock_session_maker,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session,
            ),
        ):
            mock_session_maker.return_value = MockSessionContext(async_session)
            result = await s3_reconciliation_job()

        assert result["total_s3_objects"] == 2
        assert result["new_orphaned"] == 1

        # Verify the unreferenced key was added to orphaned_images
        stmt = select(OrphanedImage).where(
            OrphanedImage.old_key == "aquariums/zzz/orphan111.webp",
        )
        row = await async_session.execute(stmt)
        orphan = row.scalar_one_or_none()
        assert orphan is not None
        assert orphan.entity_type == "aquarium"

        # Verify the referenced key was NOT added
        stmt = select(OrphanedImage).where(
            OrphanedImage.old_key == referenced_key,
        )
        row = await async_session.execute(stmt)
        assert row.scalar_one_or_none() is None
    finally:
        await cleanup_reconciliation_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_reconciliation_handles_pagination(async_session: AsyncSession):
    """Job processes all S3 pages when IsTruncated is True."""
    await cleanup_reconciliation_data(async_session)
    try:
        from app.jobs.image_cleanup import s3_reconciliation_job

        user = await create_test_user(async_session)
        await async_session.commit()

        # 3 unreferenced objects across 2 pages (page_size=2)
        mock_s3 = _make_mock_s3_for_listing(
            {
                "aquariums/": [
                    "aquariums/aaa/page1_a.webp",
                    "aquariums/bbb/page1_b.webp",
                    "aquariums/ccc/page2_a.webp",
                ],
                "fish/": [],
                "avatars/": [],
            },
            page_size=2,
        )
        mock_s3_session = _make_mock_s3_session(mock_s3)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch(
                "app.jobs.image_cleanup.async_session_maker",
            ) as mock_session_maker,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session,
            ),
        ):
            mock_session_maker.return_value = MockSessionContext(async_session)
            result = await s3_reconciliation_job()

        assert result["total_s3_objects"] == 3
        assert result["new_orphaned"] == 3

        # All three should be in orphaned_images
        stmt = select(OrphanedImage)
        rows = await async_session.execute(stmt)
        orphans = rows.scalars().all()
        orphan_keys = {o.old_key for o in orphans}
        assert orphan_keys == {
            "aquariums/aaa/page1_a.webp",
            "aquariums/bbb/page1_b.webp",
            "aquariums/ccc/page2_a.webp",
        }

        # Verify list_objects_v2 was called at least twice for aquariums/
        calls = mock_s3.list_objects_v2.call_args_list
        aquarium_calls = [c for c in calls if c.kwargs.get("Prefix") == "aquariums/"]
        assert len(aquarium_calls) >= 2
    finally:
        await cleanup_reconciliation_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_reconciliation_idempotency(async_session: AsyncSession):
    """Running reconciliation twice does not duplicate orphaned_images records."""
    await cleanup_reconciliation_data(async_session)
    try:
        from app.jobs.image_cleanup import s3_reconciliation_job

        user = await create_test_user(async_session)
        await async_session.commit()

        s3_objects = {
            "aquariums/": ["aquariums/xxx/orphan_a.webp"],
            "fish/": [],
            "avatars/": [],
        }

        # First run — should register 1 orphan
        mock_s3 = _make_mock_s3_for_listing(s3_objects)
        mock_s3_session = _make_mock_s3_session(mock_s3)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch(
                "app.jobs.image_cleanup.async_session_maker",
            ) as mock_session_maker,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session,
            ),
        ):
            mock_session_maker.return_value = MockSessionContext(async_session)
            result1 = await s3_reconciliation_job()

        assert result1["new_orphaned"] == 1

        # Second run — same S3 state, should register 0 new orphans
        mock_s3 = _make_mock_s3_for_listing(s3_objects)
        mock_s3_session = _make_mock_s3_session(mock_s3)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch(
                "app.jobs.image_cleanup.async_session_maker",
            ) as mock_session_maker,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session,
            ),
        ):
            mock_session_maker.return_value = MockSessionContext(async_session)
            result2 = await s3_reconciliation_job()

        assert result2["new_orphaned"] == 0

        # Still only 1 record in orphaned_images
        stmt = select(OrphanedImage)
        rows = await async_session.execute(stmt)
        assert len(rows.scalars().all()) == 1
    finally:
        await cleanup_reconciliation_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_reconciliation_empty_bucket(async_session: AsyncSession):
    """Job handles empty S3 bucket without errors."""
    await cleanup_reconciliation_data(async_session)
    try:
        from app.jobs.image_cleanup import s3_reconciliation_job

        mock_s3 = _make_mock_s3_for_listing(
            {"aquariums/": [], "fish/": [], "avatars/": []},
        )
        mock_s3_session = _make_mock_s3_session(mock_s3)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch(
                "app.jobs.image_cleanup.async_session_maker",
            ) as mock_session_maker,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session,
            ),
        ):
            mock_session_maker.return_value = MockSessionContext(async_session)
            result = await s3_reconciliation_job()

        assert result["total_s3_objects"] == 0
        assert result["new_orphaned"] == 0
        assert result["known_keys_in_db"] == 0
    finally:
        await cleanup_reconciliation_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_reconciliation_skips_when_s3_not_configured(async_session: AsyncSession):
    """Reconciliation returns early when S3 credentials are missing."""
    from app.jobs.image_cleanup import s3_reconciliation_job

    with patch(
        "app.jobs.image_cleanup.settings",
        _make_mock_settings(S3_ENDPOINT_URL=None),
    ):
        result = await s3_reconciliation_job()

    assert result["skipped"] is True
    assert result["reason"] == "S3 not configured"


@pytest.mark.asyncio(loop_scope="session")
async def test_reconciliation_all_entity_types(async_session: AsyncSession):
    """Job correctly detects referenced keys across all entity types.

    Uses User (avatar_key) and Aquarium (photo_key) as real DB entities.
    Fish prefix is tested via S3 listing only (no Fish entity created)
    because species FK data may be absent in full test suite runs.
    """
    await cleanup_reconciliation_data(async_session)
    try:
        from app.jobs.image_cleanup import s3_reconciliation_job

        # Create entities with photo_key/avatar_key
        user = await create_test_user(
            async_session,
            avatar_key="avatars/uid1/avatar11.webp",
        )
        await create_test_aquarium(
            async_session,
            user.id,
            photo_key="aquariums/aq1/photo111.webp",
        )
        await async_session.commit()

        # S3 contains 2 referenced keys + 1 unreferenced per prefix
        mock_s3 = _make_mock_s3_for_listing(
            {
                "aquariums/": [
                    "aquariums/aq1/photo111.webp",
                    "aquariums/aq2/orphan111.webp",
                ],
                "fish/": [
                    "fish/fish1/orphan111.webp",
                ],
                "avatars/": [
                    "avatars/uid1/avatar11.webp",
                    "avatars/uid2/orphan111.webp",
                ],
            },
        )
        mock_s3_session = _make_mock_s3_session(mock_s3)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch(
                "app.jobs.image_cleanup.async_session_maker",
            ) as mock_session_maker,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session,
            ),
        ):
            mock_session_maker.return_value = MockSessionContext(async_session)
            result = await s3_reconciliation_job()

        assert result["total_s3_objects"] == 5
        assert result["known_keys_in_db"] == 2
        assert result["new_orphaned"] == 3

        # Verify entity_type assignment for all three prefixes
        stmt = select(OrphanedImage).order_by(OrphanedImage.old_key)
        rows = await async_session.execute(stmt)
        orphans = {o.old_key: o.entity_type for o in rows.scalars().all()}
        assert orphans == {
            "aquariums/aq2/orphan111.webp": "aquarium",
            "avatars/uid2/orphan111.webp": "avatar",
            "fish/fish1/orphan111.webp": "fish",
        }
    finally:
        await cleanup_reconciliation_data(async_session)


# ===========================================================================
# E2E workflow: cleanup → reconciliation pipeline
# ===========================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_cleanup_then_reconciliation_workflow(async_session: AsyncSession):
    """Full garbage collection pipeline: cleanup removes old orphans, then
    reconciliation detects unreferenced S3 objects missed by normal tracking.

    Scenario:
    - Aquarium has current photo (referenced in DB)
    - Old photo was replaced → tracked in orphaned_images (10 days old)
    - A "ghost" photo exists in S3 (crash after upload, never tracked)

    Step 1: image_cleanup_job deletes the tracked orphan from S3 + DB
    Step 2: s3_reconciliation_job discovers the ghost and registers it
    """
    await cleanup_reconciliation_data(async_session)
    try:
        from app.jobs.image_cleanup import image_cleanup_job, s3_reconciliation_job

        # --- Setup: entities + orphan record ---
        current_key = "aquariums/wf1/current1.webp"
        old_key = "aquariums/wf1/old11111.webp"
        ghost_key = "aquariums/wf1/ghost111.webp"

        user = await create_test_user(async_session)
        await create_test_aquarium(
            async_session,
            user.id,
            photo_key=current_key,
        )
        await async_session.commit()

        # Old photo was replaced 10 days ago → tracked in orphaned_images
        await create_orphaned_image(
            async_session,
            old_key=old_key,
            entity_type="aquarium",
            orphaned_at=datetime.now(UTC) - timedelta(days=10),
        )

        # --- Step 1: cleanup job ---
        # S3 has all three files; cleanup should delete old_key
        deleted_keys: list[str] = []

        async def _track_delete(*, Bucket: str, Key: str) -> dict:  # noqa: N803
            deleted_keys.append(Key)
            return {}

        mock_s3_cleanup = AsyncMock()
        mock_s3_cleanup.delete_object = AsyncMock(side_effect=_track_delete)
        mock_s3_session_cleanup = _make_mock_s3_session(mock_s3_cleanup)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch("app.jobs.image_cleanup.async_session_maker") as mock_sm,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session_cleanup,
            ),
        ):
            mock_sm.return_value = MockSessionContext(async_session)
            cleanup_result = await image_cleanup_job()

        assert cleanup_result["total_deleted_s3"] == 1
        assert cleanup_result["total_deleted_db"] == 1
        assert old_key in deleted_keys

        # Verify orphaned_images is now empty
        stmt = select(OrphanedImage)
        rows = await async_session.execute(stmt)
        assert len(rows.scalars().all()) == 0

        # --- Step 2: reconciliation job ---
        # S3 now has current_key + ghost_key (old_key was deleted in step 1)
        mock_s3_recon = _make_mock_s3_for_listing(
            {
                "aquariums/": [current_key, ghost_key],
                "fish/": [],
                "avatars/": [],
            },
        )
        mock_s3_session_recon = _make_mock_s3_session(mock_s3_recon)

        with (
            patch("app.jobs.image_cleanup.settings", _make_mock_settings()),
            patch("app.jobs.image_cleanup.async_session_maker") as mock_sm,
            patch(
                "app.jobs.image_cleanup.aioboto3.Session",
                return_value=mock_s3_session_recon,
            ),
        ):
            mock_sm.return_value = MockSessionContext(async_session)
            recon_result = await s3_reconciliation_job()

        # current_key is referenced → not orphaned
        # ghost_key is unreferenced → newly orphaned
        assert recon_result["total_s3_objects"] == 2
        assert recon_result["new_orphaned"] == 1

        stmt = select(OrphanedImage).where(OrphanedImage.old_key == ghost_key)
        row = await async_session.execute(stmt)
        ghost_orphan = row.scalar_one_or_none()
        assert ghost_orphan is not None
        assert ghost_orphan.entity_type == "aquarium"

        # current_key must NOT be in orphaned_images
        stmt = select(OrphanedImage).where(OrphanedImage.old_key == current_key)
        row = await async_session.execute(stmt)
        assert row.scalar_one_or_none() is None
    finally:
        await cleanup_reconciliation_data(async_session)
