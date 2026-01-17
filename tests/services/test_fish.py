"""Integration tests for fish service."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.fish import Fish
from app.models.species import Species
from app.models.user import User
from app.schemas.fish import FishCreate, FishUpdate
from app.services.aquarium import AquariumAccessDeniedError
from app.services.fish import (
    FishNotFoundError,
    SpeciesNotFoundError,
    add_fish,
    get_fish,
    get_fish_by_species,
    list_fish,
    remove_fish,
    update_fish,
)


async def cleanup_fish_data(session: AsyncSession) -> None:
    """Helper to cleanup fish-related data."""
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


async def create_test_species(
    session: AsyncSession,
    species_id: str = "guppy",
    common_name: str = "Guppy",
) -> Species:
    """Helper to create a test species."""
    species = Species(
        id=species_id,
        common_name=common_name,
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


# add_fish tests


@pytest.mark.asyncio(loop_scope="session")
async def test_add_fish_creates_record_with_correct_data(async_session: AsyncSession):
    """Test that add_fish creates DB record with correct data."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        data = FishCreate(
            species_id=species.id,
            quantity=5,
            custom_name="My Guppies",
            added_via="manual",
        )

        fish = await add_fish(async_session, aquarium.id, user.id, data)

        assert fish.id is not None
        assert fish.aquarium_id == aquarium.id
        assert fish.species_id == species.id
        assert fish.quantity == 5
        assert fish.custom_name == "My Guppies"
        assert fish.added_via == "manual"
        assert fish.deleted_at is None
        assert fish.species is not None
        assert fish.species.common_name == "Guppy"
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_add_fish_with_nonexistent_species_raises_404(
    async_session: AsyncSession,
):
    """Test that add_fish raises 404 for non-existent species."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        data = FishCreate(species_id="nonexistent_species")

        with pytest.raises(SpeciesNotFoundError) as exc_info:
            await add_fish(async_session, aquarium.id, user.id, data)

        assert exc_info.value.status_code == 404
        assert "nonexistent_species" in str(exc_info.value.message)
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_add_fish_with_ai_scan_via(async_session: AsyncSession):
    """Test that add_fish correctly logs added_via for AI scan."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        data = FishCreate(species_id=species.id, added_via="ai_scan")

        fish = await add_fish(async_session, aquarium.id, user.id, data)

        assert fish.added_via == "ai_scan"
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_add_fish_raises_403_for_non_member(async_session: AsyncSession):
    """Test that add_fish raises 403 for user without aquarium access."""
    await cleanup_fish_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, owner)

        data = FishCreate(species_id=species.id)

        with pytest.raises(AquariumAccessDeniedError) as exc_info:
            await add_fish(async_session, aquarium.id, other_user.id, data)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_add_fish_allows_member_access(async_session: AsyncSession):
    """Test that add_fish allows members to add fish."""
    await cleanup_fish_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, owner)

        # Add member_user as member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        data = FishCreate(species_id=species.id)

        fish = await add_fish(async_session, aquarium.id, member_user.id, data)

        assert fish.id is not None
    finally:
        await cleanup_fish_data(async_session)


# list_fish tests


