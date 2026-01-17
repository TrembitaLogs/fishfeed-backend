"""Tests for cache utility functions."""


from app.utils.cache import (
    CACHE_VERSION,
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


class TestCacheConstants:
    """Tests for cache constants."""

    def test_cache_version_format(self):
        """Test that CACHE_VERSION is a valid version string."""
        assert isinstance(CACHE_VERSION, str)
        assert len(CACHE_VERSION) > 0

    def test_ttl_species_list_is_one_hour(self):
        """Test that TTL_SPECIES_LIST is 1 hour in seconds."""
        assert TTL_SPECIES_LIST == 3600

    def test_ttl_species_detail_is_24_hours(self):
        """Test that TTL_SPECIES_DETAIL is 24 hours in seconds."""
        assert TTL_SPECIES_DETAIL == 86400

    def test_ttl_species_popular_is_one_hour(self):
        """Test that TTL_SPECIES_POPULAR is 1 hour in seconds."""
        assert TTL_SPECIES_POPULAR == 3600

    def test_ttl_species_search_is_30_minutes(self):
        """Test that TTL_SPECIES_SEARCH is 30 minutes in seconds."""
        assert TTL_SPECIES_SEARCH == 1800


class TestSpeciesListKey:
    """Tests for species_list_key function."""

    def test_species_list_key_basic(self):
        """Test basic species_list_key generation."""
        key = species_list_key(1, 20)
        assert key.startswith(f"species:{CACHE_VERSION}:list:")
        assert ":1:" in key
        assert ":20:" in key

    def test_species_list_key_different_pages(self):
        """Test that different pages produce different keys."""
        key1 = species_list_key(1, 20)
        key2 = species_list_key(2, 20)
        assert key1 != key2

    def test_species_list_key_different_per_page(self):
        """Test that different per_page produces different keys."""
        key1 = species_list_key(1, 10)
        key2 = species_list_key(1, 20)
        assert key1 != key2

    def test_species_list_key_with_filters(self):
        """Test species_list_key with filters."""
        key1 = species_list_key(1, 20, care_level="beginner")
        key2 = species_list_key(1, 20, care_level="advanced")
        key3 = species_list_key(1, 20)
        assert key1 != key2
        assert key1 != key3
        assert key2 != key3

    def test_species_list_key_with_water_type(self):
        """Test species_list_key with water_type filter."""
        key1 = species_list_key(1, 20, water_type="freshwater")
        key2 = species_list_key(1, 20, water_type="saltwater")
        assert key1 != key2

    def test_species_list_key_deterministic(self):
        """Test that same parameters produce same key."""
        key1 = species_list_key(1, 20, "beginner", "freshwater")
        key2 = species_list_key(1, 20, "beginner", "freshwater")
        assert key1 == key2


class TestSpeciesDetailKey:
    """Tests for species_detail_key function."""

    def test_species_detail_key_format(self):
        """Test species_detail_key format."""
        key = species_detail_key("betta")
        assert key == f"species:{CACHE_VERSION}:detail:betta"

    def test_species_detail_key_different_ids(self):
        """Test that different IDs produce different keys."""
        key1 = species_detail_key("betta")
        key2 = species_detail_key("guppy")
        assert key1 != key2

    def test_species_detail_key_deterministic(self):
        """Test that same ID produces same key."""
        key1 = species_detail_key("neon-tetra")
        key2 = species_detail_key("neon-tetra")
        assert key1 == key2


class TestSpeciesPopularKey:
    """Tests for species_popular_key function."""

    def test_species_popular_key_format(self):
        """Test species_popular_key format."""
        key = species_popular_key()
        assert key == f"species:{CACHE_VERSION}:popular"

    def test_species_popular_key_deterministic(self):
        """Test that calling twice produces same key."""
        key1 = species_popular_key()
        key2 = species_popular_key()
        assert key1 == key2


class TestSpeciesSearchKey:
    """Tests for species_search_key function."""

    def test_species_search_key_format(self):
        """Test species_search_key format."""
        key = species_search_key("guppy")
        assert key.startswith(f"species:{CACHE_VERSION}:search:")

    def test_species_search_key_different_queries(self):
        """Test that different queries produce different keys."""
        key1 = species_search_key("guppy")
        key2 = species_search_key("betta")
        assert key1 != key2

    def test_species_search_key_case_insensitive(self):
        """Test that search keys are case-insensitive."""
        key1 = species_search_key("GUPPY")
        key2 = species_search_key("guppy")
        key3 = species_search_key("Guppy")
        assert key1 == key2 == key3

    def test_species_search_key_strips_whitespace(self):
        """Test that search keys strip whitespace."""
        key1 = species_search_key("  guppy  ")
        key2 = species_search_key("guppy")
        assert key1 == key2

    def test_species_search_key_deterministic(self):
        """Test that same query produces same key."""
        key1 = species_search_key("neon tetra")
        key2 = species_search_key("neon tetra")
        assert key1 == key2


class TestCachePatterns:
    """Tests for cache pattern functions."""

    def test_species_cache_pattern(self):
        """Test species_cache_pattern format."""
        pattern = species_cache_pattern()
        assert pattern == f"species:{CACHE_VERSION}:*"

    def test_species_list_pattern(self):
        """Test species_list_pattern format."""
        pattern = species_list_pattern()
        assert pattern == f"species:{CACHE_VERSION}:list:*"

    def test_species_popular_pattern(self):
        """Test species_popular_pattern format."""
        pattern = species_popular_pattern()
        assert pattern == f"species:{CACHE_VERSION}:popular*"
