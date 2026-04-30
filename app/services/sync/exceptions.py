"""Sync-specific exceptions."""

from uuid import UUID

from app.core.errors import AppError, ErrorCode


class SyncError(AppError):
    """Base class for sync errors. Subclass per concrete failure mode."""


class SyncValidationError(SyncError):
    """Raised when sync request validation fails."""

    def __init__(self, message: str):
        super().__init__(ErrorCode.SYNC_VALIDATION, message, status_code=400)


class SyncAccessDeniedError(SyncError):
    """Raised when user doesn't have access to synced entities."""

    def __init__(self, entity_type: str, entity_id: UUID):
        super().__init__(
            ErrorCode.SYNC_ACCESS_DENIED,
            f"Access denied to {entity_type} '{entity_id}'",
            status_code=403,
        )