@pytest.mark.asyncio(loop_scope="session")
async def test_list_fish_returns_aquarium_fish(async_session: AsyncSession):
    """Test that list_fish returns all fish in aquarium."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species1 = await create_test_species(async_session, "guppy", "Guppy")
        species2 = await create_test_species(async_session, "neon", "Neon Tetra")
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species1.id)
        )
        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species2.id)
        )

        fish_list = await list_fish(async_session, aquarium.id, user.id)

        assert len(fish_list) == 2
        species_ids = [f.species_id for f in fish_list]
        assert "guppy" in species_ids
        assert "neon" in species_ids
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_fish_excludes_deleted_fish(async_session: AsyncSession):
    """Test that list_fish does not return soft-deleted fish."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        fish1 = await add_fish(
            async_session,
            aquarium.id,
            user.id,
            FishCreate(species_id=species.id, custom_name="Keep"),
        )
        fish2 = await add_fish(
            async_session,
            aquarium.id,
            user.id,
            FishCreate(species_id=species.id, custom_name="Delete"),
        )

        # Soft delete fish2
        await remove_fish(async_session, fish2.id, user.id)

        fish_list = await list_fish(async_session, aquarium.id, user.id)

        assert len(fish_list) == 1
        assert fish_list[0].id == fish1.id
        assert fish_list[0].custom_name == "Keep"
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_fish_eager_loads_species(async_session: AsyncSession):
    """Test that list_fish eagerly loads species relationship."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )

        fish_list = await list_fish(async_session, aquarium.id, user.id)

        assert len(fish_list) == 1
        assert fish_list[0].species is not None
        assert fish_list[0].species.common_name == "Guppy"
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_fish_sorted_by_created_at(async_session: AsyncSession):
    """Test that list_fish returns results sorted by created_at ASC."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session,
            aquarium.id,
            user.id,
            FishCreate(species_id=species.id, custom_name="First"),
        )
        await add_fish(
            async_session,
            aquarium.id,
            user.id,
            FishCreate(species_id=species.id, custom_name="Second"),
        )
        await add_fish(
            async_session,
            aquarium.id,
            user.id,
            FishCreate(species_id=species.id, custom_name="Third"),
        )

        fish_list = await list_fish(async_session, aquarium.id, user.id)

        assert fish_list[0].custom_name == "First"
        assert fish_list[1].custom_name == "Second"
        assert fish_list[2].custom_name == "Third"
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_fish_raises_403_for_non_member(async_session: AsyncSession):
    """Test that list_fish raises 403 for user without access."""
    await cleanup_fish_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner)

        with pytest.raises(AquariumAccessDeniedError) as exc_info:
            await list_fish(async_session, aquarium.id, other_user.id)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_fish_data(async_session)


# get_fish tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_fish_returns_fish(async_session: AsyncSession):
    """Test that get_fish returns the fish for authorized user."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        created_fish = await add_fish(
            async_session,
            aquarium.id,
            user.id,
            FishCreate(species_id=species.id, custom_name="My Fish"),
        )

        fish = await get_fish(async_session, created_fish.id, user.id)

        assert fish.id == created_fish.id
        assert fish.custom_name == "My Fish"
        assert fish.species is not None
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_fish_raises_404_for_nonexistent(async_session: AsyncSession):
    """Test that get_fish raises 404 for non-existent fish."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        random_id = uuid.uuid4()

        with pytest.raises(FishNotFoundError) as exc_info:
            await get_fish(async_session, random_id, user.id)

        assert exc_info.value.status_code == 404
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_fish_raises_404_for_deleted_fish(async_session: AsyncSession):
    """Test that get_fish raises 404 for soft-deleted fish."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        fish = await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )
        fish_id = fish.id

        await remove_fish(async_session, fish_id, user.id)

        with pytest.raises(FishNotFoundError) as exc_info:
            await get_fish(async_session, fish_id, user.id)

        assert exc_info.value.status_code == 404
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_fish_raises_403_for_non_member(async_session: AsyncSession):
    """Test that get_fish raises 403 for user without access."""
    await cleanup_fish_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, owner)

        fish = await add_fish(
            async_session, aquarium.id, owner.id, FishCreate(species_id=species.id)
        )

        with pytest.raises(AquariumAccessDeniedError) as exc_info:
            await get_fish(async_session, fish.id, other_user.id)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_fish_data(async_session)


# update_fish tests


@pytest.mark.asyncio(loop_scope="session")
async def test_update_fish_updates_quantity(async_session: AsyncSession):
    """Test that update_fish updates the quantity."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        fish = await add_fish(
            async_session,
            aquarium.id,
            user.id,
            FishCreate(species_id=species.id, quantity=1),
        )

        update_data = FishUpdate(quantity=10)
        updated_fish = await update_fish(async_session, fish.id, user.id, update_data)

        assert updated_fish.quantity == 10
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_fish_updates_custom_name(async_session: AsyncSession):
    """Test that update_fish updates the custom name."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        fish = await add_fish(
            async_session,
            aquarium.id,
            user.id,
            FishCreate(species_id=species.id, custom_name="Old Name"),
        )

        update_data = FishUpdate(custom_name="New Name")
        updated_fish = await update_fish(async_session, fish.id, user.id, update_data)

        assert updated_fish.custom_name == "New Name"
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_fish_partial_update(async_session: AsyncSession):
    """Test that update_fish only updates provided fields."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        fish = await add_fish(
            async_session,
            aquarium.id,
            user.id,
            FishCreate(species_id=species.id, quantity=5, custom_name="Original"),
        )

        # Update only quantity, custom_name should remain
        update_data = FishUpdate(quantity=10)
        updated_fish = await update_fish(async_session, fish.id, user.id, update_data)

        assert updated_fish.quantity == 10
        assert updated_fish.custom_name == "Original"
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_fish_raises_403_for_non_member(async_session: AsyncSession):
    """Test that update_fish raises 403 for user without access."""
    await cleanup_fish_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, owner)

        fish = await add_fish(
            async_session, aquarium.id, owner.id, FishCreate(species_id=species.id)
        )

        update_data = FishUpdate(quantity=100)
        with pytest.raises(AquariumAccessDeniedError) as exc_info:
            await update_fish(async_session, fish.id, other_user.id, update_data)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_fish_eager_loads_species(async_session: AsyncSession):
    """Test that update_fish returns fish with species loaded."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        fish = await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )

        update_data = FishUpdate(quantity=5)
        updated_fish = await update_fish(async_session, fish.id, user.id, update_data)

        assert updated_fish.species is not None
        assert updated_fish.species.common_name == "Guppy"
    finally:
        await cleanup_fish_data(async_session)


# remove_fish tests


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_fish_sets_deleted_at(async_session: AsyncSession):
    """Test that remove_fish sets deleted_at (soft delete)."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        fish = await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )
        fish_id = fish.id

        await remove_fish(async_session, fish_id, user.id)

        # Check directly in DB
        from sqlalchemy import select

        stmt = select(Fish).where(Fish.id == fish_id)
        result = await async_session.execute(stmt)
        deleted_fish = result.scalar_one()

        assert deleted_fish.deleted_at is not None
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_fish_does_not_physically_delete(async_session: AsyncSession):
    """Test that remove_fish does not physically delete the record."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        fish = await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )
        fish_id = fish.id

        await remove_fish(async_session, fish_id, user.id)

        # Record should still exist in DB
        from sqlalchemy import select

        stmt = select(Fish).where(Fish.id == fish_id)
        result = await async_session.execute(stmt)
        fish_record = result.scalar_one_or_none()

        assert fish_record is not None
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_fish_raises_404_for_nonexistent(async_session: AsyncSession):
    """Test that remove_fish raises 404 for non-existent fish."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        random_id = uuid.uuid4()

        with pytest.raises(FishNotFoundError) as exc_info:
            await remove_fish(async_session, random_id, user.id)

        assert exc_info.value.status_code == 404
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_fish_raises_403_for_non_member(async_session: AsyncSession):
    """Test that remove_fish raises 403 for user without access."""
    await cleanup_fish_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, owner)

        fish = await add_fish(
            async_session, aquarium.id, owner.id, FishCreate(species_id=species.id)
        )

        with pytest.raises(AquariumAccessDeniedError) as exc_info:
            await remove_fish(async_session, fish.id, other_user.id)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_fish_data(async_session)


