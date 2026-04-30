"""Species service with business logic for fish species management."""

import json
from datetime import datetime

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.models.species import Species
from app.schemas.species import (
    SpeciesCreate,
    SpeciesListResponse,
    SpeciesResponse,
    SpeciesSearchQuery,
    SpeciesUpdate,
)
from app.services.image_service import batch_generate_presigned_urls
from app.utils.cache import (
    TTL_SPECIES_DETAIL,
    TTL_SPECIES_LIST,
    TTL_SPECIES_POPULAR,
    TTL_SPECIES_SEARCH,
    species_cache_pattern,
    species_detail_key,
    species_list_key,
    species_list_pattern,
    species_popular_key,
    species_popular_pattern,
    species_search_key,
)

logger = structlog.get_logger(__name__)


class SpeciesError(AppError):
    """Base class for species errors. Subclass per concrete failure mode."""


class SpeciesNotFoundError(SpeciesError):
    """Raised when species is not found."""

    def __init__(self, species_id: str):
        super().__init__(
            ErrorCode.SPECIES_NOT_FOUND,
            f"Species with id '{species_id}' not found",
            status_code=404,
        )


class SpeciesAlreadyExistsError(SpeciesError):
    """Raised when species with given ID already exists."""

    def __init__(self, species_id: str):
        super().__init__(
            ErrorCode.SPECIES_ALREADY_EXISTS,
            f"Species with id '{species_id}' already exists",
            status_code=409,
        )


# Popular species IDs for onboarding
POPULAR_SPECIES_IDS = [
    "betta",
    "guppy",
    "goldfish",
    "neon-tetra",
    "angelfish",
    "molly",
    "platy",
    "corydoras",
    "zebra-danio",
    "cherry-barb",
    "swordtail",
    "pleco",
    "ram-cichlid",
    "discus",
    "oscar",
    "kuhli-loach",
    "bristlenose-pleco",
    "cardinal-tetra",
    "harlequin-rasbora",
    "dwarf-gourami",
]


def _species_public_cdn_url(key: str, updated_at: datetime, cdn_domain: str) -> str:
    """Build a public CDN URL for a species image with cache-buster.

    The cache-buster (`?v=<unix_ts>`) is the species' `updated_at` timestamp so
    Cloudflare's edge cache invalidates whenever an admin updates the photo.
    """
    return f"https://{cdn_domain}/{key}?v={int(updated_at.timestamp())}"


async def _resolve_species_image_urls(
    species_list: list[SpeciesResponse],
) -> list[SpeciesResponse]:
    """Replace S3 keys in image_url with viewable URLs.

    Species images are stored as S3 keys (e.g., 'species/betta/photo.webp')
    in the dedicated `S3_SPECIES_BUCKET_NAME` bucket. Resolution strategy:

    - If `S3_PUBLIC_CDN_DOMAIN` is configured (production), build a public CDN
      URL directly. The CDN is mapped to the species bucket and serves objects
      without signing — no expiration, edge-cached, no AccessDenied 403s from
      cross-bucket presigning. Cache-buster `?v=<updated_at>` invalidates on
      photo updates.
    - Otherwise (dev / unconfigured CDN), fall back to presigned URLs. Note
      that the legacy presigner targets `S3_IMAGES_BUCKET_NAME` and only
      works in setups where species objects live there too.

    Args:
        species_list: List of SpeciesResponse with S3 keys in image_url.

    Returns:
        List of SpeciesResponse with viewable URLs in image_url.
    """
    keyed = [s for s in species_list if s.image_url]
    if not keyed:
        return species_list

    settings = get_settings()
    cdn_domain = settings.S3_PUBLIC_CDN_DOMAIN

    if cdn_domain:
        url_map: dict[str, str] = {
            s.image_url: _species_public_cdn_url(s.image_url, s.updated_at, cdn_domain)
            for s in keyed
            if s.image_url is not None
        }
    else:
        keys = [s.image_url for s in keyed if s.image_url]
        try:
            url_map = await batch_generate_presigned_urls(keys)
        except Exception:
            logger.warning("failed_to_generate_species_image_urls", count=len(keys))
            return species_list

    return [
        s.model_copy(update={"image_url": url_map[s.image_url]})
        if s.image_url and s.image_url in url_map
        else s
        for s in species_list
    ]


