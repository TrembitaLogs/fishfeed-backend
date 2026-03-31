"""S3-compatible object storage service for AI scan images.

This module provides async S3 operations for storing and retrieving
fish scan images using Hetzner Object Storage (S3-compatible).
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aioboto3
import structlog
from botocore.exceptions import ClientError

from app.config import get_settings

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client as S3ClientType

logger = structlog.get_logger(__name__)


class StorageError(Exception):
    """Base exception for storage operations."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class StorageNotConfiguredError(StorageError):
    """Raised when S3 storage is not configured."""

    def __init__(self) -> None:
        super().__init__(
            "S3 storage is not configured. Set S3_ENDPOINT_URL, S3_ACCESS_KEY, and S3_SECRET_KEY.",
            status_code=503,
        )


class S3StorageService:
    """Async S3-compatible storage service for scan images.

    Provides upload, existence check, and cleanup operations for
    AI scan images with automatic deduplication via content hash.

    Usage:
        storage = S3StorageService()
        url = await storage.upload_image(image_bytes, "sha256hash")
        existing = await storage.check_exists("sha256hash")
    """

    def __init__(self) -> None:
        """Initialize storage service with settings."""
        self._settings = get_settings()
        self._session = aioboto3.Session()

    def _is_configured(self) -> bool:
        """Check if S3 storage is properly configured."""
        return bool(
            self._settings.S3_ENDPOINT_URL
            and self._settings.S3_ACCESS_KEY
            and self._settings.S3_SECRET_KEY
        )

    def _get_object_key(self, image_hash: str) -> str:
        """Generate S3 object key from image hash.

        Uses date-based prefix for better S3 partitioning.

        Args:
            image_hash: SHA-256 hash of the image.

        Returns:
            S3 object key string.
        """
        today = datetime.now(UTC).strftime("%Y/%m/%d")
        return f"scans/{today}/{image_hash}.jpg"

    def _get_client_config(self) -> dict:
        """Get boto3 client configuration."""
        return {
            "service_name": "s3",
            "endpoint_url": self._settings.S3_ENDPOINT_URL,
            "aws_access_key_id": self._settings.S3_ACCESS_KEY,
            "aws_secret_access_key": self._settings.S3_SECRET_KEY,
            "region_name": self._settings.S3_REGION,
        }

    async def upload_image(self, image_bytes: bytes, image_hash: str) -> str:
        """Upload image to S3 storage.

        Checks for existing image with same hash first (deduplication).
        Returns URL of uploaded or existing image.

        Args:
            image_bytes: Image data as bytes.
            image_hash: SHA-256 hash of the image for deduplication.

        Returns:
            Public URL of the uploaded image.

        Raises:
            StorageNotConfiguredError: If S3 is not configured.
            StorageError: If upload fails.
        """
        if not self._is_configured():
            raise StorageNotConfiguredError()

        # Check for existing image first
        existing_url = await self.check_exists(image_hash)
        if existing_url:
            logger.info("Image already exists, returning cached URL", image_hash=image_hash[:16])
            return existing_url

        object_key = self._get_object_key(image_hash)

        try:
            async with self._session.client(**self._get_client_config()) as s3:
                s3: S3ClientType  # type: ignore[no-redef]
                await s3.put_object(
                    Bucket=self._settings.S3_BUCKET_NAME,
                    Key=object_key,
                    Body=image_bytes,
                    ContentType="image/jpeg",
                    Metadata={"image-hash": image_hash},
                )

            # Construct URL
            url = f"{self._settings.S3_ENDPOINT_URL}/{self._settings.S3_BUCKET_NAME}/{object_key}"
            logger.info("Uploaded image to S3", image_hash=image_hash[:16], object_key=object_key)
            return url

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error("S3 upload failed", error_code=error_code, error=str(e))
            raise StorageError(f"Failed to upload image: {error_code}") from None

    async def check_exists(self, image_hash: str) -> str | None:
        """Check if image with given hash already exists in storage.

        Searches for object with matching hash in metadata.

        Args:
            image_hash: SHA-256 hash of the image.

        Returns:
            URL of existing image if found, None otherwise.

        Raises:
            StorageNotConfiguredError: If S3 is not configured.
        """
        if not self._is_configured():
            return None

        # We use a predictable path based on hash, so we can check directly
        # by listing objects with the hash prefix across date folders
        try:
            async with self._session.client(**self._get_client_config()) as s3:
                s3: S3ClientType  # type: ignore[no-redef]
                # Search in recent date folders (last 30 days would be covered by retention)
                # For simplicity, we'll use the hash as part of object name and search
                paginator = s3.get_paginator("list_objects_v2")

                async for page in paginator.paginate(
                    Bucket=self._settings.S3_BUCKET_NAME,
                    Prefix="scans/",
                ):
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        # Check if the key ends with our hash
                        if key.endswith(f"{image_hash}.jpg"):
                            url = f"{self._settings.S3_ENDPOINT_URL}/{self._settings.S3_BUCKET_NAME}/{key}"
                            return url

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.warning("S3 existence check failed", error_code=error_code)
            return None

        return None

    async def delete_old_images(self, retention_days: int | None = None) -> int:
        """Delete images older than retention period.

        Used for cleanup job to enforce retention policy.

        Args:
            retention_days: Number of days to retain images.
                           Defaults to S3_RETENTION_DAYS setting.

        Returns:
            Number of deleted objects.

        Raises:
            StorageNotConfiguredError: If S3 is not configured.
            StorageError: If deletion fails.
        """
        if not self._is_configured():
            raise StorageNotConfiguredError()

        if retention_days is None:
            retention_days = self._settings.S3_RETENTION_DAYS

        cutoff_date = datetime.now(UTC)
        deleted_count = 0

        try:
            async with self._session.client(**self._get_client_config()) as s3:
                s3: S3ClientType  # type: ignore[no-redef]
                paginator = s3.get_paginator("list_objects_v2")

                objects_to_delete: list[dict] = []

                async for page in paginator.paginate(
                    Bucket=self._settings.S3_BUCKET_NAME,
                    Prefix="scans/",
                ):
                    for obj in page.get("Contents", []):
                        last_modified = obj.get("LastModified")
                        if last_modified:
                            age_days = (cutoff_date - last_modified).days
                            if age_days > retention_days:
                                objects_to_delete.append({"Key": obj["Key"]})

                        # Batch delete in groups of 1000 (S3 limit)
                        if len(objects_to_delete) >= 1000:
                            response = await s3.delete_objects(
                                Bucket=self._settings.S3_BUCKET_NAME,
                                Delete={"Objects": objects_to_delete},
                            )
                            deleted_count += len(response.get("Deleted", []))
                            objects_to_delete = []

                # Delete remaining objects
                if objects_to_delete:
                    response = await s3.delete_objects(
                        Bucket=self._settings.S3_BUCKET_NAME,
                        Delete={"Objects": objects_to_delete},
                    )
                    deleted_count += len(response.get("Deleted", []))

            logger.info("Deleted old images from S3", deleted_count=deleted_count)
            return deleted_count

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error("S3 cleanup failed", error_code=error_code, error=str(e))
            raise StorageError(f"Failed to cleanup old images: {error_code}") from None

    async def get_image_url(self, image_hash: str) -> str | None:
        """Get URL for image by hash if it exists.

        Args:
            image_hash: SHA-256 hash of the image.

        Returns:
            URL of image if found, None otherwise.
        """
        return await self.check_exists(image_hash)

    async def upload_json(self, data: bytes, object_key: str) -> str:
        """Upload JSON data to S3 storage.

        Args:
            data: JSON data as bytes.
            object_key: S3 object key for the file.

        Returns:
            S3 object key of uploaded file.

        Raises:
            StorageNotConfiguredError: If S3 is not configured.
            StorageError: If upload fails.
        """
        if not self._is_configured():
            raise StorageNotConfiguredError()

        try:
            async with self._session.client(**self._get_client_config()) as s3:
                s3: S3ClientType  # type: ignore[no-redef]
                await s3.put_object(
                    Bucket=self._settings.S3_BUCKET_NAME,
                    Key=object_key,
                    Body=data,
                    ContentType="application/json",
                )

            logger.info("Uploaded JSON data to S3", object_key=object_key)
            return object_key

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error("S3 JSON upload failed", error_code=error_code, error=str(e))
            raise StorageError(f"Failed to upload JSON data: {error_code}") from None

    async def generate_presigned_url(
        self,
        object_key: str,
        expires_in_seconds: int = 3600,
    ) -> str:
        """Generate a presigned URL for downloading an object.

        Args:
            object_key: S3 object key.
            expires_in_seconds: URL expiration time in seconds (default 1 hour).

        Returns:
            Presigned URL string.

        Raises:
            StorageNotConfiguredError: If S3 is not configured.
            StorageError: If URL generation fails.
        """
        if not self._is_configured():
            raise StorageNotConfiguredError()

        try:
            async with self._session.client(**self._get_client_config()) as s3:
                s3: S3ClientType  # type: ignore[no-redef]
                url = await s3.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": self._settings.S3_BUCKET_NAME,
                        "Key": object_key,
                    },
                    ExpiresIn=expires_in_seconds,
                )

            logger.info("Generated presigned URL", object_key=object_key)
            return str(url)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error("S3 presigned URL generation failed", error_code=error_code, error=str(e))
            raise StorageError(f"Failed to generate presigned URL: {error_code}") from None

    async def get_object_size(self, object_key: str) -> int:
        """Get the size of an S3 object in bytes.

        Args:
            object_key: S3 object key.

        Returns:
            Object size in bytes.

        Raises:
            StorageNotConfiguredError: If S3 is not configured.
            StorageError: If size retrieval fails.
        """
        if not self._is_configured():
            raise StorageNotConfiguredError()

        try:
            async with self._session.client(**self._get_client_config()) as s3:
                s3: S3ClientType  # type: ignore[no-redef]
                response = await s3.head_object(
                    Bucket=self._settings.S3_BUCKET_NAME,
                    Key=object_key,
                )
                return int(response.get("ContentLength", 0))

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error("S3 get object size failed", error_code=error_code, error=str(e))
            raise StorageError(f"Failed to get object size: {error_code}") from None


# Module-level instance for dependency injection
_storage_service: S3StorageService | None = None


def get_storage_service() -> S3StorageService:
    """Get or create storage service instance.

    Returns:
        S3StorageService instance.
    """
    global _storage_service
    if _storage_service is None:
        _storage_service = S3StorageService()
    return _storage_service
