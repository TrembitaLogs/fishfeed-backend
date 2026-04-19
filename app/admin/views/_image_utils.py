"""Shared image utilities for admin views.

Provides sync URL generation and upload for species images, used by sqladmin
column formatters and on_model_change hooks where async is not available.

Species images live in a separate public bucket (S3_SPECIES_BUCKET_NAME).
When S3_PUBLIC_CDN_DOMAIN is configured, URLs are served directly via the CDN
without presigning. Otherwise (e.g. local dev without CDN), falls back to
presigned GET URLs.
"""

import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_presign_client: boto3.client | None = None
_upload_client: boto3.client | None = None


def _get_presign_client() -> boto3.client:
    global _presign_client  # noqa: PLW0603
    if _presign_client is None:
        from app.config import get_settings

        settings = get_settings()
        endpoint_url = (
            settings.S3_ADMIN_PRESIGNED_ENDPOINT_URL
            or settings.S3_PRESIGNED_ENDPOINT_URL
            or settings.S3_ENDPOINT_URL
        )
        _presign_client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
        )
    return _presign_client


def _get_upload_client() -> boto3.client:
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


def species_image_url_sync(key: str, expires_in: int = 3600) -> str | None:
    """Return a browser-accessible URL for a species image key.

    Prefers the public CDN (direct URL, no signing). Falls back to a presigned
    GET URL against the species bucket for dev environments without a CDN.
    Returns None on failure so column formatters can show a placeholder.
    """
    if not key:
        return None

    from app.config import get_settings

    settings = get_settings()

    if settings.S3_PUBLIC_CDN_DOMAIN:
        return f"https://{settings.S3_PUBLIC_CDN_DOMAIN}/{key.lstrip('/')}"

    try:
        client = _get_presign_client()
        url: str = client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.S3_SPECIES_BUCKET_NAME,
                "Key": key,
            },
            ExpiresIn=expires_in,
        )
        return url
    except ClientError:
        logger.warning("Failed to generate presigned URL for species key=%s", key)
        return None


def upload_species_image(key: str, data: bytes) -> None:
    """Upload species image bytes to the species bucket (sync, for admin panel)."""
    from app.config import get_settings

    settings = get_settings()
    client = _get_upload_client()
    client.put_object(
        Bucket=settings.S3_SPECIES_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType="image/webp",
    )
