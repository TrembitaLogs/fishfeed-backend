"""Admin view for User model."""

from typing import Any

from sqladmin import ModelView
from starlette.requests import Request

from app.models.user import User


class UserAdmin(ModelView, model=User):
    """User admin view — full CRUD except delete (use ban endpoint instead)."""

    column_list = [
        User.id,
        User.email,
        User.nickname,
        User.is_admin,
        User.subscription_status,
        User.created_at,
        User.deleted_at,
    ]
    column_details_exclude_list = [User.password_hash]
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
