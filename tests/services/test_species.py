"""Integration tests for species service."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.species import Species
from app.schemas.species import SpeciesCreate, SpeciesSearchQuery, SpeciesUpdate
from app.services.species import (
    POPULAR_SPECIES_IDS,
    SpeciesAlreadyExistsError,
    SpeciesNotFoundError,
    create_species,
    delete_species,
    get_popular_species,
    get_species,
    list_species,
    search_species,
    update_species,
)


async def cleanup_species(session: AsyncSession) -> None:
    """Helper to cleanup species and related data."""
    await session.execute(text("TRUNCATE TABLE species CASCADE"))
    await session.commit()


async def create_test_species(
    session: AsyncSession,
    species_id: str,
    common_name: str,
    scientific_name: str | None = None,
    care_level: str = "beginner",
    water_type: str = "freshwater",
) -> Species:
    """Helper to create a test species."""
    species = Species(
        id=species_id,
        common_name=common_name,
        scientific_name=scientific_name,
        food_types=["flakes", "pellets"],
        feeding_frequency=2,
        care_level=care_level,
        water_type=water_type,
    )
    session.add(species)
    await session.commit()
    await session.refresh(species)
    return species


# list_species tests


@pytest.mark.asyncio(loop_scope="session")
async def test_list_species_returns_paginated_list(async_session: AsyncSession):
    """Test that list_species returns a paginated list of species."""
    await cleanup_species(async_session)
    try:
        # Create test species
        await create_test_species(async_session, "species-1", "Alpha Fish")
        await create_test_species(async_session, "species-2", "Beta Fish")
        await create_test_species(async_session, "species-3", "Gamma Fish")

        result = await list_species(async_session, page=1, per_page=2)

        assert result.total == 3
        assert len(result.items) == 2
        assert result.page == 1
        assert result.per_page == 2
        assert result.pages == 2
        # Should be sorted by common_name
        assert result.items[0].common_name == "Alpha Fish"
        assert result.items[1].common_name == "Beta Fish"
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_species_pagination_second_page(async_session: AsyncSession):
    """Test that list_species returns correct second page."""
    await cleanup_species(async_session)
    try:
        await create_test_species(async_session, "species-1", "Alpha Fish")
        await create_test_species(async_session, "species-2", "Beta Fish")
        await create_test_species(async_session, "species-3", "Gamma Fish")

        result = await list_species(async_session, page=2, per_page=2)

        assert result.total == 3
        assert len(result.items) == 1
        assert result.page == 2
        assert result.items[0].common_name == "Gamma Fish"
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_species_filter_by_care_level(async_session: AsyncSession):
    """Test that list_species filters by care_level."""
    await cleanup_species(async_session)
    try:
        await create_test_species(
            async_session, "species-1", "Easy Fish", care_level="beginner"
        )
        await create_test_species(
            async_session, "species-2", "Medium Fish", care_level="intermediate"
        )
        await create_test_species(
            async_session, "species-3", "Hard Fish", care_level="advanced"
        )

        filters = SpeciesSearchQuery(care_level="intermediate")
        result = await list_species(async_session, filters=filters)

        assert result.total == 1
        assert result.items[0].common_name == "Medium Fish"
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_species_filter_by_water_type(async_session: AsyncSession):
    """Test that list_species filters by water_type."""
    await cleanup_species(async_session)
    try:
        await create_test_species(
            async_session, "species-1", "Fresh Fish", water_type="freshwater"
        )
        await create_test_species(
            async_session, "species-2", "Salt Fish", water_type="saltwater"
        )

        filters = SpeciesSearchQuery(water_type="saltwater")
        result = await list_species(async_session, filters=filters)

        assert result.total == 1
        assert result.items[0].common_name == "Salt Fish"
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_species_empty_result(async_session: AsyncSession):
    """Test that list_species returns empty list when no species exist."""
    await cleanup_species(async_session)
    try:
        result = await list_species(async_session)

        assert result.total == 0
        assert len(result.items) == 0
        assert result.pages == 0
    finally:
        await cleanup_species(async_session)


# get_species tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_species_returns_species(async_session: AsyncSession):
    """Test that get_species returns the species by ID."""
    await cleanup_species(async_session)
    try:
        await create_test_species(async_session, "test-species", "Test Fish")

        species = await get_species(async_session, "test-species")

        assert species.id == "test-species"
        assert species.common_name == "Test Fish"
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_species_raises_not_found(async_session: AsyncSession):
    """Test that get_species raises SpeciesNotFoundError for non-existent ID."""
    await cleanup_species(async_session)
    try:
        with pytest.raises(SpeciesNotFoundError) as exc_info:
            await get_species(async_session, "non-existent-id")

        assert exc_info.value.status_code == 404
        assert "non-existent-id" in exc_info.value.message
    finally:
        await cleanup_species(async_session)


# search_species tests


@pytest.mark.asyncio(loop_scope="session")
async def test_search_species_finds_by_common_name(async_session: AsyncSession):
    """Test that search_species finds species by common name."""
    await cleanup_species(async_session)
    try:
        await create_test_species(async_session, "guppy", "Guppy", "Poecilia reticulata")
        await create_test_species(async_session, "betta", "Betta Fish", "Betta splendens")

        results = await search_species(async_session, "gup")

        assert len(results) >= 1
        assert any(s.common_name == "Guppy" for s in results)
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_search_species_finds_by_scientific_name(async_session: AsyncSession):
    """Test that search_species finds species by scientific name."""
    await cleanup_species(async_session)
    try:
        await create_test_species(
            async_session, "neon-tetra", "Neon Tetra", "Paracheirodon innesi"
        )
        await create_test_species(async_session, "betta", "Betta", "Betta splendens")

        results = await search_species(async_session, "paracheirodon")

        assert len(results) >= 1
        assert any(s.scientific_name == "Paracheirodon innesi" for s in results)
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_search_species_empty_query_returns_empty(async_session: AsyncSession):
    """Test that search_species returns empty list for empty query."""
    await cleanup_species(async_session)
    try:
        await create_test_species(async_session, "guppy", "Guppy")

        results = await search_species(async_session, "")

        assert len(results) == 0
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_search_species_no_matches_returns_empty(async_session: AsyncSession):
    """Test that search_species returns empty list when no matches."""
    await cleanup_species(async_session)
    try:
        await create_test_species(async_session, "guppy", "Guppy")

        results = await search_species(async_session, "nonexistent")

        assert len(results) == 0
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_search_species_respects_limit(async_session: AsyncSession):
    """Test that search_species respects the limit parameter."""
    await cleanup_species(async_session)
    try:
        for i in range(10):
            await create_test_species(async_session, f"fish-{i}", f"Fish Number {i}")

        results = await search_species(async_session, "fish", limit=5)

        assert len(results) <= 5
    finally:
        await cleanup_species(async_session)


# get_popular_species tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_popular_species_returns_popular_list(async_session: AsyncSession):
    """Test that get_popular_species returns species from popular list."""
    await cleanup_species(async_session)
    try:
        # Create some popular species
        await create_test_species(async_session, "betta", "Betta")
        await create_test_species(async_session, "guppy", "Guppy")
        await create_test_species(async_session, "goldfish", "Goldfish")
        # Create a non-popular species
        await create_test_species(async_session, "rare-fish", "Rare Fish")

        results = await get_popular_species(async_session)

        assert len(results) == 3  # Only the popular ones
        ids = [s.id for s in results]
        assert "betta" in ids
        assert "guppy" in ids
        assert "goldfish" in ids
        assert "rare-fish" not in ids
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_popular_species_respects_limit(async_session: AsyncSession):
    """Test that get_popular_species respects the limit parameter."""
    await cleanup_species(async_session)
    try:
        # Create multiple popular species
        for species_id in POPULAR_SPECIES_IDS[:10]:
            await create_test_species(async_session, species_id, species_id.title())

        results = await get_popular_species(async_session, limit=5)

        assert len(results) <= 5
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_popular_species_sorted_by_name(async_session: AsyncSession):
    """Test that get_popular_species returns results sorted by common_name."""
    await cleanup_species(async_session)
    try:
        await create_test_species(async_session, "guppy", "Guppy")
        await create_test_species(async_session, "betta", "Betta")
        await create_test_species(async_session, "angelfish", "Angelfish")

        results = await get_popular_species(async_session)

        # Should be sorted alphabetically
        names = [s.common_name for s in results]
        assert names == sorted(names)
    finally:
        await cleanup_species(async_session)


# create_species tests


@pytest.mark.asyncio(loop_scope="session")
async def test_create_species_creates_new_species(async_session: AsyncSession):
    """Test that create_species creates a new species in the database."""
    await cleanup_species(async_session)
    try:
        data = SpeciesCreate(
            id="new-species",
            common_name="New Fish",
            scientific_name="Novus piscis",
            food_types=["flakes"],
            feeding_frequency=2,
            care_level="beginner",
            water_type="freshwater",
        )

        species = await create_species(async_session, data)

        assert species.id == "new-species"
        assert species.common_name == "New Fish"
        assert species.scientific_name == "Novus piscis"
        assert species.care_level == "beginner"
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_species_raises_already_exists(async_session: AsyncSession):
    """Test that create_species raises error if species ID already exists."""
    await cleanup_species(async_session)
    try:
        await create_test_species(async_session, "existing", "Existing Fish")

        data = SpeciesCreate(
            id="existing",
            common_name="Another Fish",
            food_types=["flakes"],
        )

        with pytest.raises(SpeciesAlreadyExistsError) as exc_info:
            await create_species(async_session, data)

        assert exc_info.value.status_code == 409
        assert "existing" in exc_info.value.message
    finally:
        await cleanup_species(async_session)


# update_species tests


@pytest.mark.asyncio(loop_scope="session")
async def test_update_species_updates_fields(async_session: AsyncSession):
    """Test that update_species updates the specified fields."""
    await cleanup_species(async_session)
    try:
        await create_test_species(async_session, "update-test", "Original Name")

        data = SpeciesUpdate(common_name="Updated Name", care_level="advanced")
        species = await update_species(async_session, "update-test", data)

        assert species.common_name == "Updated Name"
        assert species.care_level == "advanced"
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_species_partial_update(async_session: AsyncSession):
    """Test that update_species only updates provided fields."""
    await cleanup_species(async_session)
    try:
        await create_test_species(
            async_session,
            "partial-update",
            "Original Name",
            scientific_name="Original Scientific",
        )

        data = SpeciesUpdate(common_name="New Name")
        species = await update_species(async_session, "partial-update", data)

        assert species.common_name == "New Name"
        assert species.scientific_name == "Original Scientific"  # Unchanged
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_species_raises_not_found(async_session: AsyncSession):
    """Test that update_species raises error for non-existent species."""
    await cleanup_species(async_session)
    try:
        data = SpeciesUpdate(common_name="New Name")

        with pytest.raises(SpeciesNotFoundError):
            await update_species(async_session, "non-existent", data)
    finally:
        await cleanup_species(async_session)


# delete_species tests


@pytest.mark.asyncio(loop_scope="session")
async def test_delete_species_removes_from_db(async_session: AsyncSession):
    """Test that delete_species removes the species from database."""
    await cleanup_species(async_session)
    try:
        await create_test_species(async_session, "to-delete", "Delete Me")

        await delete_species(async_session, "to-delete")

        with pytest.raises(SpeciesNotFoundError):
            await get_species(async_session, "to-delete")
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_delete_species_raises_not_found(async_session: AsyncSession):
    """Test that delete_species raises error for non-existent species."""
    await cleanup_species(async_session)
    try:
        with pytest.raises(SpeciesNotFoundError):
            await delete_species(async_session, "non-existent")
    finally:
        await cleanup_species(async_session)
