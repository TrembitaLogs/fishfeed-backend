"""Cache utilities for Redis caching with key generation and TTL management."""

import hashlib
import json
from typing import Any

# Cache version for global invalidation - increment to invalidate all cache
CACHE_VERSION = "v1"

# TTL constants in seconds
TTL_SPECIES_LIST = 3600  # 1 hour
TTL_SPECIES_DETAIL = 86400  # 24 hours
TTL_SPECIES_POPULAR = 3600  # 1 hour
TTL_SPECIES_SEARCH = 1800  # 30 minutes
TTL_USER_STATS = 60  # 1 minute - invalidated on feed/achievement actions
TTL_USER_ACHIEVEMENTS = 120  # 2 minutes
TTL_FEEDING_SCHEDULES = 300  # 5 minutes


def _hash_dict(data: dict[str, Any]) -> str:
    """Create a stable hash from a dictionary for cache key generation."""
    sorted_json = json.dumps(data, sort_keys=True, default=str)
    return hashlib.md5(sorted_json.encode()).hexdigest()[:12]


def species_list_key(
    page: int,
    per_page: int,
    care_level: str | None = None,
    water_type: str | None = None,
) -> str:
    """Generate cache key for species list endpoint.

    Args:
        page: Page number.
        per_page: Items per page.
        care_level: Optional care level filter.
        water_type: Optional water type filter.

    Returns:
        Cache key string.
    """
    filters_hash = _hash_dict({
        "care_level": care_level,
        "water_type": water_type,
    })
    return f"species:{CACHE_VERSION}:list:{page}:{per_page}:{filters_hash}"


def species_detail_key(species_id: str) -> str:
    """Generate cache key for species detail endpoint.

    Args:
        species_id: Species ID.

    Returns:
        Cache key string.
    """
    return f"species:{CACHE_VERSION}:detail:{species_id}"


def species_popular_key() -> str:
    """Generate cache key for popular species endpoint.

    Returns:
        Cache key string.
    """
    return f"species:{CACHE_VERSION}:popular"


def species_search_key(query: str) -> str:
    """Generate cache key for species search endpoint.

    Args:
        query: Search query string.

    Returns:
        Cache key string.
    """
    query_hash = hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
    return f"species:{CACHE_VERSION}:search:{query_hash}"


def species_cache_pattern() -> str:
    """Get pattern for all species cache keys (for bulk invalidation).

    Returns:
        Redis key pattern string.
    """
    return f"species:{CACHE_VERSION}:*"


def species_list_pattern() -> str:
    """Get pattern for species list cache keys.

    Returns:
        Redis key pattern string.
    """
    return f"species:{CACHE_VERSION}:list:*"


def species_popular_pattern() -> str:
    """Get pattern for species popular cache keys.

    Returns:
        Redis key pattern string.
    """
    return f"species:{CACHE_VERSION}:popular*"


# User stats / gamification cache keys


def user_stats_key(user_id: str) -> str:
    """Generate cache key for user gamification stats."""
    return f"user_stats:{CACHE_VERSION}:{user_id}"


def user_achievements_key(user_id: str) -> str:
    """Generate cache key for user achievements list."""
    return f"achievements:{CACHE_VERSION}:{user_id}"


def user_gamification_pattern(user_id: str) -> str:
    """Get pattern for all gamification cache keys for a user."""
    return f"*:{CACHE_VERSION}:{user_id}"


def invalidate_user_gamification_keys(user_id: str) -> list[str]:
    """Get list of cache keys to delete when user gamification data changes."""
    return [
        user_stats_key(user_id),
        user_achievements_key(user_id),
    ]


# Feeding schedule cache keys


def feeding_schedules_key(aquarium_id: str, active_only: bool = True) -> str:
    """Generate cache key for feeding schedules of an aquarium."""
    suffix = "active" if active_only else "all"
    return f"feeding_schedules:{CACHE_VERSION}:{aquarium_id}:{suffix}"


def feeding_schedules_pattern(aquarium_id: str) -> str:
    """Get pattern for all feeding schedule cache keys for an aquarium."""
    return f"feeding_schedules:{CACHE_VERSION}:{aquarium_id}:*"
