from datetime import UTC, datetime
from uuid import uuid4

from app.schemas.sync import (
    DeletedEntities,
    FailedChange,
    ServerState,
    SyncResponse,
)


def test_failed_change_round_trips():
    fc = FailedChange(
        index=3,
        entity_type="schedule",
        entity_id="a74663b3-e1c1-4cb7-ad05-8e6a92af4f82_1530",
        error_code="sync.invalid_entity_id",
        error_message="entity_id is not a valid UUID",
    )
    assert fc.model_dump()["index"] == 3


def test_sync_response_defaults_failed_to_empty_list():
    resp = SyncResponse(
        server_state=ServerState(deleted=DeletedEntities()),
        sync_token="t",
    )
    assert resp.failed == []
