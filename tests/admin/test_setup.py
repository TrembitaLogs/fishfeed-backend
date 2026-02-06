"""Tests for admin panel setup and FastAPI integration."""

import pytest
from httpx import AsyncClient
from sqladmin import Admin

from app.admin.setup import setup_admin


@pytest.mark.asyncio(loop_scope="session")
class TestSetupAdmin:
    """Verify setup_admin integrates SQLAdmin with authentication."""

    async def test_setup_admin_returns_admin_instance(self, async_engine):
        """setup_admin should return a configured Admin instance."""
        from fastapi import FastAPI

        test_app = FastAPI()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.database.engine", async_engine)
            result = setup_admin(test_app)

        assert isinstance(result, Admin)

    async def test_admin_redirects_to_login(self, admin_client: AsyncClient):
        """/admin without session should redirect to /admin/login."""
        response = await admin_client.get("/admin/", follow_redirects=False)

        assert response.status_code in (301, 302, 303, 307)
        assert "/admin/login" in response.headers.get("location", "")

    async def test_admin_login_page_renders(self, admin_client: AsyncClient):
        """The /admin/login page should return HTML with the panel title."""
        response = await admin_client.get("/admin/login")

        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert "FishFeed Admin" in response.text
