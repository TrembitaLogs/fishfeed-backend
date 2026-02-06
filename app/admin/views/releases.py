"""Admin view for mobile releases management."""

import json
import mimetypes
from datetime import UTC, datetime
from pathlib import Path

from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from app.config import get_settings


def _load_releases() -> tuple[list[dict], Path]:
    """Load releases index and return (releases_list, base_dir)."""
    settings = get_settings()
    base = Path(settings.RELEASES_DIR).resolve()
    index_file = base / "index.json"

    if not index_file.is_file():
        return [], base

    data = json.loads(index_file.read_text())
    releases = data.get("releases", data) if isinstance(data, dict) else data

    for release in releases:
        apk_path = base / (release.get("apk") or release.get("file", ""))
        if apk_path.is_file():
            stat = apk_path.stat()
            release["size_mb"] = f"{stat.st_size / (1024 * 1024):.1f}"
            release["date"] = datetime.fromtimestamp(
                stat.st_mtime, tz=UTC
            ).strftime("%Y-%m-%d %H:%M")

        ver = release.get("version", "")
        release["version_display"] = ver if ver.startswith("v") else f"v{ver}"

    return releases, base


class ReleasesView(BaseView):
    """Custom admin page for browsing and downloading mobile APK releases."""

    name = "Mobile Releases"
    icon = "fa-solid fa-mobile-screen"

    @expose("/releases", methods=["GET"], identity="releases-list")
    async def releases_list(self, request: Request) -> Response:
        """Show all available releases."""
        releases, _ = _load_releases()
        return await self.templates.TemplateResponse(
            request,
            "releases_list.html",
            context={"releases": releases},
        )

    @expose("/releases/detail", methods=["GET"], identity="releases-detail")
    async def releases_detail(self, request: Request) -> Response:
        """Show detail page for a specific release version."""
        version = request.query_params.get("v", "")
        releases, _ = _load_releases()

        norm = version.lstrip("v")
        release = None
        is_latest = False
        for idx, r in enumerate(releases):
            if r["version"].lstrip("v") == norm:
                release = r
                is_latest = idx == 0
                break

        if release is None:
            return await self.templates.TemplateResponse(
                request,
                "releases_list.html",
                context={"releases": releases},
            )

        return await self.templates.TemplateResponse(
            request,
            "releases_detail.html",
            context={"release": release, "is_latest": is_latest},
        )

    @expose(
        "/releases/download/{file_path:path}",
        methods=["GET"],
        identity="releases-download",
    )
    async def releases_download(self, request: Request) -> Response:
        """Serve APK and other release files for download."""
        file_path = request.path_params["file_path"]
        settings = get_settings()
        base = Path(settings.RELEASES_DIR).resolve()
        target = (base / file_path).resolve()

        if not target.is_relative_to(base) or not target.is_file():
            return Response("Not found", status_code=404)

        media_type, _ = mimetypes.guess_type(str(target))
        return FileResponse(
            path=target, media_type=media_type or "application/octet-stream"
        )