async def list_species(
    db: AsyncSession,
    page: int = 1,
    per_page: int = 20,
    filters: SpeciesSearchQuery | None = None,
    redis: Redis | None = None,
) -> SpeciesListResponse:
    """List species with pagination and optional filters.

    Args:
        db: Database session.
        page: Page number (1-indexed).
        per_page: Number of items per page.
        filters: Optional filters for care_level and water_type.
        redis: Optional Redis client for caching.

    Returns:
        SpeciesListResponse with paginated results.
    """
    care_level = filters.care_level if filters else None
    water_type = filters.water_type if filters else None
    cache_key = species_list_key(page, per_page, care_level, water_type)

    # Try to get from cache
    if redis is not None:
        try:
            cached = await redis.get(cache_key)
            if cached:
                logger.debug("Cache hit", cache_key=cache_key)
                response = SpeciesListResponse.model_validate_json(cached)
                response.items = await _resolve_species_image_urls(response.items)
                return response
        except RedisError as e:
            logger.warning("Redis error on get", error=str(e))

    # Query database
    stmt = select(Species)

    if filters:
        if filters.care_level:
            stmt = stmt.where(Species.care_level == filters.care_level)
        if filters.water_type:
            stmt = stmt.where(Species.water_type == filters.water_type)

    stmt = stmt.order_by(Species.common_name)

    # Count total
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    # Apply pagination
    offset = (page - 1) * per_page
    stmt = stmt.offset(offset).limit(per_page)

    result = await db.execute(stmt)
    species_list = result.scalars().all()

    response = SpeciesListResponse(
        items=[SpeciesResponse.model_validate(s) for s in species_list],
        total=total,
        page=page,
        per_page=per_page,
    )

    # Store in cache (with S3 keys, not presigned URLs)
    if redis is not None:
        try:
            await redis.set(cache_key, response.model_dump_json(), ex=TTL_SPECIES_LIST)
            logger.debug("Cached species list", cache_key=cache_key, ttl=TTL_SPECIES_LIST)
        except RedisError as e:
            logger.warning("Redis error on set", error=str(e))

    # Resolve S3 keys to presigned URLs before returning
    response.items = await _resolve_species_image_urls(response.items)
    return response


async def get_species(db: AsyncSession, species_id: str) -> Species:
    """Get a species by ID.

    Args:
        db: Database session.
        species_id: Species ID.

    Returns:
        Species object.

    Raises:
        SpeciesNotFoundError: If species not found.
    """
    stmt = select(Species).where(Species.id == species_id)
    result = await db.execute(stmt)
    species = result.scalar_one_or_none()

    if species is None:
        raise SpeciesNotFoundError(species_id)

    return species


async def get_species_cached(
    db: AsyncSession,
    species_id: str,
    redis: Redis | None = None,
) -> SpeciesResponse:
    """Get a species by ID with caching support.

    Args:
        db: Database session.
        species_id: Species ID.
        redis: Optional Redis client for caching.

    Returns:
        SpeciesResponse object.

    Raises:
        SpeciesNotFoundError: If species not found.
    """
    cache_key = species_detail_key(species_id)

    # Try to get from cache
    if redis is not None:
        try:
            cached = await redis.get(cache_key)
            if cached:
                logger.debug("Cache hit", cache_key=cache_key)
                response = SpeciesResponse.model_validate_json(cached)
                resolved = await _resolve_species_image_urls([response])
                return resolved[0]
        except RedisError as e:
            logger.warning("Redis error on get", error=str(e))

    # Query database
    species = await get_species(db, species_id)
    response = SpeciesResponse.model_validate(species)

    # Store in cache (with S3 keys, not presigned URLs)
    if redis is not None:
        try:
            await redis.set(cache_key, response.model_dump_json(), ex=TTL_SPECIES_DETAIL)
            logger.debug("Cached species detail", cache_key=cache_key, ttl=TTL_SPECIES_DETAIL)
        except RedisError as e:
            logger.warning("Redis error on set", error=str(e))

    # Resolve S3 keys to presigned URLs before returning
    resolved = await _resolve_species_image_urls([response])
    return resolved[0]


