import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Species


def unique_id(prefix: str) -> str:
    """Generate a unique species ID for testing."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio(loop_scope="session")
async def test_species_creation_with_slug_pk(async_session: AsyncSession):
    """Test that Species uses a string slug as primary key."""
    species_id = unique_id("goldfish")
    species = Species(
        id=species_id,
        common_name="Goldfish",
        scientific_name="Carassius auratus",
    )
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    assert species.id == species_id
    assert species.common_name == "Goldfish"
    assert species.scientific_name == "Carassius auratus"


@pytest.mark.asyncio(loop_scope="session")
async def test_species_has_timestamp_mixin(async_session: AsyncSession):
    """Test that Species has TimestampMixin fields."""
    species = Species(
        id=unique_id("betta"),
        common_name="Betta",
    )
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    assert species.created_at is not None
    assert species.updated_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_species_food_types_jsonb(async_session: AsyncSession):
    """Test that Species food_types accepts JSONB data."""
    food_types = ["flakes", "pellets", "frozen brine shrimp"]
    species = Species(
        id=unique_id("guppy"),
        common_name="Guppy",
        food_types=food_types,
    )
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    assert species.food_types == food_types


@pytest.mark.asyncio(loop_scope="session")
async def test_species_metadata_jsonb(async_session: AsyncSession):
    """Test that Species metadata accepts JSONB data."""
    metadata = {
        "lifespan_years": 5,
        "temperature_range": {"min": 20, "max": 28},
        "compatible_species": ["tetra", "corydoras"],
    }
    species = Species(
        id=unique_id("angelfish"),
        common_name="Angelfish",
        metadata_=metadata,
    )
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    assert species.metadata_ == metadata


@pytest.mark.asyncio(loop_scope="session")
async def test_species_default_values(async_session: AsyncSession):
    """Test Species default values for optional fields."""
    species = Species(
        id=unique_id("neon-tetra"),
        common_name="Neon Tetra",
    )
    async_session.add(species)
    await async_session.commit()
    await async_session.refresh(species)

    assert species.food_types == []
    assert species.feeding_frequency == 2
    assert species.care_level == "beginner"
    assert species.water_type == "freshwater"
    assert species.metadata_ == {}
