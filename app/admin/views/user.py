"""Admin view for User model."""

from sqladmin import ModelView

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
    form_excluded_columns = [User.password_hash, User.refresh_tokens]
    column_searchable_list = [User.email, User.nickname]
    column_sortable_list = [User.email, User.created_at, User.subscription_status]

    can_delete = False
    name = "User"
    name_plural = "Users"
    icon = "fa-solid fa-user"
