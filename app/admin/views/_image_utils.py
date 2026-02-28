"""Shared image utilities for admin views.

Provides sync S3 presigned URL generation and upload for use in sqladmin
column formatters and on_model_change hooks, where async is not available.
"""

import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Module-level S3 client caches (lazily initialized).
_presign_client: boto3.client | None = None
_upload_client: boto3.client | None = None


def _get_presign_client() -> boto3.client:
    """Return a cached S3 client for presigned URL generation.

    Uses S3_PRESIGNED_ENDPOINT_URL (localhost) so generated URLs are
    accessible from the browser outside Docker.
    """
    global _presign_client  # noqa: PLW0603
    if _presign_client is None:
        from app.config import get_settings

        settings = get_settings()
        endpoint_url = settings.S3_PRESIGNED_ENDPOINT_URL or settings.S3_ENDPOINT_URL
        _presign_client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
        )
    return _presign_client


def _get_upload_client() -> boto3.client:
    """Return a cached S3 client for uploads.

    Uses S3_ENDPOINT_URL (minio:9000 inside Docker) for actual S3 operations.
    """
    global _upload_client  # noqa: PLW0603
    if _upload_client is None:
        from app.config import get_settings

        settings = get_settings()
        _upload_client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
        )
    return _upload_client


def presigned_url_sync(key: str, expires_in: int = 3600) -> str | None:
    """Generate a presigned GET URL for an S3 object (sync version).

    Returns None on failure instead of raising, so column formatters
    can gracefully degrade to a placeholder.
    """
    if not key:
        return None

    from app.config import get_settings

    settings = get_settings()

    try:
        client = _get_presign_client()
        url: str = client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.S3_IMAGES_BUCKET_NAME,
                "Key": key,
            },
            ExpiresIn=expires_in,
        )
        return url
    except ClientError:
        logger.warning("Failed to generate presigned URL for key=%s", key)
        return None


def upload_species_image(key: str, data: bytes) -> None:
    """Upload image bytes to S3/MinIO (sync version for admin panel)."""
    from app.config import get_settings

    settings = get_settings()
    client = _get_upload_client()
    client.put_object(
        Bucket=settings.S3_IMAGES_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType="image/webp",
    )
