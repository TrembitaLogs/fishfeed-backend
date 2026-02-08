"""Sync-specific exceptions."""

from uuid import UUID


class SyncError(Exception):
    """Base exception for sync errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class SyncValidationError(SyncError):
    """Raised when sync request validation fails."""

    def __init__(self, message: str):
        super().__init__(message, status_code=400)


class SyncAccessDeniedError(SyncError):
    """Raised when user doesn't have access to synced entities."""

    def __init__(self, entity_type: str, entity_id: UUID):
        super().__init__(f"Access denied to {entity_type} '{entity_id}'", status_code=403)
