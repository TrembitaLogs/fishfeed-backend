"""Admin view for database backups — dashboard + history + actions."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from markupsafe import Markup
from sqladmin import BaseView, ModelView, expose
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.models.database_backup import BackupSettings, DatabaseBackup
from app.services.backup_service import (
    get_backup_stats,
    run_backup,
    update_settings,
)

logger = structlog.get_logger(__name__)


def _form_str(value: str | UploadFile | None, default: str) -> str:
    if isinstance(value, str):
        return value
    return default


def _fmt_size_col(model: Any, _attr: str) -> str:
    return _format_bytes(model.size_bytes)


def _fmt_duration_col(model: Any, _attr: str) -> str:
    return _format_duration(model.duration_seconds)


def _fmt_status_col(model: Any, _attr: str) -> Markup:
    return _format_status(model.status.value)


def _format_bytes(n: int | None) -> str:
    if not n:
        return "—"
    units = ["B", "KB", "MB", "GB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _format_status(status: str) -> Markup:
    color = {"ok": "green", "running": "blue", "failed": "red"}.get(status, "gray")
    return Markup(f'<span class="badge bg-{color}">{status.upper()}</span>')


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m {secs}s"


class BackupsDashboardView(BaseView):
    """Custom dashboard — summary cards + settings form + manual trigger."""

    name = "Database Backups"
    icon = "fa-solid fa-database"

    @expose("/backups", methods=["GET"], identity="backups-dashboard")
    async def dashboard(self, request: Request) -> Response:
        stats = await get_backup_stats()
        return await self.templates.TemplateResponse(
            request,
            "backups_dashboard.html",
            context={
                "stats": stats,
                "fmt_bytes": _format_bytes,
                "fmt_status": _format_status,
                "fmt_duration": _format_duration,
                "now": datetime.now(UTC),
            },
        )

    @expose("/backups/trigger", methods=["POST"], identity="backups-trigger")
    async def trigger(self, request: Request) -> Response:
        # Fire and forget: user sees the new "running" record immediately on
        # redirect, then the real status once the dump finishes.
        asyncio.create_task(run_backup(triggered_by="manual"))
        return RedirectResponse(
            url=str(request.url_for("admin:backups-dashboard")), status_code=303
        )

    @expose("/backups/settings", methods=["POST"], identity="backups-save-settings")
    async def save_settings(self, request: Request) -> Response:
        form = await request.form()
        try:
            interval = int(_form_str(form.get("interval_hours"), "24"))
            retention = int(_form_str(form.get("retention_days"), "7"))
            enabled = _form_str(form.get("enabled"), "") == "on"
            await update_settings(
                interval_hours=interval,
                retention_days=retention,
                enabled=enabled,
            )
        except (ValueError, TypeError) as exc:
            logger.warning("Invalid backup settings", error=str(exc))
        return RedirectResponse(
            url=str(request.url_for("admin:backups-dashboard")), status_code=303
        )


class DatabaseBackupAdmin(ModelView, model=DatabaseBackup):
    """Raw history table — useful for debugging, hidden from main nav."""

    name = "Backup History"
    name_plural = "Backup History"
    icon = "fa-solid fa-clock-rotate-left"
    category = "Database Backups"

    column_list = [
        DatabaseBackup.started_at,
        DatabaseBackup.status,
        DatabaseBackup.filename,
        DatabaseBackup.size_bytes,
        DatabaseBackup.duration_seconds,
        DatabaseBackup.storage,
        DatabaseBackup.triggered_by,
    ]
    column_default_sort = [(DatabaseBackup.started_at, True)]
    column_sortable_list = [DatabaseBackup.started_at, DatabaseBackup.status]
    column_searchable_list = [DatabaseBackup.filename]
    column_formatters = {
        DatabaseBackup.size_bytes: _fmt_size_col,  # type: ignore[dict-item]
        DatabaseBackup.duration_seconds: _fmt_duration_col,  # type: ignore[dict-item]
        DatabaseBackup.status: _fmt_status_col,  # type: ignore[dict-item]
    }

    can_create = False
    can_edit = False
    can_delete = False


class BackupSettingsAdmin(ModelView, model=BackupSettings):
    """Allow raw edit of the single settings row for power users."""

    name = "Settings"
    name_plural = "Settings"
    icon = "fa-solid fa-sliders"
    category = "Database Backups"

    column_list = [
        BackupSettings.id,
        BackupSettings.interval_hours,
        BackupSettings.retention_days,
        BackupSettings.enabled,
        BackupSettings.updated_at,
    ]
    form_columns = [
        BackupSettings.interval_hours,
        BackupSettings.retention_days,
        BackupSettings.enabled,
    ]
    can_create = False
    can_delete = False
