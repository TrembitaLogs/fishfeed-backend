"""Unit tests for species image URL resolution (CDN vs presigned fallback)."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.schemas.species import SpeciesResponse
from app.services.species import (
    _resolve_species_image_urls,
    _species_public_cdn_url,
)


def _make_species(species_id: str, image_url: str | None, updated_at: datetime) -> SpeciesResponse:
    return SpeciesResponse(
        id=species_id,
        common_name=species_id.replace("-", " ").title(),
        scientific_name=None,
        image_url=image_url,
        food_types=["pellets"],
        feeding_frequency=2,
        portion_hint=None,
        care_level="beginner",
        water_type="freshwater",
        metadata_=None,
        created_at=updated_at,
        updated_at=updated_at,
    )


class TestSpeciesPublicCdnUrl:
    """Tests for the pure URL builder."""

    def test_builds_url_with_unix_timestamp_cache_buster(self):
        ts = datetime(2026, 4, 19, 16, 49, 6, tzinfo=UTC)
        url = _species_public_cdn_url(
            "species/african-butterfly-fish/photo.webp",
            ts,
            "cdn.fishfeed.club",
        )
        assert url == (
            "https://cdn.fishfeed.club/species/african-butterfly-fish/photo.webp"
            f"?v={int(ts.timestamp())}"
        )


class TestResolveSpeciesImageUrls:
    """Tests for the resolver dispatching between CDN and presign."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_uses_cdn_when_configured(self):
        ts = datetime(2026, 4, 19, 16, 49, 6, tzinfo=UTC)
        items = [
            _make_species("betta", "species/betta/photo.webp", ts),
            _make_species("guppy", "species/guppy/photo.webp", ts),
        ]
        with (
            patch("app.services.species.get_settings") as mock_settings,
            patch(
                "app.services.species.batch_generate_presigned_urls"
            ) as mock_presign,
        ):
            mock_settings.return_value.S3_PUBLIC_CDN_DOMAIN = "cdn.fishfeed.club"
            resolved = await _resolve_species_image_urls(items)

        # CDN path must not call the presigner at all.
        mock_presign.assert_not_called()
        assert resolved[0].image_url == (
            f"https://cdn.fishfeed.club/species/betta/photo.webp?v={int(ts.timestamp())}"
        )
        assert resolved[1].image_url == (
            f"https://cdn.fishfeed.club/species/guppy/photo.webp?v={int(ts.timestamp())}"
        )

    @pytest.mark.asyncio(loop_scope="session")
    async def test_falls_back_to_presigned_when_cdn_unset(self):
        ts = datetime(2026, 4, 19, 16, 49, 6, tzinfo=UTC)
        items = [_make_species("betta", "species/betta/photo.webp", ts)]
        with (
            patch("app.services.species.get_settings") as mock_settings,
            patch(
                "app.services.species.batch_generate_presigned_urls"
            ) as mock_presign,
        ):
            mock_settings.return_value.S3_PUBLIC_CDN_DOMAIN = None
            mock_presign.return_value = {
                "species/betta/photo.webp": "https://signed.example/betta?sig=xyz"
            }
            resolved = await _resolve_species_image_urls(items)

        mock_presign.assert_awaited_once_with(["species/betta/photo.webp"])
        assert resolved[0].image_url == "https://signed.example/betta?sig=xyz"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_skips_species_without_image_url(self):
        ts = datetime(2026, 4, 19, 16, 49, 6, tzinfo=UTC)
        items = [
            _make_species("betta", None, ts),
            _make_species("guppy", "species/guppy/photo.webp", ts),
        ]
        with patch("app.services.species.get_settings") as mock_settings:
            mock_settings.return_value.S3_PUBLIC_CDN_DOMAIN = "cdn.fishfeed.club"
            resolved = await _resolve_species_image_urls(items)

        assert resolved[0].image_url is None
        assert resolved[1].image_url and resolved[1].image_url.startswith(
            "https://cdn.fishfeed.club/"
        )

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_empty_list_unchanged(self):
        result = await _resolve_species_image_urls([])
        assert result == []

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_unchanged_when_no_keys(self):
        ts = datetime(2026, 4, 19, 16, 49, 6, tzinfo=UTC)
        items = [_make_species("betta", None, ts)]
        # Should not call settings or presigner at all.
        with (
            patch("app.services.species.get_settings") as mock_settings,
            patch(
                "app.services.species.batch_generate_presigned_urls"
            ) as mock_presign,
        ):
            resolved = await _resolve_species_image_urls(items)

        mock_settings.assert_not_called()
        mock_presign.assert_not_called()
        assert resolved == items
