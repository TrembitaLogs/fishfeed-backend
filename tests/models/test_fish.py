import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Aquarium, Fish, Species, User


@pytest.mark.asyncio(loop_scope="session")
async def test_fish_creation(async_session: AsyncSession):
    """Test Fish creation with UUID primary key."""
    user = User(email="fish_owner@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Fish Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(id="goldfish", common_name="Goldfish")
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    fish = Fish(
        aquarium_id=aquarium.id,
        species_id=species.id,
        quantity=3,
        custom_name="Nemo",
    )
    async_session.add(fish)
    await async_session.commit()
    await async_session.refresh(fish)

    assert isinstance(fish.id, uuid.UUID)
    assert fish.aquarium_id == aquarium.id
    assert fish.species_id == species.id
    assert fish.quantity == 3
    assert fish.custom_name == "Nemo"


@pytest.mark.asyncio(loop_scope="session")
async def test_fish_has_timestamp_mixin(async_session: AsyncSession):
    """Test that Fish has TimestampMixin fields."""
    user = User(email="fish_ts@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Timestamp Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(id="fish-ts-species", common_name="Test Fish")
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()
    await async_session.refresh(fish)

    assert fish.created_at is not None
    assert fish.updated_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_fish_has_soft_delete_mixin(async_session: AsyncSession):
    """Test that Fish has SoftDeleteMixin functionality."""
    user = User(email="fish_sd@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="SoftDelete Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(id="fish-sd-species", common_name="Test Fish")
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()
    await async_session.refresh(fish)

    assert fish.deleted_at is None
    assert fish.is_deleted() is False


@pytest.mark.asyncio(loop_scope="session")
async def test_fish_default_values(async_session: AsyncSession):
    """Test Fish default values."""
    user = User(email="fish_defaults@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Defaults Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(id="fish-defaults-species", common_name="Test Fish")
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()
    await async_session.refresh(fish)

    assert fish.quantity == 1
    assert fish.added_via == "manual"
    assert fish.custom_name is None


@pytest.mark.asyncio(loop_scope="session")
async def test_fish_aquarium_relationship(async_session: AsyncSession):
    """Test Fish to Aquarium relationship."""
    user = User(email="fish_aquarium_rel@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="My Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(id="fish-aquarium-rel-species", common_name="Guppy")
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()

    result = await async_session.execute(select(Fish).where(Fish.id == fish.id))
    loaded_fish = result.scalar_one()
    await async_session.refresh(loaded_fish, ["aquarium"])

    assert loaded_fish.aquarium.id == aquarium.id
    assert loaded_fish.aquarium.name == "My Tank"


@pytest.mark.asyncio(loop_scope="session")
async def test_fish_species_relationship(async_session: AsyncSession):
    """Test Fish to Species relationship."""
    user = User(email="fish_species_rel@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Species Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(id="neon-tetra-rel", common_name="Neon Tetra")
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()

    result = await async_session.execute(select(Fish).where(Fish.id == fish.id))
    loaded_fish = result.scalar_one()
    await async_session.refresh(loaded_fish, ["species"])

    assert loaded_fish.species.id == "neon-tetra-rel"
    assert loaded_fish.species.common_name == "Neon Tetra"


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_fish_relationship(async_session: AsyncSession):
    """Test Aquarium to Fish relationship."""
    user = User(email="aquarium_fish_rel@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Multi Fish Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(id="multi-fish-species", common_name="Guppy")
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    fish1 = Fish(aquarium_id=aquarium.id, species_id=species.id, custom_name="Fish1")
    fish2 = Fish(aquarium_id=aquarium.id, species_id=species.id, custom_name="Fish2")
    async_session.add_all([fish1, fish2])
    await async_session.commit()

    result = await async_session.execute(
        select(Aquarium).where(Aquarium.id == aquarium.id)
    )
    loaded_aquarium = result.scalar_one()
    await async_session.refresh(loaded_aquarium, ["fish"])

    assert len(loaded_aquarium.fish) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_fish_cascade_delete_from_aquarium(async_session: AsyncSession):
    """Test that Fish is deleted when Aquarium is deleted."""
    user = User(email="fish_cascade@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Cascade Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(id="cascade-fish-species", common_name="Guppy")
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()

    fish_id = fish.id
    await async_session.delete(aquarium)
    await async_session.commit()

    result = await async_session.execute(select(Fish).where(Fish.id == fish_id))
    assert result.scalar_one_or_none() is None
