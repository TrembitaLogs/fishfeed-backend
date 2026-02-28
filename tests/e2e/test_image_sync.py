"""E2E tests: full image upload → sync → presigned URL → cleanup flow.

These tests exercise the complete image lifecycle against a real MinIO
instance (no S3 mocking). Requires docker-compose services running
(PostgreSQL, Redis, MinIO).

Run:
    uv run pytest tests/e2e/test_image_sync.py -v
"""

import asyncio
import io
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
from botocore.exceptions import ClientError
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.orphaned_image import OrphanedImage
from app.models.user import User
from app.utils.jwt import create_access_token
from tests.e2e.conftest import E2E_S3_BUCKET

# --- Helpers ---


def _create_image_bytes(
    fmt: str = "WEBP",
    width: int = 200,
    height: int = 200,
    color: tuple[int, int, int] = (255, 0, 0),
) -> bytes:
    """Create valid test image bytes in the specified format."""
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    buffer.seek(0)
    return buffer.getvalue()


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """Build Authorization header with a valid JWT for the given user."""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


async def _create_user(session: AsyncSession, email: str | None = None) -> User:
    """Create a test user for E2E tests."""
    user = User(
        email=email or f"e2e-{uuid.uuid4()}@example.com",
        password_hash="hashed_password",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _create_aquarium(
    session: AsyncSession,
    owner: User,
    name: str = "E2E Aquarium",
) -> Aquarium:
    """Create a test aquarium with the owner registered as AquariumMember."""
    aquarium = Aquarium(owner_id=owner.id, name=name)
    session.add(aquarium)
    await session.flush()
    member = AquariumMember(
        aquarium_id=aquarium.id,
        user_id=owner.id,
        role="owner",
    )
    session.add(member)
    await session.commit()
    await session.refresh(aquarium)
    return aquarium


class _MockSessionContext:
    """Async context manager that returns a pre-existing session.

    Used to patch ``async_session_maker()`` in the cleanup job so it
    operates on the test database session instead of creating its own.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        pass


# --- E2E Test ---


@pytest.mark.asyncio(loop_scope="session")
async def test_full_image_upload_flow(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    s3_client: object,
    s3_cleanup: list[str],
    cleanup_e2e_data: None,
) -> None:
    """Full E2E: upload → S3 verify → sync → presigned URL → download → replace → orphan → cleanup."""
    # ── Step 1: Setup ────────────────────────────────────────────────
    user = await _create_user(async_session)
    aquarium = await _create_aquarium(async_session, user)
    headers = _auth_headers(user.id)

    image_data = _create_image_bytes("WEBP", 300, 300, color=(0, 128, 255))

    # ── Step 2: Upload image via API ─────────────────────────────────
    upload_resp = await client.post(
        "/api/v1/images/upload",
        files={"file": ("photo.webp", image_data, "image/webp")},
        data={
            "entity_type": "aquarium",
            "entity_id": str(aquarium.id),
        },
        headers=headers,
    )
    assert upload_resp.status_code == 201, f"Upload failed: {upload_resp.text}"

    upload_data = upload_resp.json()
    first_key = upload_data["key"]
    assert first_key.startswith("aquariums/")
    assert first_key.endswith(".webp")
    assert upload_data["entity_type"] == "aquarium"
    assert upload_data["entity_id"] == str(aquarium.id)
    s3_cleanup.append(first_key)

    # ── Step 3: Verify S3 object exists ──────────────────────────────
    s3_obj = await s3_client.get_object(Bucket=E2E_S3_BUCKET, Key=first_key)
    assert s3_obj["ContentType"] == "image/webp"
    s3_body = await s3_obj["Body"].read()
    assert len(s3_body) > 0

    # ── Step 4: Sync aquarium with photo_key ─────────────────────────
    # Refresh the aquarium to get the updated_at AFTER upload (the upload
    # endpoint updates photo_key and bumps updated_at in DB).
    await async_session.refresh(aquarium)

    sync_resp = await client.post(
        "/api/v1/sync",
        json={
            "changes": [
                {
                    "entity_type": "aquarium",
                    "entity_id": str(aquarium.id),
                    "operation": "update",
                    "data": {"photo_key": first_key},
                    "client_updated_at": (
                        aquarium.updated_at + timedelta(seconds=1)
                    ).isoformat(),
                },
            ],
            "last_sync_at": None,
        },
        headers=headers,
    )
    assert sync_resp.status_code == 200, f"Sync failed: {sync_resp.text}"

    sync_data = sync_resp.json()
    assert str(aquarium.id) in sync_data["synced_ids"]
    assert len(sync_data["conflicts"]) == 0

    # ── Step 5: Get presigned URL ────────────────────────────────────
    urls_resp = await client.post(
        "/api/v1/images/urls",
        json={
            "items": [
                {
                    "entity_type": "aquarium",
                    "entity_id": str(aquarium.id),
                },
            ],
        },
        headers=headers,
    )
    assert urls_resp.status_code == 200, f"Presigned URLs failed: {urls_resp.text}"

    urls_data = urls_resp.json()
    assert len(urls_data["items"]) == 1
    item = urls_data["items"][0]
    assert item["key"] == first_key
    assert item["url"] is not None
    presigned_url = item["url"]

    # ── Step 6: Download image via presigned URL ─────────────────────
    async with httpx.AsyncClient() as download_client:
        download_resp = await download_client.get(presigned_url)

    assert download_resp.status_code == 200, (
        f"Presigned download failed: {download_resp.status_code}"
    )
    assert download_resp.headers["content-type"] == "image/webp"
    # The server may have stripped EXIF and re-processed the image,
    # so we compare length rather than exact bytes.
    assert len(download_resp.content) > 0

    # ── Step 7: Replace image (upload new one) ───────────────────────
    replacement_data = _create_image_bytes("WEBP", 400, 400, color=(255, 128, 0))

    replace_resp = await client.post(
        "/api/v1/images/upload",
        files={"file": ("photo2.webp", replacement_data, "image/webp")},
        data={
            "entity_type": "aquarium",
            "entity_id": str(aquarium.id),
        },
        headers=headers,
    )
    assert replace_resp.status_code == 201, f"Replace upload failed: {replace_resp.text}"

    second_key = replace_resp.json()["key"]
    assert second_key != first_key
    assert second_key.startswith("aquariums/")
    s3_cleanup.append(second_key)

    # ── Step 8: Verify old key is in orphaned_images ─────────────────
    stmt = select(OrphanedImage).where(OrphanedImage.old_key == first_key)
    result = await async_session.execute(stmt)
    orphan = result.scalar_one_or_none()
    assert orphan is not None, f"Old key {first_key} not found in orphaned_images"
    assert orphan.entity_type == "aquarium"

    # ── Step 9: Run cleanup job (backdate orphan, then execute) ──────
    # Move orphaned_at to 10 days ago so it's past the 7-day grace period.
    orphan.orphaned_at = datetime.now(UTC) - timedelta(days=10)
    await async_session.commit()

    # The cleanup job captures ``settings`` at module level. If the module
    # was imported before the ``override_s3_env`` fixture ran, the module-
    # level ``settings`` would have stale S3 config. Patching it explicitly
    # with the current ``get_settings()`` (which has env overrides) is safe.
    from app.config import get_settings as _get_settings
    from app.jobs.image_cleanup import image_cleanup_job

    with (
        patch("app.jobs.image_cleanup.settings", _get_settings()),
        patch("app.jobs.image_cleanup.async_session_maker") as mock_sm,
    ):
        mock_sm.return_value = _MockSessionContext(async_session)
        cleanup_result = await image_cleanup_job()

    assert cleanup_result["total_deleted_s3"] >= 1, f"Cleanup stats: {cleanup_result}"
    assert cleanup_result["total_deleted_db"] >= 1

    # ── Step 10: Verify old S3 object is deleted ─────────────────────
    with pytest.raises(ClientError) as exc_info:
        await s3_client.get_object(Bucket=E2E_S3_BUCKET, Key=first_key)
    assert exc_info.value.response["Error"]["Code"] in ("NoSuchKey", "404")

    # The replacement image should still exist
    replacement_obj = await s3_client.get_object(
        Bucket=E2E_S3_BUCKET,
        Key=second_key,
    )
    assert replacement_obj["ContentType"] == "image/webp"

    # Remove first_key from cleanup list since it was already deleted
    if first_key in s3_cleanup:
        s3_cleanup.remove(first_key)


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_upload(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    s3_client: object,
    s3_cleanup: list[str],
    cleanup_e2e_data: None,
) -> None:
    """Two simultaneous uploads for the same entity — one wins DB, other becomes orphaned."""
    # ── Setup ──────────────────────────────────────────────────────────
    user = await _create_user(async_session)
    aquarium = await _create_aquarium(async_session, user)
    headers = _auth_headers(user.id)

    img_a = _create_image_bytes("WEBP", 250, 250, color=(255, 0, 0))
    img_b = _create_image_bytes("WEBP", 250, 250, color=(0, 255, 0))

    # ── Concurrent uploads via asyncio.gather ──────────────────────────
    async def _upload(image_data: bytes, filename: str) -> httpx.Response:
        return await client.post(
            "/api/v1/images/upload",
            files={"file": (filename, image_data, "image/webp")},
            data={
                "entity_type": "aquarium",
                "entity_id": str(aquarium.id),
            },
            headers=headers,
        )

    resp_a, resp_b = await asyncio.gather(
        _upload(img_a, "a.webp"),
        _upload(img_b, "b.webp"),
    )

    assert resp_a.status_code == 201, f"Upload A failed: {resp_a.text}"
    assert resp_b.status_code == 201, f"Upload B failed: {resp_b.text}"

    key_a = resp_a.json()["key"]
    key_b = resp_b.json()["key"]
    assert key_a != key_b, "Both uploads must produce different S3 keys"
    s3_cleanup.extend([key_a, key_b])

    # ── Both S3 objects should exist ───────────────────────────────────
    for key in (key_a, key_b):
        obj = await s3_client.get_object(Bucket=E2E_S3_BUCKET, Key=key)
        assert obj["ContentType"] == "image/webp"

    # ── DB should have exactly one winner ──────────────────────────────
    await async_session.refresh(aquarium)
    winning_key = aquarium.photo_key
    assert winning_key in (key_a, key_b), (
        f"DB photo_key {winning_key} is not one of the uploaded keys"
    )

    losing_key = key_b if winning_key == key_a else key_a

    # ── Losing key must be in orphaned_images ──────────────────────────
    stmt = select(OrphanedImage).where(OrphanedImage.old_key == losing_key)
    result = await async_session.execute(stmt)
    orphan = result.scalar_one_or_none()
    assert orphan is not None, (
        f"Losing key {losing_key} not found in orphaned_images"
    )
    assert orphan.entity_type == "aquarium"


@pytest.mark.asyncio(loop_scope="session")
async def test_offline_sync_scenario(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    s3_client: object,
    s3_cleanup: list[str],
    cleanup_e2e_data: None,
) -> None:
    """Offline sync conflict: two clients upload, LWW resolves on sync."""
    # ── Setup ──────────────────────────────────────────────────────────
    user = await _create_user(async_session)
    aquarium = await _create_aquarium(async_session, user)
    headers = _auth_headers(user.id)

    # ── Client A uploads first ─────────────────────────────────────────
    img_a = _create_image_bytes("WEBP", 300, 300, color=(255, 0, 0))
    resp_a = await client.post(
        "/api/v1/images/upload",
        files={"file": ("a.webp", img_a, "image/webp")},
        data={
            "entity_type": "aquarium",
            "entity_id": str(aquarium.id),
        },
        headers=headers,
    )
    assert resp_a.status_code == 201
    key_a = resp_a.json()["key"]
    s3_cleanup.append(key_a)

    # ── Client B uploads second ────────────────────────────────────────
    img_b = _create_image_bytes("WEBP", 300, 300, color=(0, 0, 255))
    resp_b = await client.post(
        "/api/v1/images/upload",
        files={"file": ("b.webp", img_b, "image/webp")},
        data={
            "entity_type": "aquarium",
            "entity_id": str(aquarium.id),
        },
        headers=headers,
    )
    assert resp_b.status_code == 201
    key_b = resp_b.json()["key"]
    s3_cleanup.append(key_b)

    # After both uploads the DB holds key_b (last write). key_a is orphaned.
    await async_session.refresh(aquarium)
    assert aquarium.photo_key == key_b

    # ── Client A syncs with key_a (T1 — older timestamp) ──────────────
    t1 = aquarium.updated_at  # updated_at was bumped by B's upload
    sync_a = await client.post(
        "/api/v1/sync",
        json={
            "changes": [
                {
                    "entity_type": "aquarium",
                    "entity_id": str(aquarium.id),
                    "operation": "update",
                    "data": {"photo_key": key_a},
                    "client_updated_at": (t1 - timedelta(seconds=5)).isoformat(),
                },
            ],
            "last_sync_at": None,
        },
        headers=headers,
    )
    assert sync_a.status_code == 200
    sync_a_data = sync_a.json()

    # Server should win because Client A's timestamp is older
    assert len(sync_a_data["conflicts"]) == 1, (
        f"Expected 1 conflict, got {sync_a_data['conflicts']}"
    )
    assert sync_a_data["conflicts"][0]["resolution"] == "server_wins"

    # DB still holds key_b
    await async_session.refresh(aquarium)
    assert aquarium.photo_key == key_b

    # ── Client B syncs with key_b (T2 — newer timestamp) ──────────────
    await async_session.refresh(aquarium)
    t2 = aquarium.updated_at
    sync_b = await client.post(
        "/api/v1/sync",
        json={
            "changes": [
                {
                    "entity_type": "aquarium",
                    "entity_id": str(aquarium.id),
                    "operation": "update",
                    "data": {"photo_key": key_b},
                    "client_updated_at": (t2 + timedelta(seconds=1)).isoformat(),
                },
            ],
            "last_sync_at": None,
        },
        headers=headers,
    )
    assert sync_b.status_code == 200
    sync_b_data = sync_b.json()
    assert len(sync_b_data["conflicts"]) == 0
    assert str(aquarium.id) in sync_b_data["synced_ids"]

    # Final state: photo_key == key_b (LWW winner)
    await async_session.refresh(aquarium)
    assert aquarium.photo_key == key_b

    # key_a should be in orphaned_images (registered during B's upload)
    stmt = select(OrphanedImage).where(OrphanedImage.old_key == key_a)
    result = await async_session.execute(stmt)
    orphan = result.scalar_one_or_none()
    assert orphan is not None, f"key_a {key_a} not in orphaned_images"


@pytest.mark.asyncio(loop_scope="session")
async def test_rate_limiting(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    s3_cleanup: list[str],
    cleanup_e2e_data: None,
    redis_client: object,
) -> None:
    """Exceeding image upload rate limit returns HTTP 429."""
    from app.config import get_settings

    settings = get_settings()
    limit = settings.RATE_LIMIT_IMAGE_UPLOAD_PER_MIN  # default 20

    user = await _create_user(async_session)
    aquarium = await _create_aquarium(async_session, user)
    headers = _auth_headers(user.id)

    # Clear any existing rate limit counter for this user
    rl_key = f"{settings.REDIS_KEY_PREFIX}image_upload:{user.id}"
    await redis_client.delete(rl_key)

    image_data = _create_image_bytes("WEBP", 100, 100, color=(128, 128, 128))

    # ── Exhaust the rate limit ─────────────────────────────────────────
    for i in range(limit):
        resp = await client.post(
            "/api/v1/images/upload",
            files={"file": (f"img{i}.webp", image_data, "image/webp")},
            data={
                "entity_type": "aquarium",
                "entity_id": str(aquarium.id),
            },
            headers=headers,
        )
        assert resp.status_code == 201, (
            f"Upload {i + 1}/{limit} failed unexpectedly: {resp.status_code} {resp.text}"
        )
        s3_cleanup.append(resp.json()["key"])

    # ── Next request should be rejected ────────────────────────────────
    over_limit_resp = await client.post(
        "/api/v1/images/upload",
        files={"file": ("over.webp", image_data, "image/webp")},
        data={
            "entity_type": "aquarium",
            "entity_id": str(aquarium.id),
        },
        headers=headers,
    )
    assert over_limit_resp.status_code == 429, (
        f"Expected 429, got {over_limit_resp.status_code}: {over_limit_resp.text}"
    )
    assert "Retry-After" in over_limit_resp.headers
    assert "rate limit" in over_limit_resp.json()["detail"].lower()

    # Clean up the rate limit key
    await redis_client.delete(rl_key)
