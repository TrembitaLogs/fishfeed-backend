"""Tests for AI fish recognition service."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIScan
from app.models.species import Species
from app.models.user import User
from app.services.ai import (
    AIServiceError,
    ScanAccessDeniedError,
    ScanLimitExceededError,
    ScanNotFoundError,
    SpeciesNotFoundError,
    confirm_species,
    get_remaining_scans,
    get_scan,
    get_scan_history,
    scan_image,
)
from app.services.ai_provider import ClassificationResult, Prediction


async def cleanup_data(session: AsyncSession) -> None:
    """Helper to cleanup test data."""
    await session.execute(text("TRUNCATE TABLE ai_scans, users, species CASCADE"))
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


# get_remaining_scans tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_remaining_scans_free_user(async_session: AsyncSession):
    """Test get_remaining_scans returns correct count for free user."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(
            async_session,
            subscription_status="free",
            free_ai_scans_remaining=3,
        )

        remaining = await get_remaining_scans(async_session, user.id)

        assert remaining == 3
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_remaining_scans_premium_user(async_session: AsyncSession):
    """Test get_remaining_scans returns -1 for premium user."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(
            async_session,
            subscription_status="premium",
            free_ai_scans_remaining=0,
        )

        remaining = await get_remaining_scans(async_session, user.id)

        assert remaining == -1
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_remaining_scans_nonexistent_user(async_session: AsyncSession):
    """Test get_remaining_scans returns 0 for nonexistent user."""
    await cleanup_data(async_session)

    remaining = await get_remaining_scans(async_session, uuid.uuid4())

    assert remaining == 0


# scan_image tests


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_image_success(async_session: AsyncSession):
    """Test successful image scan."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session, free_ai_scans_remaining=5)
        species = await create_test_species(async_session, "goldfish", "Goldfish")

        mock_result = ClassificationResult(
            predictions=[
                Prediction(label="Goldfish", confidence=0.95),
                Prediction(label="Koi", confidence=0.75),
            ]
        )

        mock_storage = AsyncMock()
        mock_storage.upload_image = AsyncMock(return_value="scans/test.webp")

        with patch(
            "app.services.ai.classify_with_fallback",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            # Use a simple base64 encoded PNG (1x1 pixel)
            image_base64 = (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            )
            response = await scan_image(
                async_session,
                user.id,
                image_base64=image_base64,
                storage=mock_storage,
            )

        assert response.species_id == "goldfish"
        assert response.species_name == "Goldfish"
        assert response.confidence == 0.95
        assert response.scans_remaining == 4  # Decreased by 1
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_image_decreases_counter(async_session: AsyncSession):
    """Test that scan_image decreases free_ai_scans_remaining."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session, free_ai_scans_remaining=3)
        await create_test_species(async_session, "goldfish", "Goldfish")

        mock_result = ClassificationResult(
            predictions=[Prediction(label="Goldfish", confidence=0.9)]
        )

        mock_storage = AsyncMock()
        mock_storage.upload_image = AsyncMock(return_value="scans/test.webp")

        with patch(
            "app.services.ai.classify_with_fallback",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            image_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            await scan_image(async_session, user.id, image_base64=image_base64, storage=mock_storage)

        await async_session.refresh(user)
        assert user.free_ai_scans_remaining == 2
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_image_limit_exceeded(async_session: AsyncSession):
    """Test scan_image raises error when limit exceeded."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session, free_ai_scans_remaining=0)

        image_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

        with pytest.raises(ScanLimitExceededError) as exc_info:
            await scan_image(async_session, user.id, image_base64=image_base64)

        assert exc_info.value.status_code == 402
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_image_premium_no_limit(async_session: AsyncSession):
    """Test premium user has no scan limit."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(
            async_session,
            subscription_status="premium",
            free_ai_scans_remaining=0,
        )
        await create_test_species(async_session, "goldfish", "Goldfish")

        mock_result = ClassificationResult(
            predictions=[Prediction(label="Goldfish", confidence=0.9)]
        )

        mock_storage = AsyncMock()
        mock_storage.upload_image = AsyncMock(return_value="scans/test.webp")

        with patch(
            "app.services.ai.classify_with_fallback",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            image_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            response = await scan_image(
                async_session,
                user.id,
                image_base64=image_base64,
                storage=mock_storage,
            )

        assert response.scans_remaining == 0  # -1 is converted to 0 in response
        assert response.species_id == "goldfish"
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_image_deduplication(async_session: AsyncSession):
    """Test scan_image returns cached result for same image."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session, free_ai_scans_remaining=5)
        species = await create_test_species(async_session, "goldfish", "Goldfish")

        mock_result = ClassificationResult(
            predictions=[Prediction(label="Goldfish", confidence=0.9)]
        )

        image_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

        mock_storage = AsyncMock()
        mock_storage.upload_image = AsyncMock(return_value="scans/test.webp")

        with patch(
            "app.services.ai.classify_with_fallback",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_classify:
            # First scan
            response1 = await scan_image(
                async_session,
                user.id,
                image_base64=image_base64,
                storage=mock_storage,
            )
            # Second scan with same image
            response2 = await scan_image(
                async_session,
                user.id,
                image_base64=image_base64,
                storage=mock_storage,
            )

        # AI should only be called once
        assert mock_classify.call_count == 1
        # Both responses should have same scan_id
        assert response1.scan_id == response2.scan_id
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_image_no_species_match(async_session: AsyncSession):
    """Test scan_image when AI label doesn't match any species."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session, free_ai_scans_remaining=5)
        # Don't create any species that matches

        mock_result = ClassificationResult(
            predictions=[Prediction(label="Unknown Fish", confidence=0.9)]
        )

        mock_storage = AsyncMock()
        mock_storage.upload_image = AsyncMock(return_value="scans/test.webp")

        with patch(
            "app.services.ai.classify_with_fallback",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            image_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            response = await scan_image(
                async_session,
                user.id,
                image_base64=image_base64,
                storage=mock_storage,
            )

        assert response.species_id is None
        assert response.species_name is None
        assert response.confidence == 0.0
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_image_no_image_provided(async_session: AsyncSession):
    """Test scan_image raises error when no image provided."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)

        with pytest.raises(AIServiceError) as exc_info:
            await scan_image(async_session, user.id)

        assert exc_info.value.status_code == 400
        assert "No image provided" in exc_info.value.message
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_image_logs_to_ai_scans(async_session: AsyncSession):
    """Test scan_image creates record in ai_scans table."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session, free_ai_scans_remaining=5)
        species = await create_test_species(async_session, "goldfish", "Goldfish")

        mock_result = ClassificationResult(
            predictions=[Prediction(label="Goldfish", confidence=0.9)]
        )

        mock_storage = AsyncMock()
        mock_storage.upload_image = AsyncMock(return_value="scans/test.webp")

        with patch(
            "app.services.ai.classify_with_fallback",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            image_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            response = await scan_image(
                async_session,
                user.id,
                image_base64=image_base64,
                storage=mock_storage,
            )

        # Verify scan was logged
        from sqlalchemy import select

        stmt = select(AIScan).where(AIScan.id == response.scan_id)
        result = await async_session.execute(stmt)
        scan = result.scalar_one()

        assert scan.user_id == user.id
        assert scan.detected_species_id == "goldfish"
        assert scan.confidence == 0.9
        assert scan.processing_time_ms is not None
    finally:
        await cleanup_data(async_session)


# confirm_species tests


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_species_success(async_session: AsyncSession):
    """Test successful species confirmation."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        await create_test_species(async_session, "goldfish", "Goldfish")
        species = await create_test_species(async_session, "betta", "Betta Fish")
        scan = await create_test_scan(
            async_session,
            user.id,
            species_id="goldfish",
        )

        await confirm_species(async_session, scan.id, user.id, "betta")

        await async_session.refresh(scan)
        assert scan.confirmed_species_id == "betta"
        assert scan.was_corrected is True
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_species_same_as_detected(async_session: AsyncSession):
    """Test confirmation with same species as detected."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session, "goldfish", "Goldfish")
        scan = await create_test_scan(
            async_session,
            user.id,
            species_id="goldfish",
        )

        await confirm_species(async_session, scan.id, user.id, "goldfish")

        await async_session.refresh(scan)
        assert scan.confirmed_species_id == "goldfish"
        assert scan.was_corrected is False
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_species_scan_not_found(async_session: AsyncSession):
    """Test confirmation with non-existent scan."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        await create_test_species(async_session, "goldfish", "Goldfish")

        with pytest.raises(ScanNotFoundError) as exc_info:
            await confirm_species(async_session, uuid.uuid4(), user.id, "goldfish")

        assert exc_info.value.status_code == 404
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_species_access_denied(async_session: AsyncSession):
    """Test confirmation for another user's scan."""
    await cleanup_data(async_session)
    try:
        user1 = await create_test_user(async_session, email="user1@example.com")
        user2 = await create_test_user(async_session, email="user2@example.com")
        await create_test_species(async_session, "goldfish", "Goldfish")
        scan = await create_test_scan(async_session, user1.id)

        with pytest.raises(ScanAccessDeniedError) as exc_info:
            await confirm_species(async_session, scan.id, user2.id, "goldfish")

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_species_species_not_found(async_session: AsyncSession):
    """Test confirmation with non-existent species."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        scan = await create_test_scan(async_session, user.id)

        with pytest.raises(SpeciesNotFoundError) as exc_info:
            await confirm_species(async_session, scan.id, user.id, "nonexistent")

        assert exc_info.value.status_code == 404
    finally:
        await cleanup_data(async_session)


# get_scan_history tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_history_returns_user_scans(async_session: AsyncSession):
    """Test get_scan_history returns scans for user."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session, "goldfish", "Goldfish")
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

        history = await get_scan_history(async_session, user.id)

        assert len(history) == 2
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_history_respects_limit(async_session: AsyncSession):
    """Test get_scan_history respects limit parameter."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        for i in range(5):
            await create_test_scan(async_session, user.id, image_hash=f"hash{i}")

        history = await get_scan_history(async_session, user.id, limit=3)

        assert len(history) == 3
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_history_ordered_by_date(async_session: AsyncSession):
    """Test get_scan_history returns newest first."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        scan1 = await create_test_scan(async_session, user.id, image_hash="hash1")
        scan2 = await create_test_scan(async_session, user.id, image_hash="hash2")

        history = await get_scan_history(async_session, user.id)

        # Newest scan should be first
        assert history[0].scan_id == scan2.id
        assert history[1].scan_id == scan1.id
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_history_empty_for_new_user(async_session: AsyncSession):
    """Test get_scan_history returns empty list for user with no scans."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)

        history = await get_scan_history(async_session, user.id)

        assert len(history) == 0
    finally:
        await cleanup_data(async_session)


# get_scan tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_success(async_session: AsyncSession):
    """Test getting a specific scan."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(async_session, "goldfish", "Goldfish")
        scan = await create_test_scan(
            async_session,
            user.id,
            species_id="goldfish",
            confidence=0.95,
        )

        response = await get_scan(async_session, scan.id, user.id)

        assert response.scan_id == scan.id
        assert response.species_id == "goldfish"
        assert response.confidence == 0.95
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_not_found(async_session: AsyncSession):
    """Test getting non-existent scan."""
    await cleanup_data(async_session)
    try:
        user = await create_test_user(async_session)

        with pytest.raises(ScanNotFoundError):
            await get_scan(async_session, uuid.uuid4(), user.id)
    finally:
        await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_scan_access_denied(async_session: AsyncSession):
    """Test getting another user's scan."""
    await cleanup_data(async_session)
    try:
        user1 = await create_test_user(async_session, email="user1@example.com")
        user2 = await create_test_user(async_session, email="user2@example.com")
        scan = await create_test_scan(async_session, user1.id)

        with pytest.raises(ScanAccessDeniedError):
            await get_scan(async_session, scan.id, user2.id)
    finally:
        await cleanup_data(async_session)
