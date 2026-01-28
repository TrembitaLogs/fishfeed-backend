"""Tests for mobile releases endpoints."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient


@pytest.fixture()
def releases_dir(tmp_path: Path) -> Path:
    """Create a temporary releases directory with test data."""
    apk_dir = tmp_path / "v1.0.0"
    apk_dir.mkdir()
    apk_file = apk_dir / "FishFeed-v1.0.0.apk"
    apk_file.write_bytes(b"\x00" * 1024 * 1024 * 10)  # 10 MB

    index = {
        "releases": [
            {
                "version": "v1.0.0",
                "apk": "v1.0.0/FishFeed-v1.0.0.apk",
                "notes": "First release",
            }
        ]
    }
    (tmp_path / "index.json").write_text(json.dumps(index))
    (tmp_path / "release-notes.txt").write_text("Test notes")
    return tmp_path


@pytest.mark.asyncio(loop_scope="session")
class TestReleasesPage:
    """Tests for GET /mobile/releases/ endpoint."""

    async def test_releases_page_returns_html(self, client: AsyncClient):
        response = await client.get("/mobile/releases/", follow_redirects=True)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "FishFeed Releases" in response.text

    async def test_releases_page_contains_js_fetch(self, client: AsyncClient):
        response = await client.get("/mobile/releases/", follow_redirects=True)
        assert "fetch('index.json')" in response.text


@pytest.mark.asyncio(loop_scope="session")
class TestReleasesIndex:
    """Tests for GET /mobile/releases/index.json endpoint."""

    async def test_index_returns_enriched_json(
        self, client: AsyncClient, releases_dir: Path
    ):
        with patch("app.api.releases.get_settings") as mock_settings:
            mock_settings.return_value.RELEASES_DIR = str(releases_dir)
            response = await client.get("/mobile/releases/index.json")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["version"] == "v1.0.0"
        assert data[0]["size_mb"] == 10.0
        assert "date" in data[0]
        assert data[0]["notes"] == "First release"

    async def test_index_missing_returns_404(
        self, client: AsyncClient, tmp_path: Path
    ):
        with patch("app.api.releases.get_settings") as mock_settings:
            mock_settings.return_value.RELEASES_DIR = str(tmp_path)
            response = await client.get("/mobile/releases/index.json")

        assert response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
class TestReleasesFile:
    """Tests for GET /mobile/releases/{file_path} endpoint."""

    async def test_serve_existing_file(
        self, client: AsyncClient, releases_dir: Path
    ):
        with patch("app.api.releases.get_settings") as mock_settings:
            mock_settings.return_value.RELEASES_DIR = str(releases_dir)
            response = await client.get("/mobile/releases/release-notes.txt")

        assert response.status_code == 200
        assert response.text == "Test notes"

    async def test_serve_apk_file(
        self, client: AsyncClient, releases_dir: Path
    ):
        with patch("app.api.releases.get_settings") as mock_settings:
            mock_settings.return_value.RELEASES_DIR = str(releases_dir)
            response = await client.get(
                "/mobile/releases/v1.0.0/FishFeed-v1.0.0.apk"
            )

        assert response.status_code == 200

    async def test_missing_file_returns_404(
        self, client: AsyncClient, releases_dir: Path
    ):
        with patch("app.api.releases.get_settings") as mock_settings:
            mock_settings.return_value.RELEASES_DIR = str(releases_dir)
            response = await client.get("/mobile/releases/nonexistent.apk")

        assert response.status_code == 404

    async def test_path_traversal_blocked(
        self, client: AsyncClient, releases_dir: Path
    ):
        with patch("app.api.releases.get_settings") as mock_settings:
            mock_settings.return_value.RELEASES_DIR = str(releases_dir)
            response = await client.get("/mobile/releases/../../etc/passwd")

        assert response.status_code in (403, 404)
