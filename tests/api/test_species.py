"""E2E tests for species API endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.species import Species
from app.models.user import User
from app.utils.jwt import create_access_token
from app.utils.password import hash_password


async def cleanup_data(session: AsyncSession) -> None:
    """Helper to cleanup test data."""
    await session.execute(text("TRUNCATE TABLE species CASCADE"))
    await session.execute(text("DELETE FROM users WHERE email LIKE '%species_test%'"))
    await session.commit()


async def create_test_species(
    session: AsyncSession,
    species_id: str,
    common_name: str,
    scientific_name: str | None = None,
    care_level: str = "beginner",
    water_type: str = "freshwater",
) -> Species:
    """Helper to create a test species."""
    species = Species(
        id=species_id,
        common_name=common_name,
        scientific_name=scientific_name,
        food_types=["flakes", "pellets"],
        feeding_frequency=2,
        care_level=care_level,
        water_type=water_type,
    )
    session.add(species)
    await session.commit()
    await session.refresh(species)
    return species


async def create_test_user(
    session: AsyncSession,
    email: str,
    is_admin: bool = False,
) -> tuple[User, str]:
    """Helper to create a test user and return user with access token."""
    user = User(
        email=email,
        password_hash=hash_password("TestPass123"),
        is_admin=is_admin,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    access_token = create_access_token(str(user.id))
    return user, access_token


@pytest.mark.asyncio(loop_scope="session")
class TestListSpecies:
    """Tests for GET /species endpoint."""

    async def test_list_species_returns_paginated_list(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that GET /species returns paginated list."""
        await cleanup_data(async_session)
        try:
            await create_test_species(async_session, "fish-a", "Alpha Fish")
            await create_test_species(async_session, "fish-b", "Beta Fish")
            await create_test_species(async_session, "fish-c", "Gamma Fish")

            response = await client.get("/api/v1/species", params={"page": 1, "per_page": 2})

            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 3
            assert len(data["items"]) == 2
            assert data["page"] == 1
            assert data["per_page"] == 2
            assert data["pages"] == 2
        finally:
            await cleanup_data(async_session)

    async def test_list_species_filter_by_care_level(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that GET /species filters by care_level."""
        await cleanup_data(async_session)
        try:
            await create_test_species(
                async_session, "easy-fish", "Easy Fish", care_level="beginner"
            )
            await create_test_species(
                async_session, "medium-fish", "Medium Fish", care_level="intermediate"
            )
            await create_test_species(
                async_session, "hard-fish", "Hard Fish", care_level="advanced"
            )

            response = await client.get(
                "/api/v1/species", params={"care_level": "intermediate"}
            )

            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 1
            assert data["items"][0]["common_name"] == "Medium Fish"
        finally:
            await cleanup_data(async_session)

    async def test_list_species_filter_by_water_type(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that GET /species filters by water_type."""
        await cleanup_data(async_session)
        try:
            await create_test_species(
                async_session, "fresh-fish", "Fresh Fish", water_type="freshwater"
            )
            await create_test_species(
                async_session, "salt-fish", "Salt Fish", water_type="saltwater"
            )

            response = await client.get("/api/v1/species", params={"water_type": "saltwater"})

            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 1
            assert data["items"][0]["common_name"] == "Salt Fish"
        finally:
            await cleanup_data(async_session)

    async def test_list_species_empty_returns_empty_list(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that GET /species returns empty list when no species exist."""
        await cleanup_data(async_session)
        try:
            response = await client.get("/api/v1/species")

            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 0
            assert len(data["items"]) == 0
        finally:
            await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestSearchSpecies:
    """Tests for GET /species/search endpoint."""

    async def test_search_finds_by_common_name(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that search finds species by common name."""
        await cleanup_data(async_session)
        try:
            await create_test_species(
                async_session, "guppy", "Guppy", "Poecilia reticulata"
            )
            await create_test_species(
                async_session, "betta", "Betta Fish", "Betta splendens"
            )

            response = await client.get("/api/v1/species/search", params={"q": "gup"})

            assert response.status_code == 200
            data = response.json()
            assert len(data) >= 1
            assert any(s["common_name"] == "Guppy" for s in data)
        finally:
            await cleanup_data(async_session)

    async def test_search_finds_by_scientific_name(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that search finds species by scientific name."""
        await cleanup_data(async_session)
        try:
            await create_test_species(
                async_session, "neon-tetra", "Neon Tetra", "Paracheirodon innesi"
            )

            response = await client.get("/api/v1/species/search", params={"q": "paracheirodon"})

            assert response.status_code == 200
            data = response.json()
            assert len(data) >= 1
            assert any(s["scientific_name"] == "Paracheirodon innesi" for s in data)
        finally:
            await cleanup_data(async_session)

    async def test_search_too_short_query_returns_422(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that search with query < 2 chars returns 422."""
        response = await client.get("/api/v1/species/search", params={"q": "x"})
        assert response.status_code == 422

    async def test_search_no_matches_returns_empty_list(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that search with no matches returns empty list."""
        await cleanup_data(async_session)
        try:
            await create_test_species(async_session, "guppy", "Guppy")

            response = await client.get(
                "/api/v1/species/search", params={"q": "nonexistent"}
            )

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 0
        finally:
            await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestGetPopularSpecies:
    """Tests for GET /species/popular endpoint."""

    async def test_popular_returns_popular_species(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that GET /species/popular returns popular species."""
        await cleanup_data(async_session)
        try:
            await create_test_species(async_session, "betta", "Betta")
            await create_test_species(async_session, "guppy", "Guppy")
            await create_test_species(async_session, "rare-fish", "Rare Fish")

            response = await client.get("/api/v1/species/popular")

            assert response.status_code == 200
            data = response.json()
            # Only popular species should be returned
            ids = [s["id"] for s in data]
            assert "betta" in ids
            assert "guppy" in ids
            assert "rare-fish" not in ids
        finally:
            await cleanup_data(async_session)

    async def test_popular_returns_max_20_species(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that GET /species/popular returns at most 20 species."""
        await cleanup_data(async_session)
        try:
            response = await client.get("/api/v1/species/popular")

            assert response.status_code == 200
            data = response.json()
            assert len(data) <= 20
        finally:
            await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestGetSpeciesById:
    """Tests for GET /species/{species_id} endpoint."""

    async def test_get_species_returns_details(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that GET /species/{id} returns species details."""
        await cleanup_data(async_session)
        try:
            await create_test_species(
                async_session, "test-fish", "Test Fish", "Testus fishus"
            )

            response = await client.get("/api/v1/species/test-fish")

            assert response.status_code == 200
            data = response.json()
            assert data["id"] == "test-fish"
            assert data["common_name"] == "Test Fish"
            assert data["scientific_name"] == "Testus fishus"
        finally:
            await cleanup_data(async_session)

    async def test_get_species_not_found_returns_404(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that GET /species/{id} returns 404 for non-existent ID."""
        await cleanup_data(async_session)
        try:
            response = await client.get("/api/v1/species/non-existent-id")

            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()
        finally:
            await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestAdminCreateSpecies:
    """Tests for POST /admin/species endpoint."""

    async def test_create_species_without_auth_returns_401(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that POST /admin/species without auth returns 401."""
        response = await client.post(
            "/api/v1/admin/species",
            json={
                "id": "new-fish",
                "common_name": "New Fish",
                "food_types": ["flakes"],
            },
        )
        assert response.status_code == 401

    async def test_create_species_non_admin_returns_403(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that POST /admin/species with non-admin returns 403."""
        await cleanup_data(async_session)
        try:
            _, access_token = await create_test_user(
                async_session, "regular_species_test@example.com", is_admin=False
            )

            response = await client.post(
                "/api/v1/admin/species",
                json={
                    "id": "new-fish",
                    "common_name": "New Fish",
                    "food_types": ["flakes"],
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert response.status_code == 403
        finally:
            await cleanup_data(async_session)

    async def test_create_species_admin_success(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that POST /admin/species with admin creates species."""
        await cleanup_data(async_session)
        try:
            _, access_token = await create_test_user(
                async_session, "admin_species_test@example.com", is_admin=True
            )

            response = await client.post(
                "/api/v1/admin/species",
                json={
                    "id": "new-fish",
                    "common_name": "New Fish",
                    "scientific_name": "Novus piscis",
                    "food_types": ["flakes", "pellets"],
                    "feeding_frequency": 2,
                    "care_level": "beginner",
                    "water_type": "freshwater",
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert response.status_code == 201
            data = response.json()
            assert data["id"] == "new-fish"
            assert data["common_name"] == "New Fish"
        finally:
            await cleanup_data(async_session)

    async def test_create_species_duplicate_returns_409(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that POST /admin/species with existing ID returns 409."""
        await cleanup_data(async_session)
        try:
            await create_test_species(async_session, "existing-fish", "Existing Fish")
            _, access_token = await create_test_user(
                async_session, "admin_dup_species_test@example.com", is_admin=True
            )

            response = await client.post(
                "/api/v1/admin/species",
                json={
                    "id": "existing-fish",
                    "common_name": "Another Fish",
                    "food_types": ["flakes"],
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert response.status_code == 409
            assert "already exists" in response.json()["detail"].lower()
        finally:
            await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestAdminUpdateSpecies:
    """Tests for PUT /admin/species/{species_id} endpoint."""

    async def test_update_species_without_auth_returns_401(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that PUT /admin/species without auth returns 401."""
        response = await client.put(
            "/api/v1/admin/species/some-fish",
            json={"common_name": "Updated Name"},
        )
        assert response.status_code == 401

    async def test_update_species_non_admin_returns_403(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that PUT /admin/species with non-admin returns 403."""
        await cleanup_data(async_session)
        try:
            await create_test_species(async_session, "update-fish", "Original Name")
            _, access_token = await create_test_user(
                async_session, "regular_upd_species_test@example.com", is_admin=False
            )

            response = await client.put(
                "/api/v1/admin/species/update-fish",
                json={"common_name": "Updated Name"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert response.status_code == 403
        finally:
            await cleanup_data(async_session)

    async def test_update_species_admin_success(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that PUT /admin/species with admin updates species."""
        await cleanup_data(async_session)
        try:
            await create_test_species(async_session, "update-fish", "Original Name")
            _, access_token = await create_test_user(
                async_session, "admin_upd_species_test@example.com", is_admin=True
            )

            response = await client.put(
                "/api/v1/admin/species/update-fish",
                json={"common_name": "Updated Name", "care_level": "advanced"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["common_name"] == "Updated Name"
            assert data["care_level"] == "advanced"
        finally:
            await cleanup_data(async_session)

    async def test_update_species_not_found_returns_404(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that PUT /admin/species for non-existent returns 404."""
        await cleanup_data(async_session)
        try:
            _, access_token = await create_test_user(
                async_session, "admin_upd404_species_test@example.com", is_admin=True
            )

            response = await client.put(
                "/api/v1/admin/species/non-existent",
                json={"common_name": "Updated Name"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert response.status_code == 404
        finally:
            await cleanup_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestAdminDeleteSpecies:
    """Tests for DELETE /admin/species/{species_id} endpoint."""

    async def test_delete_species_without_auth_returns_401(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that DELETE /admin/species without auth returns 401."""
        response = await client.delete("/api/v1/admin/species/some-fish")
        assert response.status_code == 401

    async def test_delete_species_non_admin_returns_403(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that DELETE /admin/species with non-admin returns 403."""
        await cleanup_data(async_session)
        try:
            await create_test_species(async_session, "delete-fish", "Delete Fish")
            _, access_token = await create_test_user(
                async_session, "regular_del_species_test@example.com", is_admin=False
            )

            response = await client.delete(
                "/api/v1/admin/species/delete-fish",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert response.status_code == 403
        finally:
            await cleanup_data(async_session)

    async def test_delete_species_admin_success(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that DELETE /admin/species with admin deletes species."""
        await cleanup_data(async_session)
        try:
            await create_test_species(async_session, "delete-fish", "Delete Fish")
            _, access_token = await create_test_user(
                async_session, "admin_del_species_test@example.com", is_admin=True
            )

            response = await client.delete(
                "/api/v1/admin/species/delete-fish",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert response.status_code == 204

            # Verify species is deleted
            get_response = await client.get("/api/v1/species/delete-fish")
            assert get_response.status_code == 404
        finally:
            await cleanup_data(async_session)

    async def test_delete_species_not_found_returns_404(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that DELETE /admin/species for non-existent returns 404."""
        await cleanup_data(async_session)
        try:
            _, access_token = await create_test_user(
                async_session, "admin_del404_species_test@example.com", is_admin=True
            )

            response = await client.delete(
                "/api/v1/admin/species/non-existent",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert response.status_code == 404
        finally:
            await cleanup_data(async_session)
