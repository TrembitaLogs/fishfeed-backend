"""Tests for image upload and presigned URL API endpoints."""

import io
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.fish import Fish
from app.models.user import User
from app.utils.jwt import create_access_token


def _create_image_bytes(fmt: str = "WEBP", width: int = 200, height: int = 200) -> bytes:
    """Create valid test image bytes in the specified format."""
    image = Image.new("RGB", (width, height), (255, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    buffer.seek(0)
    return buffer.getvalue()


def get_auth_headers(user_id: uuid.UUID) -> dict:
    """Get authorization headers for a user."""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


async def cleanup_data(session: AsyncSession) -> None:
    """Helper to cleanup test data."""
    await session.execute(text("DELETE FROM orphaned_images"))
    await session.execute(text("DELETE FROM fish"))
    await session.execute(text("DELETE FROM aquarium_members"))
    await session.execute(text("DELETE FROM aquariums"))
    await session.execute(text("DELETE FROM users"))
    await session.commit()


async def create_test_user(
    session: AsyncSession,
    email: str | None = None,
) -> User:
    """Helper to create a test user."""
    user = User(
        email=email or f"test-{uuid.uuid4()}@example.com",
        password_hash="hashed_password",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def create_test_aquarium(
    session: AsyncSession,
    owner: User,
    name: str = "Test Aquarium",
) -> Aquarium:
    """Helper to create a test aquarium with owner as member."""
    aquarium = Aquarium(owner_id=owner.id, name=name)
    session.add(aquarium)
    await session.flush()
    member = AquariumMember(aquarium_id=aquarium.id, user_id=owner.id, role="owner")
    session.add(member)
    await session.commit()
    await session.refresh(aquarium)
    return aquarium


async def create_test_fish(
    session: AsyncSession,
    aquarium: Aquarium,
    species_id: str = "test-guppy",
    custom_name: str = "Nemo",
) -> Fish:
    """Helper to create a test fish."""
    fish = Fish(
        aquarium_id=aquarium.id,
        species_id=species_id,
        custom_name=custom_name,
        quantity=1,
    )
    session.add(fish)
    await session.commit()
    await session.refresh(fish)
    return fish


# --- POST /images/upload tests ---


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_valid_webp(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 201 Created with valid WebP image for aquarium."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)
        image_data = _create_image_bytes("WEBP")

        with patch(
            "app.services.image_service._upload_to_s3",
            new_callable=AsyncMock,
        ):
            response = await client.post(
                "/api/v1/images/upload",
                files={"file": ("photo.webp", image_data, "image/webp")},
                data={"entity_type": "aquarium", "entity_id": str(aquarium.id)},
                headers=get_auth_headers(user.id),
            )

        assert response.status_code == 201
        data = response.json()
        assert data["entity_type"] == "aquarium"
        assert data["entity_id"] == str(aquarium.id)
        assert data["key"].startswith("aquariums/")
        assert data["key"].endswith(".webp")
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_valid_jpeg(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 201 Created with valid JPEG image."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)
        image_data = _create_image_bytes("JPEG")

        with patch(
            "app.services.image_service._upload_to_s3",
            new_callable=AsyncMock,
        ):
            response = await client.post(
                "/api/v1/images/upload",
                files={"file": ("photo.jpg", image_data, "image/jpeg")},
                data={"entity_type": "aquarium", "entity_id": str(aquarium.id)},
                headers=get_auth_headers(user.id),
            )

        assert response.status_code == 201
        data = response.json()
        assert data["key"].startswith("aquariums/")
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_valid_png(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 201 Created with valid PNG image."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)
        image_data = _create_image_bytes("PNG")

        with patch(
            "app.services.image_service._upload_to_s3",
            new_callable=AsyncMock,
        ):
            response = await client.post(
                "/api/v1/images/upload",
                files={"file": ("photo.png", image_data, "image/png")},
                data={"entity_type": "aquarium", "entity_id": str(aquarium.id)},
                headers=get_auth_headers(user.id),
            )

        assert response.status_code == 201
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_avatar(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 201 Created for avatar upload (entity_id = user_id)."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        image_data = _create_image_bytes("WEBP")

        with patch(
            "app.services.image_service._upload_to_s3",
            new_callable=AsyncMock,
        ):
            response = await client.post(
                "/api/v1/images/upload",
                files={"file": ("avatar.webp", image_data, "image/webp")},
                data={"entity_type": "avatar", "entity_id": str(user.id)},
                headers=get_auth_headers(user.id),
            )

        assert response.status_code == 201
        data = response.json()
        assert data["key"].startswith("avatars/")
        assert data["entity_type"] == "avatar"
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_fish_photo(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 201 Created for fish photo upload."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)
        fish = await create_test_fish(async_session, aquarium)
        image_data = _create_image_bytes("WEBP")

        with patch(
            "app.services.image_service._upload_to_s3",
            new_callable=AsyncMock,
        ):
            response = await client.post(
                "/api/v1/images/upload",
                files={"file": ("fish.webp", image_data, "image/webp")},
                data={"entity_type": "fish", "entity_id": str(fish.id)},
                headers=get_auth_headers(user.id),
            )

        assert response.status_code == 201
        data = response.json()
        assert data["key"].startswith("fish/")
        assert data["entity_type"] == "fish"
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_invalid_entity_type(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 400 Bad Request with invalid entity_type."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        image_data = _create_image_bytes("WEBP")

        response = await client.post(
            "/api/v1/images/upload",
            files={"file": ("photo.webp", image_data, "image/webp")},
            data={"entity_type": "invalid", "entity_id": str(uuid.uuid4())},
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 400
        assert "Invalid entity_type" in response.json()["detail"]
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_invalid_entity_id(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 400 Bad Request with invalid entity_id (not UUID)."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        image_data = _create_image_bytes("WEBP")

        response = await client.post(
            "/api/v1/images/upload",
            files={"file": ("photo.webp", image_data, "image/webp")},
            data={"entity_type": "aquarium", "entity_id": "not-a-uuid"},
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 400
        assert "Invalid entity_id" in response.json()["detail"]
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_entity_not_found(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 404 Not Found when entity does not exist in DB."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        image_data = _create_image_bytes("WEBP")
        nonexistent_id = uuid.uuid4()

        response = await client.post(
            "/api/v1/images/upload",
            files={"file": ("photo.webp", image_data, "image/webp")},
            data={"entity_type": "aquarium", "entity_id": str(nonexistent_id)},
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 404
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_access_denied(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 403 Forbidden when user has no access to entity."""
    await cleanup_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        other_user = await create_test_user(async_session, email="other@example.com")
        aquarium = await create_test_aquarium(async_session, owner)
        image_data = _create_image_bytes("WEBP")

        response = await client.post(
            "/api/v1/images/upload",
            files={"file": ("photo.webp", image_data, "image/webp")},
            data={"entity_type": "aquarium", "entity_id": str(aquarium.id)},
            headers=get_auth_headers(other_user.id),
        )

        assert response.status_code == 403
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_file_too_large(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 413 Content Too Large for oversized files."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        # Avatar has 2 MB limit; create image exceeding it
        # Use large dimensions to produce a large file
        large_image = _create_image_bytes("PNG", width=3000, height=3000)

        response = await client.post(
            "/api/v1/images/upload",
            files={"file": ("big.png", large_image, "image/png")},
            data={"entity_type": "avatar", "entity_id": str(user.id)},
            headers=get_auth_headers(user.id),
        )

        # Either 413 (too large) or 400 (dimension exceeded) depending on which check fires first
        assert response.status_code in (400, 413)
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_unsupported_media_type(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 415 Unsupported Media Type for GIF."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)
        gif_data = _create_image_bytes("GIF")

        response = await client.post(
            "/api/v1/images/upload",
            files={"file": ("photo.gif", gif_data, "image/gif")},
            data={"entity_type": "aquarium", "entity_id": str(aquarium.id)},
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 415
        assert "Unsupported" in response.json()["detail"]
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_rate_limit(
    client: AsyncClient,
    async_session: AsyncSession,
    redis_client,
):
    """Test 429 Too Many Requests when rate limit exceeded."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Set Redis counter above the limit (20/min)
        from app.config import get_settings

        settings = get_settings()
        key = f"{settings.REDIS_KEY_PREFIX}image_upload:{user.id}"
        await redis_client.set(key, "21", ex=60)

        image_data = _create_image_bytes("WEBP")

        response = await client.post(
            "/api/v1/images/upload",
            files={"file": ("photo.webp", image_data, "image/webp")},
            data={"entity_type": "aquarium", "entity_id": str(aquarium.id)},
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 429
        assert "Retry-After" in response.headers
    finally:
        # Clean up rate limit key
        from app.config import get_settings

        settings = get_settings()
        key = f"{settings.REDIS_KEY_PREFIX}image_upload:{user.id}"
        await redis_client.delete(key)
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_unauthorized(client: AsyncClient):
    """Test 401 when no auth token provided."""
    image_data = _create_image_bytes("WEBP")
    response = await client.post(
        "/api/v1/images/upload",
        files={"file": ("photo.webp", image_data, "image/webp")},
        data={"entity_type": "aquarium", "entity_id": str(uuid.uuid4())},
    )
    assert response.status_code == 401


# --- POST /images/urls tests ---


@pytest.mark.asyncio(loop_scope="session")
async def test_presigned_urls_batch(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 200 OK with batch presigned URL request."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        with patch(
            "app.services.image_service.batch_generate_presigned_urls",
            new_callable=AsyncMock,
            return_value={},
        ):
            response = await client.post(
                "/api/v1/images/urls",
                json={
                    "items": [
                        {"entity_type": "aquarium", "entity_id": str(aquarium.id)},
                    ],
                },
                headers=get_auth_headers(user.id),
            )

        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["entity_type"] == "aquarium"
        assert item["entity_id"] == str(aquarium.id)
        # Aquarium has no photo_key yet, so key and url should be null
        assert item["key"] is None
        assert item["url"] is None
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_presigned_urls_inaccessible_excluded(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test that inaccessible entities are excluded from response."""
    await cleanup_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        other_user = await create_test_user(async_session, email="other@example.com")
        aquarium = await create_test_aquarium(async_session, owner)

        with patch(
            "app.services.image_service.batch_generate_presigned_urls",
            new_callable=AsyncMock,
            return_value={},
        ):
            response = await client.post(
                "/api/v1/images/urls",
                json={
                    "items": [
                        {"entity_type": "aquarium", "entity_id": str(aquarium.id)},
                    ],
                },
                headers=get_auth_headers(other_user.id),
            )

        assert response.status_code == 200
        data = response.json()
        # other_user has no access, so items should be empty
        assert len(data["items"]) == 0
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_presigned_urls_no_photo_returns_null(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test that entities without photo_key return null key and url."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        with patch(
            "app.services.image_service.batch_generate_presigned_urls",
            new_callable=AsyncMock,
            return_value={},
        ):
            response = await client.post(
                "/api/v1/images/urls",
                json={
                    "items": [
                        {"entity_type": "aquarium", "entity_id": str(aquarium.id)},
                    ],
                },
                headers=get_auth_headers(user.id),
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["key"] is None
        assert data["items"][0]["url"] is None
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_presigned_urls_too_many_items(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 400 Bad Request when more than 50 items requested."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)

        items = [
            {"entity_type": "aquarium", "entity_id": str(uuid.uuid4())}
            for _ in range(51)
        ]

        response = await client.post(
            "/api/v1/images/urls",
            json={"items": items},
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 400
        assert "50" in response.json()["detail"]
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_presigned_urls_with_photo_key(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test successful presigned URL for entities with photo_key set."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Set photo_key directly in DB
        aquarium.photo_key = "aquariums/test-id/abcd1234.webp"
        await async_session.commit()
        await async_session.refresh(aquarium)

        fake_url = "https://s3.example.com/presigned?Signature=abc"
        with patch(
            "app.services.image_service.batch_generate_presigned_urls",
            new_callable=AsyncMock,
            return_value={"aquariums/test-id/abcd1234.webp": fake_url},
        ):
            response = await client.post(
                "/api/v1/images/urls",
                json={
                    "items": [
                        {"entity_type": "aquarium", "entity_id": str(aquarium.id)},
                    ],
                },
                headers=get_auth_headers(user.id),
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["entity_type"] == "aquarium"
        assert item["entity_id"] == str(aquarium.id)
        assert item["key"] == "aquariums/test-id/abcd1234.webp"
        assert item["url"] == fake_url
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_presigned_urls_mixed_results(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test mixed results: some entities with photo, some without."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium_with_photo = await create_test_aquarium(
            async_session, user, name="With Photo",
        )
        aquarium_no_photo = await create_test_aquarium(
            async_session, user, name="No Photo",
        )

        # Set photo_key only on one aquarium
        aquarium_with_photo.photo_key = "aquariums/a1/f7a3b2c1.webp"
        await async_session.commit()
        await async_session.refresh(aquarium_with_photo)

        fake_url = "https://s3.example.com/presigned?Signature=xyz"
        with patch(
            "app.services.image_service.batch_generate_presigned_urls",
            new_callable=AsyncMock,
            return_value={"aquariums/a1/f7a3b2c1.webp": fake_url},
        ):
            response = await client.post(
                "/api/v1/images/urls",
                json={
                    "items": [
                        {
                            "entity_type": "aquarium",
                            "entity_id": str(aquarium_with_photo.id),
                        },
                        {
                            "entity_type": "aquarium",
                            "entity_id": str(aquarium_no_photo.id),
                        },
                    ],
                },
                headers=get_auth_headers(user.id),
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 2

        # Find items by entity_id
        items_by_id = {item["entity_id"]: item for item in data["items"]}

        # Aquarium with photo: key and url populated
        with_photo = items_by_id[str(aquarium_with_photo.id)]
        assert with_photo["key"] == "aquariums/a1/f7a3b2c1.webp"
        assert with_photo["url"] == fake_url

        # Aquarium without photo: key and url are null
        no_photo = items_by_id[str(aquarium_no_photo.id)]
        assert no_photo["key"] is None
        assert no_photo["url"] is None
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_presigned_urls_invalid_entity_id(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test 422 Unprocessable Entity when entity_id is not a valid UUID.

    Pydantic validates entity_id as uuid.UUID at the schema level,
    returning 422 before the handler executes.
    """
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)

        response = await client.post(
            "/api/v1/images/urls",
            json={
                "items": [
                    {"entity_type": "aquarium", "entity_id": "not-a-uuid"},
                ],
            },
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 422
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_presigned_urls_unauthorized(client: AsyncClient):
    """Test 401 when no auth token provided."""
    response = await client.post(
        "/api/v1/images/urls",
        json={"items": [{"entity_type": "aquarium", "entity_id": str(uuid.uuid4())}]},
    )
    assert response.status_code == 401


# --- Integration: router accessible in app ---


@pytest.mark.asyncio(loop_scope="session")
async def test_images_routes_registered(client: AsyncClient):
    """Test that /images routes are accessible in the app."""
    # POST to /upload without auth should return 401 (not 404)
    response = await client.post("/api/v1/images/upload")
    assert response.status_code != 404

    # POST to /urls without auth should return 401 (not 404)
    response = await client.post("/api/v1/images/urls")
    assert response.status_code != 404