async def search_species(
    db: AsyncSession,
    query: str,
    limit: int = 20,
    redis: Redis | None = None,
) -> list[SpeciesResponse]:
    """Search species by name using full-text search with ILIKE fallback.

    Uses PostgreSQL full-text search on common_name and scientific_name.
    Falls back to ILIKE if FTS returns no results.

    Args:
        db: Database session.
        query: Search query string.
        limit: Maximum number of results.
        redis: Optional Redis client for caching.

    Returns:
        List of matching SpeciesResponse objects.
    """
    # Try full-text search first
    search_query = query.strip()
    if not search_query:
        return []

    cache_key = species_search_key(search_query)

    # Try to get from cache
    if redis is not None:
        try:
            cached = await redis.get(cache_key)
            if cached:
                logger.debug("Cache hit", cache_key=cache_key)
                cached_list = json.loads(cached)
                response_list = [SpeciesResponse.model_validate(item) for item in cached_list]
                return await _resolve_species_image_urls(response_list)
        except RedisError as e:
            logger.warning("Redis error on get", error=str(e))

    # Use plainto_tsquery for safe full-text search (no raw tsquery syntax injection).
    # websearch_to_tsquery handles prefix matching via :* through a separate safe step.
    tsvector_expr = func.to_tsvector(
        "english",
        func.coalesce(Species.common_name, "") + " " + func.coalesce(Species.scientific_name, ""),
    )
    tsquery_expr = func.websearch_to_tsquery("english", search_query)

    fts_stmt = (
        select(Species)
        .where(tsvector_expr.bool_op("@@")(tsquery_expr))
        .order_by(func.ts_rank(tsvector_expr, tsquery_expr).desc())
        .limit(limit)
    )

    result = await db.execute(fts_stmt)
    species_list = result.scalars().all()

    # Fallback to ILIKE if FTS returns no results
    if not species_list:
        ilike_pattern = f"%{search_query}%"
        ilike_stmt = (
            select(Species)
            .where(
                or_(
                    Species.common_name.ilike(ilike_pattern),
                    Species.scientific_name.ilike(ilike_pattern),
                )
            )
            .order_by(Species.common_name)
            .limit(limit)
        )
        result = await db.execute(ilike_stmt)
        species_list = result.scalars().all()

    response_list = [SpeciesResponse.model_validate(s) for s in species_list]

    # Store in cache (with S3 keys, not presigned URLs)
    if redis is not None:
        try:
            cache_data = json.dumps([r.model_dump(mode="json") for r in response_list])
            await redis.set(cache_key, cache_data, ex=TTL_SPECIES_SEARCH)
            logger.debug("Cached search results", cache_key=cache_key, ttl=TTL_SPECIES_SEARCH)
        except RedisError as e:
            logger.warning("Redis error on set", error=str(e))

    # Resolve S3 keys to presigned URLs before returning
    return await _resolve_species_image_urls(response_list)


async def get_popular_species(
    db: AsyncSession,
    limit: int = 20,
    redis: Redis | None = None,
) -> list[SpeciesResponse]:
    """Get popular species for onboarding.

    Returns species from the predefined popular list, ordered by common_name.

    Args:
        db: Database session.
        limit: Maximum number of results.
        redis: Optional Redis client for caching.

    Returns:
        List of popular SpeciesResponse objects.
    """
    cache_key = species_popular_key()

    # Try to get from cache
    if redis is not None:
        try:
            cached = await redis.get(cache_key)
            if cached:
                logger.debug("Cache hit", cache_key=cache_key)
                cached_list = json.loads(cached)
                response_list = [SpeciesResponse.model_validate(item) for item in cached_list]
                return await _resolve_species_image_urls(response_list)
        except RedisError as e:
            logger.warning("Redis error on get", error=str(e))

    # Query database
    stmt = (
        select(Species)
        .where(Species.id.in_(POPULAR_SPECIES_IDS))
        .order_by(Species.common_name)
        .limit(limit)
    )

    result = await db.execute(stmt)
    species_list = list(result.scalars().all())
    response_list = [SpeciesResponse.model_validate(s) for s in species_list]

    # Store in cache (with S3 keys, not presigned URLs)
    if redis is not None:
        try:
            cache_data = json.dumps([r.model_dump(mode="json") for r in response_list])
            await redis.set(cache_key, cache_data, ex=TTL_SPECIES_POPULAR)
            logger.debug("Cached popular species", cache_key=cache_key, ttl=TTL_SPECIES_POPULAR)
        except RedisError as e:
            logger.warning("Redis error on set", error=str(e))

    # Resolve S3 keys to presigned URLs before returning
    return await _resolve_species_image_urls(response_list)


