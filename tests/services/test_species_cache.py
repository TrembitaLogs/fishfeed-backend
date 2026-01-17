"""Tests for Redis caching in species service."""

import json

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.species import Species
from app.schemas.species import SpeciesCreate, SpeciesSearchQuery, SpeciesUpdate
from app.services.species import (
    create_species,
    delete_species,
    get_popular_species,
    get_species_cached,
    invalidate_species_cache,
    list_species,
    search_species,
    update_species,
)
from app.utils.cache import (
    CACHE_VERSION,
    species_detail_key,
    species_list_key,
    species_popular_key,
    species_search_key,
)


async def cleanup_species(session: AsyncSession) -> None:
    """Helper to cleanup species and related data."""
    await session.execute(text("TRUNCATE TABLE species CASCADE"))
    await session.commit()


async def cleanup_cache(redis: Redis) -> None:
    """Helper to cleanup cache."""
    await redis.flushdb()


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


# list_species cache tests


@pytest.mark.asyncio(loop_scope="session")
async def test_list_species_caches_result(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that list_species caches the result in Redis."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        await create_test_species(async_session, "cache-test-1", "Cache Fish 1")
        await create_test_species(async_session, "cache-test-2", "Cache Fish 2")

        # First call - should cache
        result1 = await list_species(async_session, page=1, per_page=10, redis=redis_client)

        # Check cache exists
        cache_key = species_list_key(1, 10, None, None)
        cached = await redis_client.get(cache_key)
        assert cached is not None

        # Verify cached data matches
        cached_data = json.loads(cached)
        assert cached_data["total"] == 2
        assert len(cached_data["items"]) == 2

        # Second call - should use cache
        result2 = await list_species(async_session, page=1, per_page=10, redis=redis_client)
        assert result1.total == result2.total
        assert len(result1.items) == len(result2.items)
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_species_different_params_different_cache_keys(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that different pagination params use different cache keys."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        for i in range(5):
            await create_test_species(async_session, f"species-{i}", f"Fish {i}")

        # Call with different params
        await list_species(async_session, page=1, per_page=2, redis=redis_client)
        await list_species(async_session, page=2, per_page=2, redis=redis_client)
        await list_species(async_session, page=1, per_page=5, redis=redis_client)

        # All should be cached with different keys
        key1 = species_list_key(1, 2, None, None)
        key2 = species_list_key(2, 2, None, None)
        key3 = species_list_key(1, 5, None, None)

        assert await redis_client.exists(key1) == 1
        assert await redis_client.exists(key2) == 1
        assert await redis_client.exists(key3) == 1
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_species_filters_affect_cache_key(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that filters create different cache keys."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        await create_test_species(async_session, "species-1", "Fish 1", care_level="beginner")
        await create_test_species(async_session, "species-2", "Fish 2", care_level="advanced")

        filters1 = SpeciesSearchQuery(care_level="beginner")
        filters2 = SpeciesSearchQuery(care_level="advanced")

        await list_species(async_session, filters=filters1, redis=redis_client)
        await list_species(async_session, filters=filters2, redis=redis_client)

        key1 = species_list_key(1, 20, "beginner", None)
        key2 = species_list_key(1, 20, "advanced", None)

        assert await redis_client.exists(key1) == 1
        assert await redis_client.exists(key2) == 1
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


# get_species_cached tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_species_cached_caches_result(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that get_species_cached caches the result."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        await create_test_species(async_session, "cached-species", "Cached Fish")

        result = await get_species_cached(async_session, "cached-species", redis=redis_client)

        cache_key = species_detail_key("cached-species")
        cached = await redis_client.get(cache_key)
        assert cached is not None

        cached_data = json.loads(cached)
        assert cached_data["id"] == "cached-species"
        assert cached_data["common_name"] == "Cached Fish"
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_species_cached_returns_from_cache(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that get_species_cached returns cached data without DB query."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        await create_test_species(async_session, "cache-return", "Cache Return Fish")

        # First call populates cache
        await get_species_cached(async_session, "cache-return", redis=redis_client)

        # Delete from DB
        await async_session.execute(text("DELETE FROM species WHERE id = 'cache-return'"))
        await async_session.commit()

        # Second call should return from cache (species no longer in DB)
        result = await get_species_cached(async_session, "cache-return", redis=redis_client)
        assert result.id == "cache-return"
        assert result.common_name == "Cache Return Fish"
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


# search_species cache tests


@pytest.mark.asyncio(loop_scope="session")
async def test_search_species_caches_result(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that search_species caches the result."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        await create_test_species(async_session, "guppy", "Guppy", "Poecilia reticulata")

        result = await search_species(async_session, "guppy", redis=redis_client)

        cache_key = species_search_key("guppy")
        cached = await redis_client.get(cache_key)
        assert cached is not None
        assert len(result) >= 1
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_search_species_case_insensitive_cache_key(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that search cache key is case-insensitive."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        await create_test_species(async_session, "betta", "Betta")

        # Search with different cases
        await search_species(async_session, "BETTA", redis=redis_client)

        # Key should be lowercase
        cache_key = species_search_key("betta")
        cached = await redis_client.get(cache_key)
        assert cached is not None
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


# get_popular_species cache tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_popular_species_caches_result(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that get_popular_species caches the result."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        await create_test_species(async_session, "betta", "Betta")
        await create_test_species(async_session, "guppy", "Guppy")

        result = await get_popular_species(async_session, redis=redis_client)

        cache_key = species_popular_key()
        cached = await redis_client.get(cache_key)
        assert cached is not None
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


# Cache invalidation tests


@pytest.mark.asyncio(loop_scope="session")
async def test_create_species_invalidates_cache(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that create_species invalidates the species cache."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        await create_test_species(async_session, "existing", "Existing Fish")

        # Populate cache
        await list_species(async_session, redis=redis_client)
        await get_popular_species(async_session, redis=redis_client)

        # Verify cache exists
        list_key = species_list_key(1, 20, None, None)
        assert await redis_client.exists(list_key) == 1

        # Create new species with cache invalidation
        data = SpeciesCreate(
            id="new-species",
            common_name="New Fish",
            food_types=["flakes"],
        )
        await create_species(async_session, data, redis=redis_client)

        # Cache should be invalidated
        assert await redis_client.exists(list_key) == 0
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_species_invalidates_cache(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that update_species invalidates the species cache."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        await create_test_species(async_session, "update-cache", "Update Cache Fish")

        # Populate cache
        await get_species_cached(async_session, "update-cache", redis=redis_client)
        await list_species(async_session, redis=redis_client)

        detail_key = species_detail_key("update-cache")
        list_key = species_list_key(1, 20, None, None)
        assert await redis_client.exists(detail_key) == 1
        assert await redis_client.exists(list_key) == 1

        # Update species
        data = SpeciesUpdate(common_name="Updated Name")
        await update_species(async_session, "update-cache", data, redis=redis_client)

        # Cache should be invalidated
        assert await redis_client.exists(detail_key) == 0
        assert await redis_client.exists(list_key) == 0
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_delete_species_invalidates_cache(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that delete_species invalidates all species cache."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        await create_test_species(async_session, "delete-cache", "Delete Cache Fish")
        await create_test_species(async_session, "betta", "Betta")

        # Populate various caches
        await get_species_cached(async_session, "delete-cache", redis=redis_client)
        await list_species(async_session, redis=redis_client)
        await get_popular_species(async_session, redis=redis_client)
        await search_species(async_session, "fish", redis=redis_client)

        # Delete species
        await delete_species(async_session, "delete-cache", redis=redis_client)

        # All species cache should be invalidated
        detail_key = species_detail_key("delete-cache")
        list_key = species_list_key(1, 20, None, None)
        popular_key = species_popular_key()

        assert await redis_client.exists(detail_key) == 0
        assert await redis_client.exists(list_key) == 0
        assert await redis_client.exists(popular_key) == 0
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_invalidate_species_cache_all(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that invalidate_species_cache with None invalidates all."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        await create_test_species(async_session, "betta", "Betta")
        await create_test_species(async_session, "guppy", "Guppy")

        # Populate caches
        await list_species(async_session, redis=redis_client)
        await get_species_cached(async_session, "betta", redis=redis_client)
        await get_popular_species(async_session, redis=redis_client)

        # Invalidate all
        await invalidate_species_cache(redis_client, species_id=None)

        # All should be gone
        pattern = f"species:{CACHE_VERSION}:*"
        cursor, keys = await redis_client.scan(0, match=pattern)
        assert len(keys) == 0
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


# Cache versioning tests


@pytest.mark.asyncio(loop_scope="session")
async def test_cache_keys_contain_version(
    async_session: AsyncSession, redis_client: Redis
):
    """Test that cache keys contain the version prefix."""
    await cleanup_species(async_session)
    await cleanup_cache(redis_client)
    try:
        await create_test_species(async_session, "version-test", "Version Test Fish")

        await list_species(async_session, redis=redis_client)
        await get_species_cached(async_session, "version-test", redis=redis_client)

        # Check that keys contain version
        pattern = f"species:{CACHE_VERSION}:*"
        cursor, keys = await redis_client.scan(0, match=pattern)
        assert len(keys) >= 2

        for key in keys:
            assert f"species:{CACHE_VERSION}:" in key
    finally:
        await cleanup_species(async_session)
        await cleanup_cache(redis_client)


# Redis fallback tests


@pytest.mark.asyncio(loop_scope="session")
async def test_list_species_works_without_redis(async_session: AsyncSession):
    """Test that list_species works when redis is None."""
    await cleanup_species(async_session)
    try:
        await create_test_species(async_session, "no-redis", "No Redis Fish")

        # Call without redis
        result = await list_species(async_session, redis=None)

        assert result.total == 1
        assert result.items[0].common_name == "No Redis Fish"
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_species_cached_works_without_redis(async_session: AsyncSession):
    """Test that get_species_cached works when redis is None."""
    await cleanup_species(async_session)
    try:
        await create_test_species(async_session, "no-redis", "No Redis Fish")

        result = await get_species_cached(async_session, "no-redis", redis=None)

        assert result.id == "no-redis"
        assert result.common_name == "No Redis Fish"
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_search_species_works_without_redis(async_session: AsyncSession):
    """Test that search_species works when redis is None."""
    await cleanup_species(async_session)
    try:
        await create_test_species(async_session, "no-redis", "No Redis Fish")

        result = await search_species(async_session, "redis", redis=None)

        assert len(result) >= 1
    finally:
        await cleanup_species(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_species_works_without_redis(async_session: AsyncSession):
    """Test that create_species works when redis is None."""
    await cleanup_species(async_session)
    try:
        data = SpeciesCreate(
            id="no-redis-create",
            common_name="No Redis Create Fish",
            food_types=["flakes"],
        )

        species = await create_species(async_session, data, redis=None)

        assert species.id == "no-redis-create"
    finally:
        await cleanup_species(async_session)
