"""Admin view for User model."""

from typing import Any

from sqladmin import ModelView
from starlette.requests import Request

from app.models.user import User


def _format_remove_ads(model: object, name: str) -> str:
    """Render the active Remove Ads projection without exposing raw settings."""
    del name
    settings = getattr(model, "settings", None)
    if not isinstance(settings, dict):
        return "No"
    non_subscriptions = settings.get("non_subscriptions")
    if not isinstance(non_subscriptions, dict):
        return "No"
    entitlements = non_subscriptions.get("entitlements")
    return "Yes" if isinstance(entitlements, list) and "remove_ads" in entitlements else "No"


class UserAdmin(ModelView, model=User):
    """User admin view — full CRUD except delete (use ban endpoint instead)."""

    column_list = [
        User.id,
        User.email,
        User.nickname,
        User.is_admin,
        User.subscription_status,
        User.settings,
        User.created_at,
        User.deleted_at,
    ]
    column_details_exclude_list = [User.password_hash]
    column_labels = {User.settings: "Remove Ads"}
    column_formatters = {User.settings: _format_remove_ads}  # type: ignore[dict-item]
    column_formatters_detail = {User.settings: _format_remove_ads}  # type: ignore[dict-item]
    form_columns = [User.email, User.nickname]
    column_searchable_list = [User.email, User.nickname]
    column_sortable_list = [User.email, User.created_at, User.subscription_status]

    can_delete = False
    name = "User"
    name_plural = "Users"
    icon = "fa-solid fa-user"

    async def _reject_unsafe_form_fields(self, request: Request) -> None:
        allowed = {"email", "nickname", "save"}
        unexpected = set((await request.form()).keys()) - allowed
        if unexpected:
            raise ValueError("User form contains read-only fields")

    async def insert_model(self, request: Request, data: dict) -> Any:
        await self._reject_unsafe_form_fields(request)
        return await super().insert_model(request, data)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        await self._reject_unsafe_form_fields(request)
        return await super().update_model(request, pk, data)