async def invalidate_species_cache(
    redis: Redis,
    species_id: str | None = None,
) -> None:
    """Invalidate species cache entries.

    Args:
        redis: Redis client.
        species_id: Optional species ID. If None, invalidates all species cache.
            If provided, invalidates detail cache for that species plus list and popular.
    """
    try:
        if species_id is None:
            # Invalidate all species cache
            pattern = species_cache_pattern()
            cursor = 0
            while True:
                cursor, keys = await redis.scan(cursor, match=pattern, count=100)
                if keys:
                    await redis.delete(*keys)
                    logger.debug("Invalidated cache keys", key_count=len(keys), pattern=pattern)
                if cursor == 0:
                    break
        else:
            # Invalidate specific species detail
            detail_key = species_detail_key(species_id)
            await redis.delete(detail_key)
            logger.debug("Invalidated cache key", cache_key=detail_key)

            # Invalidate list and popular caches (they may contain this species)
            for pattern in [species_list_pattern(), species_popular_pattern()]:
                cursor = 0
                while True:
                    cursor, keys = await redis.scan(cursor, match=pattern, count=100)
                    if keys:
                        await redis.delete(*keys)
                        logger.debug("Invalidated cache keys", key_count=len(keys), pattern=pattern)
                    if cursor == 0:
                        break
    except RedisError as e:
        logger.warning("Redis error during cache invalidation", error=str(e))


async def create_species(
    db: AsyncSession,
    data: SpeciesCreate,
    redis: Redis | None = None,
) -> Species:
    """Create a new species.

    Args:
        db: Database session.
        data: Species creation data.
        redis: Optional Redis client for cache invalidation.

    Returns:
        Created Species object.

    Raises:
        SpeciesAlreadyExistsError: If species with ID already exists.
    """
    # Check if species already exists
    existing = await db.execute(select(Species).where(Species.id == data.id))
    if existing.scalar_one_or_none() is not None:
        raise SpeciesAlreadyExistsError(data.id)

    species = Species(
        id=data.id,
        common_name=data.common_name,
        scientific_name=data.scientific_name,
        image_url=data.image_url,
        food_types=data.food_types,
        feeding_frequency=data.feeding_frequency,
        portion_hint=data.portion_hint,
        care_level=data.care_level,
        water_type=data.water_type,
        metadata_=data.metadata or {},
    )

    db.add(species)
    await db.flush()
    await db.refresh(species)

    # Invalidate list and popular caches
    if redis is not None:
        await invalidate_species_cache(redis, species_id=None)

    return species


async def update_species(
    db: AsyncSession,
    species_id: str,
    data: SpeciesUpdate,
    redis: Redis | None = None,
) -> Species:
    """Update an existing species.

    Args:
        db: Database session.
        species_id: Species ID to update.
        data: Partial update data.
        redis: Optional Redis client for cache invalidation.

    Returns:
        Updated Species object.

    Raises:
        SpeciesNotFoundError: If species not found.
    """
    species = await get_species(db, species_id)

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "metadata":
            species.metadata_ = value
        else:
            setattr(species, field, value)

    await db.flush()
    await db.refresh(species)

    # Invalidate detail, list, and popular caches
    if redis is not None:
        await invalidate_species_cache(redis, species_id=species_id)

    return species


async def delete_species(
    db: AsyncSession,
    species_id: str,
    redis: Redis | None = None,
) -> None:
    """Delete a species.

    Args:
        db: Database session.
        species_id: Species ID to delete.
        redis: Optional Redis client for cache invalidation.

    Raises:
        SpeciesNotFoundError: If species not found.
    """
    species = await get_species(db, species_id)
    await db.delete(species)
    await db.flush()

    # Invalidate all species cache
    if redis is not None:
        await invalidate_species_cache(redis, species_id=None)