# get_fish_by_species tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_fish_by_species_returns_fish(async_session: AsyncSession):
    """Test that get_fish_by_species returns fish if exists."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        created_fish = await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )

        fish = await get_fish_by_species(async_session, aquarium.id, species.id)

        assert fish is not None
        assert fish.id == created_fish.id
        assert fish.species is not None
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_fish_by_species_returns_none_if_not_exists(
    async_session: AsyncSession,
):
    """Test that get_fish_by_species returns None if no fish of species."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        fish = await get_fish_by_species(async_session, aquarium.id, "nonexistent")

        assert fish is None
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_fish_by_species_excludes_deleted_fish(async_session: AsyncSession):
    """Test that get_fish_by_species excludes soft-deleted fish."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        fish = await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )
        await remove_fish(async_session, fish.id, user.id)

        result = await get_fish_by_species(async_session, aquarium.id, species.id)

        assert result is None
    finally:
        await cleanup_fish_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_fish_by_species_only_searches_in_specified_aquarium(
    async_session: AsyncSession,
):
    """Test that get_fish_by_species only searches in specified aquarium."""
    await cleanup_fish_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium1 = await create_test_aquarium(async_session, user, "Aquarium 1")
        aquarium2 = await create_test_aquarium(async_session, user, "Aquarium 2")

        # Add fish only to aquarium1
        await add_fish(
            async_session, aquarium1.id, user.id, FishCreate(species_id=species.id)
        )

        # Search in aquarium2 should return None
        result = await get_fish_by_species(async_session, aquarium2.id, species.id)

        assert result is None
    finally:
        await cleanup_fish_data(async_session)
