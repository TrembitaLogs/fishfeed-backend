"""Tests for /.well-known/ endpoints (Android App Links, iOS Universal Links)."""

import pytest
from httpx import AsyncClient

from app.config import get_settings


@pytest.mark.asyncio(loop_scope="session")
async def test_assetlinks_returns_android_package_from_settings(client: AsyncClient) -> None:
    """assetlinks.json must expose the Android package name from settings."""
    response = await client.get("/.well-known/assetlinks.json")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == 1

    target = payload[0]["target"]
    settings = get_settings()
    assert target["namespace"] == "android_app"
    assert target["package_name"] == settings.ANDROID_PACKAGE_NAME
    assert target["package_name"] == "com.fishfeed.fishfeed"
    assert target["sha256_cert_fingerprints"] == settings.APP_LINK_FINGERPRINTS


@pytest.mark.asyncio(loop_scope="session")
async def test_apple_app_site_association_uses_ios_bundle_id(client: AsyncClient) -> None:
    """apple-app-site-association must use IOS_BUNDLE_ID, not the Android package."""
    response = await client.get("/.well-known/apple-app-site-association")

    assert response.status_code == 200
    payload = response.json()
    details = payload["applinks"]["details"]
    assert len(details) == 1

    settings = get_settings()
    expected_app_id = f"{settings.APPLE_TEAM_ID}.{settings.IOS_BUNDLE_ID}"
    assert details[0]["appID"] == expected_app_id
    # Critical: the iOS appID must reference com.fishfeed.mobile, not the Android package.
    assert details[0]["appID"].endswith(".com.fishfeed.mobile")
    assert details[0]["paths"] == ["/join/*"]
