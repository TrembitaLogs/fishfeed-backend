"""Tests for app.core.errors module."""

from app.core.errors import ErrorCode


def test_sync_invalid_entity_id_code_is_stable():
    """Once shipped, this string is part of the public client contract."""
    assert ErrorCode.SYNC_INVALID_ENTITY_ID.value == "sync.invalid_entity_id"
