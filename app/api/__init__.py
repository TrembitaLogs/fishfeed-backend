"""API routers package."""

from app.api.admin import router as admin_router
from app.api.ai import router as ai_router
from app.api.aquariums import router as aquariums_router
from app.api.auth import router as auth_router
from app.api.family import router as family_router
from app.api.feeding import router as feeding_router
from app.api.fish import router as fish_router
from app.api.gamification import router as gamification_router
from app.api.health import router as health_router
from app.api.purchase import router as purchase_router
from app.api.push import router as push_router
from app.api.species import admin_router as species_admin_router
from app.api.species import router as species_router
from app.api.sync import router as sync_router
from app.api.users import router as users_router

__all__ = [
    "admin_router",
    "ai_router",
    "auth_router",
    "species_router",
    "species_admin_router",
    "aquariums_router",
    "fish_router",
    "feeding_router",
    "sync_router",
    "family_router",
    "push_router",
    "gamification_router",
    "health_router",
    "purchase_router",
    "users_router",
]
