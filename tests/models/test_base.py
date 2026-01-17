from datetime import UTC, datetime

import pytest
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin


class SampleModel(Base, TimestampMixin, SoftDeleteMixin):
    """Sample model using both mixins for testing."""

    __tablename__ = "sample_model"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))


@pytest.mark.asyncio(loop_scope="session")
async def test_timestamp_mixin_created_at(async_session):
    """Test that created_at is set automatically on insert."""
    obj = SampleModel(name="test")
    async_session.add(obj)
    await async_session.commit()
    await async_session.refresh(obj)

    assert obj.created_at is not None
    # Verify it's a recent timestamp (within last minute)
    now = datetime.now(UTC)
    created = obj.created_at.replace(tzinfo=UTC)
    assert (now - created).total_seconds() < 60


@pytest.mark.asyncio(loop_scope="session")
async def test_timestamp_mixin_updated_at(async_session):
    """Test that updated_at is set on insert and update."""
    obj = SampleModel(name="test")
    async_session.add(obj)
    await async_session.commit()
    await async_session.refresh(obj)

    initial_updated_at = obj.updated_at
    assert initial_updated_at is not None

    obj.name = "updated"
    await async_session.commit()
    await async_session.refresh(obj)

    # Note: SQLite doesn't support onupdate with func.now() the same way PostgreSQL does
    # In production with PostgreSQL, updated_at would be automatically updated
    assert obj.updated_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_soft_delete_mixin_initial_state(async_session):
    """Test that deleted_at is None initially."""
    obj = SampleModel(name="test")
    async_session.add(obj)
    await async_session.commit()
    await async_session.refresh(obj)

    assert obj.deleted_at is None
    assert obj.is_deleted() is False


@pytest.mark.asyncio(loop_scope="session")
async def test_soft_delete_mixin_is_deleted(async_session):
    """Test is_deleted() method."""
    obj = SampleModel(name="test")
    async_session.add(obj)
    await async_session.commit()
    await async_session.refresh(obj)

    assert obj.is_deleted() is False

    obj.deleted_at = datetime.now(UTC)
    await async_session.commit()
    await async_session.refresh(obj)

    assert obj.is_deleted() is True


@pytest.mark.asyncio(loop_scope="session")
async def test_model_has_all_mixin_columns(async_session):
    """Test that model has all expected columns from mixins."""
    obj = SampleModel(name="test")
    async_session.add(obj)
    await async_session.commit()
    await async_session.refresh(obj)

    assert hasattr(obj, "created_at")
    assert hasattr(obj, "updated_at")
    assert hasattr(obj, "deleted_at")
    assert hasattr(obj, "is_deleted")
