"""Admin authentication backend using static env-based credentials."""

import hmac

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request

from app.config import get_settings
from app.utils.password import verify_password


class AdminAuth(AuthenticationBackend):
    """Cookie-based session authentication for the SQLAdmin panel.

    Credentials are configured via ADMIN_USERNAME / ADMIN_PASSWORD env vars.
    ADMIN_PASSWORD must be an argon2id hash (generate with the hash-admin-password script).
    If either is empty, admin login is disabled.
    """

    async def login(self, request: Request) -> bool:
        """Validate admin credentials from the login form.

        SQLAdmin sends form fields "username" and "password".
        Returns True on success (sets session), False to re-show login form.
        """
        settings = get_settings()
        if not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD.get_secret_value():
            return False

        form = await request.form()
        username = form.get("username", "")
        password = form.get("password", "")

        if not username or not password:
            return False

        username_ok = hmac.compare_digest(str(username), settings.ADMIN_USERNAME)
        password_ok = verify_password(str(password), settings.ADMIN_PASSWORD.get_secret_value())

        if not username_ok or not password_ok:
            return False

        request.session.update({"admin": True})
        return True

    async def logout(self, request: Request) -> bool:
        """Clear admin session on logout."""
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        """Check whether the current request has a valid admin session."""
        return request.session.get("admin", False) is True
