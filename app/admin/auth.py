"""Admin authentication backend using cookie-based sessions."""

from sqladmin.authentication import AuthenticationBackend
from sqlalchemy import select
from starlette.requests import Request

from app.database import async_session_maker
from app.models.user import User
from app.utils.password import verify_password


class AdminAuth(AuthenticationBackend):
    """Cookie-based session authentication for the SQLAdmin panel."""

    async def login(self, request: Request) -> bool:
        """Validate admin credentials from the login form.

        SQLAdmin sends form fields "username" (email) and "password".
        Returns True on success (sets session), False to re-show login form.
        """
        form = await request.form()
        email = form.get("username")
        password = form.get("password")

        if not email or not password:
            return False

        async with async_session_maker() as db:
            stmt = select(User).where(
                User.email == email,
                User.deleted_at.is_(None),
            )
            result = await db.execute(stmt)
            user = result.scalar_one_or_none()

        if user is None or user.password_hash is None:
            return False
        if not verify_password(str(password), user.password_hash):
            return False
        if not user.is_admin:
            return False

        request.session.update({"admin_user_id": str(user.id)})
        return True

    async def logout(self, request: Request) -> bool:
        """Clear admin session on logout."""
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        """Check whether the current request has a valid admin session.

        Called on every request to a protected admin page.
        """
        admin_user_id = request.session.get("admin_user_id")
        if not admin_user_id:
            return False

        async with async_session_maker() as db:
            stmt = select(User).where(
                User.id == admin_user_id,
                User.is_admin.is_(True),
                User.deleted_at.is_(None),
            )
            result = await db.execute(stmt)
            user = result.scalar_one_or_none()

        return user is not None
