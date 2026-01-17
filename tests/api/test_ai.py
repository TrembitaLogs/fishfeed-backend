"""Tests for AI fish recognition API endpoints."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIScan
from app.models.species import Species
from app.models.user import User
from app.services.ai_provider import ClassificationResult, Prediction
from app.utils.jwt import create_access_token


async def cleanup_data(session: AsyncSession) -> None:
    """Helper to cleanup test data."""
    await session.execute(text("TRUNCATE TABLE ai_scans, users CASCADE"))
    await session.commit()


async def create_test_user(
    session: AsyncSession,
    email: str = "test@example.com",
    subscription_status: str = "free",
    free_ai_scans_remaining: int = 5,
) -> User:
    """Helper to create a test user."""
    user = User(
        email=email,
        password_hash="hashed_password",
        subscription_status=subscription_status,
        free_ai_scans_remaining=free_ai_scans_remaining,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def create_test_species(
    session: AsyncSession,
    species_id: str = "goldfish",
    common_name: str = "Goldfish",
) -> Species:
    """Helper to create a test species."""
    # Check if species already exists
    from sqlalchemy import select

    stmt = select(Species).where(Species.id == species_id)
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    species = Species(
        id=species_id,
        common_name=common_name,
        scientific_name="Carassius auratus",
        food_types=["flakes", "pellets"],
        feeding_frequency=2,
        care_level="beginner",
        water_type="freshwater",
    )
    session.add(species)
    await session.commit()
    await session.refresh(species)
    return species


async def create_test_scan(
    session: AsyncSession,
    user_id: uuid.UUID,
    species_id: str | None = None,
    image_hash: str = "test_hash",
    confidence: float = 0.9,
) -> AIScan:
    """Helper to create a test scan."""
    scan = AIScan(
        user_id=user_id,
        image_hash=image_hash,
        detected_species_id=species_id,
        confidence=confidence,
        alternatives=[],
        processing_time_ms=100,
    )
    session.add(scan)
    await session.commit()
    await session.refresh(scan)
    return scan


def get_auth_headers(user_id: uuid.UUID) -> dict:
    """Get authorization headers for a user."""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


# POST /ai/scan tests


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_with_base64_image(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test scanning with base64 encoded image."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session, free_ai_scans_remaining=5)
        await create_test_species(async_session, "goldfish", "Goldfish")

        mock_result = ClassificationResult(
            predictions=[Prediction(label="Goldfish", confidence=0.95)]
        )

        with patch(
            "app.services.ai.classify_with_fallback",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            # 1x1 PNG image
            image_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

            response = await client.post(
                "/ai/scan",
                json={"image_base64": image_base64},
                headers=get_auth_headers(user.id),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["species_id"] == "goldfish"
        assert data["species_name"] == "Goldfish"
        assert data["confidence"] == 0.95
        assert data["scans_remaining"] == 4
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_no_image_provided(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test scanning without providing any image."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)

        response = await client.post(
            "/ai/scan",
            json={},
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 400
        assert "No image provided" in response.json()["detail"]
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_unauthorized(client: AsyncClient):
    """Test scanning without authentication."""
    response = await client.post("/ai/scan", json={"image_base64": "test"})
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_limit_exceeded(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test scanning when limit is exceeded returns 402."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session, free_ai_scans_remaining=0)

        image_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

        response = await client.post(
            "/ai/scan",
            json={"image_base64": image_base64},
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 402
    finally:
        await cleanup_data(async_session)


# GET /ai/scans/remaining tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scans_remaining_free_user(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test getting remaining scans for free user."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(
            async_session,
            subscription_status="free",
            free_ai_scans_remaining=3,
        )

        response = await client.get(
            "/ai/scans/remaining",
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["scans_remaining"] == 3
        assert data["is_premium"] is False
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scans_remaining_premium_user(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test getting remaining scans for premium user."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(
            async_session,
            subscription_status="premium",
            free_ai_scans_remaining=0,
        )

        response = await client.get(
            "/ai/scans/remaining",
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["scans_remaining"] == 999999  # Unlimited
        assert data["is_premium"] is True
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scans_remaining_unauthorized(client: AsyncClient):
    """Test getting remaining scans without authentication."""
    response = await client.get("/ai/scans/remaining")
    assert response.status_code == 401


# POST /ai/scans/{scan_id}/confirm tests


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_species_success(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test confirming species identification."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        await create_test_species(async_session, "goldfish", "Goldfish")
        await create_test_species(async_session, "betta", "Betta Fish")
        scan = await create_test_scan(
            async_session,
            user.id,
            species_id="goldfish",
        )

        response = await client.post(
            f"/ai/scans/{scan.id}/confirm",
            json={"species_id": "betta"},
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 204
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_species_scan_not_found(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test confirming non-existent scan returns 404."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        await create_test_species(async_session, "goldfish", "Goldfish")

        response = await client.post(
            f"/ai/scans/{uuid.uuid4()}/confirm",
            json={"species_id": "goldfish"},
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 404
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_species_access_denied(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test confirming another user's scan returns 403."""
    await cleanup_data(async_session)
    try:
        user1 = await create_test_user(async_session, email="user1@example.com")
        user2 = await create_test_user(async_session, email="user2@example.com")
        await create_test_species(async_session, "goldfish", "Goldfish")
        scan = await create_test_scan(async_session, user1.id)

        response = await client.post(
            f"/ai/scans/{scan.id}/confirm",
            json={"species_id": "goldfish"},
            headers=get_auth_headers(user2.id),
        )

        assert response.status_code == 403
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_species_species_not_found(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test confirming with non-existent species returns 404."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        scan = await create_test_scan(async_session, user.id)

        response = await client.post(
            f"/ai/scans/{scan.id}/confirm",
            json={"species_id": "nonexistent"},
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 404
    finally:
        await cleanup_data(async_session)


# GET /ai/scans/history tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_history_success(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test getting scan history."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        await create_test_species(async_session, "goldfish", "Goldfish")
        await create_test_scan(
            async_session,
            user.id,
            species_id="goldfish",
            image_hash="hash1",
        )
        await create_test_scan(
            async_session,
            user.id,
            species_id="goldfish",
            image_hash="hash2",
        )

        response = await client.get(
            "/ai/scans/history",
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_history_with_pagination(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test scan history pagination."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        for i in range(5):
            await create_test_scan(async_session, user.id, image_hash=f"hash{i}")

        response = await client.get(
            "/ai/scans/history?limit=2&offset=1",
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_history_empty(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test getting empty scan history."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)

        response = await client.get(
            "/ai/scans/history",
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 200
        assert response.json() == []
    finally:
        await cleanup_data(async_session)


# GET /ai/scans/{scan_id} tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_by_id_success(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test getting a specific scan."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        await create_test_species(async_session, "goldfish", "Goldfish")
        scan = await create_test_scan(
            async_session,
            user.id,
            species_id="goldfish",
            confidence=0.95,
        )

        response = await client.get(
            f"/ai/scans/{scan.id}",
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["scan_id"] == str(scan.id)
        assert data["species_id"] == "goldfish"
        assert data["confidence"] == 0.95
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_by_id_not_found(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test getting non-existent scan returns 404."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)

        response = await client.get(
            f"/ai/scans/{uuid.uuid4()}",
            headers=get_auth_headers(user.id),
        )

        assert response.status_code == 404
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_by_id_access_denied(
    client: AsyncClient,
    async_session: AsyncSession,
):
    """Test getting another user's scan returns 403."""
    await cleanup_data(async_session)
    try:
        user1 = await create_test_user(async_session, email="user1@example.com")
        user2 = await create_test_user(async_session, email="user2@example.com")
        scan = await create_test_scan(async_session, user1.id)

        response = await client.get(
            f"/ai/scans/{scan.id}",
            headers=get_auth_headers(user2.id),
        )

        assert response.status_code == 403
    finally:
        await cleanup_data(async_session)
