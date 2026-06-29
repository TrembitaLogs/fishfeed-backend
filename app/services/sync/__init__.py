"""Sync service package — offline-first data synchronization.

Re-exports all public symbols for backwards compatibility with
``from app.services.sync import ...``.
"""

from .changes import (
    _apply_achievement_change,
    _apply_aquarium_change,
    _apply_feeding_log_change,
    _apply_fish_change,
    _apply_progress_change,
    _apply_schedule_change,
    _apply_schedule_fields,
    _apply_streak_change,
    _ensure_schedules_for_user,
    _get_user_nickname,
    apply_changes,
)
from .exceptions import SyncAccessDeniedError, SyncError, SyncValidationError
from .process import process_sync
from .state import _apply_pagination, get_paginated_server_state, get_server_state
from .utils import (
    _entity_to_dict,
    _generate_sync_token,
    _group_changes_by_entity_type,
    resolve_conflict,
)
from .validation import _get_user_aquarium_ids, _validate_entity_ownership

__all__ = [
    # Exceptions
    "SyncError",
    "SyncValidationError",
    "SyncAccessDeniedError",
    # Main entry point
    "process_sync",
    # Change application
    "apply_changes",
    "_apply_aquarium_change",
    "_apply_fish_change",
    "_apply_feeding_log_change",
    "_apply_schedule_change",
    "_apply_streak_change",
    "_apply_achievement_change",
    "_apply_progress_change",
    "_apply_schedule_fields",
    "_get_user_nickname",
    "_ensure_schedules_for_user",
    # Server state
    "get_server_state",
    "get_paginated_server_state",
    "_apply_pagination",
    # Utilities
    "_generate_sync_token",
    "resolve_conflict",
    "_entity_to_dict",
    "_group_changes_by_entity_type",
    # Validation
    "_get_user_aquarium_ids",
    "_validate_entity_ownership",
]
