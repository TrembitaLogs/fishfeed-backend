"""Integration tests for aquarium service."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.fish import Fish
from app.models.species import Species
from app.models.user import User
from app.schemas.aquarium import AquariumCreate, AquariumUpdate
from app.services.aquarium import (
    AquariumAccessDeniedError,
    AquariumNotFoundError,
    AquariumOwnerRequiredError,
    check_access,
    create_aquarium,
    delete_aquarium,
    get_aquarium,
    list_user_aquariums,
    update_aquarium,
)


async def cleanup_aquarium_data(session: AsyncSession) -> None:
    """Helper to cleanup aquarium-related data."""
    await session.execute(text("DELETE FROM fish"))
    await session.execute(text("DELETE FROM aquarium_members"))
    await session.execute(text("DELETE FROM aquariums"))
    await session.execute(text("DELETE FROM users"))
    await session.execute(text("DELETE FROM species"))
    await session.commit()


async def create_test_user(
    session: AsyncSession,
    email: str | None = None,
) -> User:
    """Helper to create a test user."""
    user = User(
        email=email or f"test-{uuid.uuid4()}@example.com",
        password_hash="hashed_password",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def create_test_species(session: AsyncSession, species_id: str = "guppy") -> Species:
    """Helper to create a test species."""
    species = Species(
        id=species_id,
        common_name="Guppy",
        scientific_name="Poecilia reticulata",
        food_types=["flakes"],
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
    aquarium_id: uuid.UUID,
    species_id: str = "guppy",
) -> Fish:
    """Helper to create a test fish."""
    fish = Fish(
        aquarium_id=aquarium_id,
        species_id=species_id,
        quantity=1,
    )
    session.add(fish)
    await session.commit()
    await session.refresh(fish)
    return fish


# create_aquarium tests


@pytest.mark.asyncio(loop_scope="session")
async def test_create_aquarium_creates_record_with_owner_id(async_session: AsyncSession):
    """Test that create_aquarium creates DB record with correct owner_id."""
    await cleanup_aquarium_data(async_session)
    try:
        user = await create_test_user(async_session)
        data = AquariumCreate(name="My Aquarium")

        aquarium = await create_aquarium(async_session, user.id, data)

        assert aquarium.id is not None
        assert aquarium.name == "My Aquarium"
        assert aquarium.owner_id == user.id
        assert aquarium.deleted_at is None
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_aquarium_adds_owner_as_member(async_session: AsyncSession):
    """Test that create_aquarium auto-adds owner to aquarium_members."""
    await cleanup_aquarium_data(async_session)
    try:
        user = await create_test_user(async_session)
        data = AquariumCreate(name="My Aquarium")

        aquarium = await create_aquarium(async_session, user.id, data)

        # Check member was created
        from sqlalchemy import select
        stmt = select(AquariumMember).where(
            AquariumMember.aquarium_id == aquarium.id,
            AquariumMember.user_id == user.id,
        )
        result = await async_session.execute(stmt)
        member = result.scalar_one_or_none()

        assert member is not None
        assert member.role == "owner"
    finally:
        await cleanup_aquarium_data(async_session)


# list_user_aquariums tests


@pytest.mark.asyncio(loop_scope="session")
async def test_list_user_aquariums_returns_owned_aquariums(async_session: AsyncSession):
    """Test that list_user_aquariums returns user's owned aquariums."""
    await cleanup_aquarium_data(async_session)
    try:
        user = await create_test_user(async_session)
        data1 = AquariumCreate(name="Aquarium 1")
        data2 = AquariumCreate(name="Aquarium 2")
        await create_aquarium(async_session, user.id, data1)
        await create_aquarium(async_session, user.id, data2)

        aquariums = await list_user_aquariums(async_session, user.id)

        assert len(aquariums) == 2
        names = [a.name for a in aquariums]
        assert "Aquarium 1" in names
        assert "Aquarium 2" in names
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_user_aquariums_returns_member_aquariums(async_session: AsyncSession):
    """Test that list_user_aquariums returns aquariums where user is member."""
    await cleanup_aquarium_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")

        # Create aquarium owned by another user
        data = AquariumCreate(name="Shared Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add member_user as member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        # member_user should see this aquarium
        aquariums = await list_user_aquariums(async_session, member_user.id)

        assert len(aquariums) == 1
        assert aquariums[0].name == "Shared Aquarium"
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_user_aquariums_excludes_other_users_aquariums(async_session: AsyncSession):
    """Test that list_user_aquariums excludes aquariums without access."""
    await cleanup_aquarium_data(async_session)
    try:
        user1 = await create_test_user(async_session, "user1@example.com")
        user2 = await create_test_user(async_session, "user2@example.com")

        # User1 creates aquarium
        data = AquariumCreate(name="User1 Aquarium")
        await create_aquarium(async_session, user1.id, data)

        # User2 should not see it
        aquariums = await list_user_aquariums(async_session, user2.id)

        assert len(aquariums) == 0
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_user_aquariums_sorted_by_created_at_desc(async_session: AsyncSession):
    """Test that list_user_aquariums returns results sorted by created_at DESC."""
    await cleanup_aquarium_data(async_session)
    try:
        user = await create_test_user(async_session)

        # Create aquariums in order
        data1 = AquariumCreate(name="First")
        data2 = AquariumCreate(name="Second")
        data3 = AquariumCreate(name="Third")
        await create_aquarium(async_session, user.id, data1)
        await create_aquarium(async_session, user.id, data2)
        await create_aquarium(async_session, user.id, data3)

        aquariums = await list_user_aquariums(async_session, user.id)

        # All three aquariums returned
        assert len(aquariums) == 3
        names = {a.name for a in aquariums}
        assert names == {"First", "Second", "Third"}

        # Verify sorted by created_at DESC (or equal timestamps are acceptable)
        for i in range(len(aquariums) - 1):
            assert aquariums[i].created_at >= aquariums[i + 1].created_at
    finally:
        await cleanup_aquarium_data(async_session)


# get_aquarium tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_aquarium_returns_aquarium(async_session: AsyncSession):
    """Test that get_aquarium returns the aquarium for owner."""
    await cleanup_aquarium_data(async_session)
    try:
        user = await create_test_user(async_session)
        data = AquariumCreate(name="My Aquarium")
        created = await create_aquarium(async_session, user.id, data)

        aquarium = await get_aquarium(async_session, created.id, user.id)

        assert aquarium.id == created.id
        assert aquarium.name == "My Aquarium"
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_aquarium_raises_not_found_for_nonexistent(async_session: AsyncSession):
    """Test that get_aquarium raises 404 for non-existent ID."""
    await cleanup_aquarium_data(async_session)
    try:
        user = await create_test_user(async_session)
        random_id = uuid.uuid4()

        with pytest.raises(AquariumNotFoundError) as exc_info:
            await get_aquarium(async_session, random_id, user.id)

        assert exc_info.value.status_code == 404
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_aquarium_raises_access_denied_for_other_user(async_session: AsyncSession):
    """Test that get_aquarium raises 403 for user without access."""
    await cleanup_aquarium_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")

        data = AquariumCreate(name="Owner's Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        with pytest.raises(AquariumAccessDeniedError) as exc_info:
            await get_aquarium(async_session, aquarium.id, other_user.id)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_aquarium_allows_member_access(async_session: AsyncSession):
    """Test that get_aquarium allows access for members."""
    await cleanup_aquarium_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")

        data = AquariumCreate(name="Shared Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        # Member should have access
        result = await get_aquarium(async_session, aquarium.id, member_user.id)

        assert result.id == aquarium.id
    finally:
        await cleanup_aquarium_data(async_session)


# update_aquarium tests


@pytest.mark.asyncio(loop_scope="session")
async def test_update_aquarium_updates_name(async_session: AsyncSession):
    """Test that update_aquarium updates the aquarium name."""
    await cleanup_aquarium_data(async_session)
    try:
        user = await create_test_user(async_session)
        data = AquariumCreate(name="Original Name")
        aquarium = await create_aquarium(async_session, user.id, data)

        update_data = AquariumUpdate(name="Updated Name")
        updated = await update_aquarium(async_session, aquarium.id, user.id, update_data)

        assert updated.name == "Updated Name"
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_aquarium_raises_403_for_member(async_session: AsyncSession):
    """Test that update_aquarium raises 403 for members (non-owners)."""
    await cleanup_aquarium_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")

        data = AquariumCreate(name="Shared Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        # Member should not be able to update
        update_data = AquariumUpdate(name="Hacked Name")
        with pytest.raises(AquariumOwnerRequiredError) as exc_info:
            await update_aquarium(async_session, aquarium.id, member_user.id, update_data)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_aquarium_partial_update(async_session: AsyncSession):
    """Test that update_aquarium only updates provided fields."""
    await cleanup_aquarium_data(async_session)
    try:
        user = await create_test_user(async_session)
        data = AquariumCreate(name="Original Name")
        aquarium = await create_aquarium(async_session, user.id, data)
        original_created_at = aquarium.created_at

        # Update with empty data (no fields set)
        update_data = AquariumUpdate()
        updated = await update_aquarium(async_session, aquarium.id, user.id, update_data)

        assert updated.name == "Original Name"  # Unchanged
        assert updated.created_at == original_created_at
    finally:
        await cleanup_aquarium_data(async_session)


# delete_aquarium tests


@pytest.mark.asyncio(loop_scope="session")
async def test_delete_aquarium_sets_deleted_at(async_session: AsyncSession):
    """Test that delete_aquarium sets deleted_at (soft delete)."""
    await cleanup_aquarium_data(async_session)
    try:
        user = await create_test_user(async_session)
        data = AquariumCreate(name="To Delete")
        aquarium = await create_aquarium(async_session, user.id, data)
        aquarium_id = aquarium.id

        await delete_aquarium(async_session, aquarium_id, user.id)

        # Refresh from DB
        from sqlalchemy import select
        stmt = select(Aquarium).where(Aquarium.id == aquarium_id)
        result = await async_session.execute(stmt)
        deleted_aquarium = result.scalar_one()

        assert deleted_aquarium.deleted_at is not None
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_delete_aquarium_cascade_soft_deletes_fish(async_session: AsyncSession):
    """Test that delete_aquarium cascade soft deletes fish."""
    await cleanup_aquarium_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        data = AquariumCreate(name="With Fish")
        aquarium = await create_aquarium(async_session, user.id, data)

        # Add fish
        fish = await create_test_fish(async_session, aquarium.id, species.id)
        fish_id = fish.id

        await delete_aquarium(async_session, aquarium.id, user.id)

        # Check fish is soft deleted
        from sqlalchemy import select
        stmt = select(Fish).where(Fish.id == fish_id)
        result = await async_session.execute(stmt)
        deleted_fish = result.scalar_one()

        assert deleted_fish.deleted_at is not None
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_delete_aquarium_raises_403_for_member(async_session: AsyncSession):
    """Test that delete_aquarium raises 403 for members (non-owners)."""
    await cleanup_aquarium_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")

        data = AquariumCreate(name="Shared Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        # Member should not be able to delete
        with pytest.raises(AquariumOwnerRequiredError) as exc_info:
            await delete_aquarium(async_session, aquarium.id, member_user.id)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_deleted_aquarium_not_in_list(async_session: AsyncSession):
    """Test that deleted aquarium is not returned in list_user_aquariums."""
    await cleanup_aquarium_data(async_session)
    try:
        user = await create_test_user(async_session)
        data1 = AquariumCreate(name="Keep")
        data2 = AquariumCreate(name="Delete")
        aquarium1 = await create_aquarium(async_session, user.id, data1)
        aquarium2 = await create_aquarium(async_session, user.id, data2)

        # Delete one
        await delete_aquarium(async_session, aquarium2.id, user.id)

        # List should only show the non-deleted one
        aquariums = await list_user_aquariums(async_session, user.id)

        assert len(aquariums) == 1
        assert aquariums[0].id == aquarium1.id
        assert aquariums[0].name == "Keep"
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_deleted_aquarium_raises_not_found(async_session: AsyncSession):
    """Test that get_aquarium raises 404 for deleted aquarium."""
    await cleanup_aquarium_data(async_session)
    try:
        user = await create_test_user(async_session)
        data = AquariumCreate(name="To Delete")
        aquarium = await create_aquarium(async_session, user.id, data)
        aquarium_id = aquarium.id

        await delete_aquarium(async_session, aquarium_id, user.id)

        # Trying to get deleted aquarium should raise 404
        with pytest.raises(AquariumNotFoundError) as exc_info:
            await get_aquarium(async_session, aquarium_id, user.id)

        assert exc_info.value.status_code == 404
    finally:
        await cleanup_aquarium_data(async_session)


# concurrent access tests


@pytest.mark.asyncio(loop_scope="session")
async def test_check_access_concurrent_requests_consistent(async_session: AsyncSession):
    """Test that concurrent check_access calls return consistent results.

    Verifies that the atomic single-query approach in check_access
    produces correct results when called concurrently for the same
    aquarium by both an authorized owner and an unauthorized user.
    """
    await cleanup_aquarium_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        stranger = await create_test_user(async_session, "stranger@example.com")
        data = AquariumCreate(name="Concurrent Test")
        aquarium = await create_aquarium(async_session, owner.id, data)

        async def owner_access():
            aq, role = await check_access(async_session, aquarium.id, owner.id)
            return aq.id, role

        async def stranger_access():
            try:
                await check_access(async_session, aquarium.id, stranger.id)
                return None, "should_have_raised"
            except AquariumAccessDeniedError:
                return None, "denied"

        results = await asyncio.gather(
            owner_access(),
            stranger_access(),
            owner_access(),
            stranger_access(),
        )

        # Owner always gets access with 'owner' role
        assert results[0] == (aquarium.id, "owner")
        assert results[2] == (aquarium.id, "owner")
        # Stranger always gets denied
        assert results[1] == (None, "denied")
        assert results[3] == (None, "denied")
    finally:
        await cleanup_aquarium_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_access_atomic_for_member_role(async_session: AsyncSession):
    """Test that check_access returns correct member role atomically."""
    await cleanup_aquarium_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")

        data = AquariumCreate(name="Member Atomic Test")
        aquarium = await create_aquarium(async_session, owner.id, data)

        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        # Concurrent access checks for owner and member
        results = await asyncio.gather(
            check_access(async_session, aquarium.id, owner.id),
            check_access(async_session, aquarium.id, member_user.id),
        )

        owner_aq, owner_role = results[0]
        member_aq, member_role = results[1]

        assert owner_aq.id == aquarium.id
        assert owner_role == "owner"
        assert member_aq.id == aquarium.id
        assert member_role == "member"
    finally:
        await cleanup_aquarium_data(async_session)
