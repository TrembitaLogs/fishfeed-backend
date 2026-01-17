"""Tests for S3 storage service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.storage import (
    S3StorageService,
    StorageNotConfiguredError,
    get_storage_service,
)


@pytest.fixture
def mock_settings():
    """Create mock settings with S3 configuration."""
    settings = MagicMock()
    settings.S3_ENDPOINT_URL = "https://s3.example.com"
    settings.S3_ACCESS_KEY = "test-access-key"
    settings.S3_SECRET_KEY = "test-secret-key"
    settings.S3_BUCKET_NAME = "test-bucket"
    settings.S3_REGION = "eu-central"
    settings.S3_RETENTION_DAYS = 30
    return settings


@pytest.fixture
def mock_settings_unconfigured():
    """Create mock settings without S3 configuration."""
    settings = MagicMock()
    settings.S3_ENDPOINT_URL = None
    settings.S3_ACCESS_KEY = None
    settings.S3_SECRET_KEY = None
    settings.S3_BUCKET_NAME = "test-bucket"
    settings.S3_REGION = "eu-central"
    settings.S3_RETENTION_DAYS = 30
    return settings


class TestS3StorageService:
    """Tests for S3StorageService class."""

    @pytest.mark.asyncio
    async def test_upload_image_not_configured(self, mock_settings_unconfigured):
        """Test upload raises error when not configured."""
        with patch(
            "app.services.storage.get_settings", return_value=mock_settings_unconfigured
        ):
            service = S3StorageService()

            with pytest.raises(StorageNotConfiguredError) as exc_info:
                await service.upload_image(b"test-bytes", "hash")

            assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_upload_image_returns_existing_url(self, mock_settings):
        """Test upload returns existing URL for duplicate hash."""
        with patch("app.services.storage.get_settings", return_value=mock_settings):
            service = S3StorageService()

            existing_url = (
                "https://s3.example.com/test-bucket/scans/2026/01/15/abc123hash.jpg"
            )

            with patch.object(
                service,
                "check_exists",
                new_callable=AsyncMock,
                return_value=existing_url,
            ):
                url = await service.upload_image(b"test-bytes", "abc123hash")

            assert url == existing_url

    @pytest.mark.asyncio
    async def test_check_exists_returns_none_when_not_configured(
        self, mock_settings_unconfigured
    ):
        """Test check_exists returns None when not configured."""
        with patch(
            "app.services.storage.get_settings", return_value=mock_settings_unconfigured
        ):
            service = S3StorageService()
            url = await service.check_exists("hash")
            assert url is None

    @pytest.mark.asyncio
    async def test_delete_old_images_not_configured(self, mock_settings_unconfigured):
        """Test delete raises error when not configured."""
        with patch(
            "app.services.storage.get_settings", return_value=mock_settings_unconfigured
        ):
            service = S3StorageService()

            with pytest.raises(StorageNotConfiguredError):
                await service.delete_old_images()

    @pytest.mark.asyncio
    async def test_get_object_key_format(self, mock_settings):
        """Test object key generation format."""
        with patch("app.services.storage.get_settings", return_value=mock_settings):
            service = S3StorageService()
            key = service._get_object_key("abc123hash")

            assert key.startswith("scans/")
            assert key.endswith("abc123hash.jpg")
            # Check date format is present (YYYY/MM/DD)
            parts = key.split("/")
            assert len(parts) == 5  # scans/YYYY/MM/DD/hash.jpg

    @pytest.mark.asyncio
    async def test_is_configured_true(self, mock_settings):
        """Test _is_configured returns True when all settings present."""
        with patch("app.services.storage.get_settings", return_value=mock_settings):
            service = S3StorageService()
            assert service._is_configured() is True

    @pytest.mark.asyncio
    async def test_is_configured_false(self, mock_settings_unconfigured):
        """Test _is_configured returns False when settings missing."""
        with patch(
            "app.services.storage.get_settings", return_value=mock_settings_unconfigured
        ):
            service = S3StorageService()
            assert service._is_configured() is False

    @pytest.mark.asyncio
    async def test_get_client_config(self, mock_settings):
        """Test client config generation."""
        with patch("app.services.storage.get_settings", return_value=mock_settings):
            service = S3StorageService()
            config = service._get_client_config()

            assert config["service_name"] == "s3"
            assert config["endpoint_url"] == "https://s3.example.com"
            assert config["aws_access_key_id"] == "test-access-key"
            assert config["aws_secret_access_key"] == "test-secret-key"
            assert config["region_name"] == "eu-central"


class TestGetStorageService:
    """Tests for get_storage_service function."""

    def test_returns_singleton(self):
        """Test get_storage_service returns same instance."""
        import app.services.storage as storage_module

        storage_module._storage_service = None

        service1 = get_storage_service()
        service2 = get_storage_service()

        assert service1 is service2

        # Cleanup
        storage_module._storage_service = None
