"""Tests for sync Pydantic schemas."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.sync import (
    ChangeItem,
    ConflictItem,
    ServerState,
    SyncRequest,
    SyncResponse,
)


class TestChangeItem:
    """Tests for ChangeItem schema."""

    def test_valid_create_operation(self):
        """Test valid ChangeItem with create operation."""
        entity_id = uuid4()
        now = datetime.now(UTC)

        change = ChangeItem(
            entity_type="aquarium",
            entity_id=entity_id,
            operation="create",
            data={"name": "My Aquarium"},
            client_updated_at=now,
        )

        assert change.entity_type == "aquarium"
        assert change.entity_id == entity_id
        assert change.operation == "create"
        assert change.data == {"name": "My Aquarium"}
        assert change.client_updated_at == now

    def test_valid_update_operation(self):
        """Test valid ChangeItem with update operation."""
        change = ChangeItem(
            entity_type="fish",
            entity_id=uuid4(),
            operation="update",
            data={"name": "Nemo", "species_id": str(uuid4())},
            client_updated_at=datetime.now(UTC),
        )

        assert change.operation == "update"
        assert "name" in change.data

    def test_valid_delete_operation(self):
        """Test valid ChangeItem with delete operation."""
        change = ChangeItem(
            entity_type="feeding_log",
            entity_id=uuid4(),
            operation="delete",
            client_updated_at=datetime.now(UTC),
        )

        assert change.operation == "delete"
        assert change.data == {}

    def test_all_entity_types_valid(self):
        """Test that all entity types are valid."""
        now = datetime.now(UTC)

        for entity_type in ["aquarium", "fish", "feeding_log"]:
            change = ChangeItem(
                entity_type=entity_type,
                entity_id=uuid4(),
                operation="create",
                data={},
                client_updated_at=now,
            )
            assert change.entity_type == entity_type

    def test_invalid_entity_type_rejected(self):
        """Test that invalid entity_type is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ChangeItem(
                entity_type="unknown",
                entity_id=uuid4(),
                operation="create",
                data={},
                client_updated_at=datetime.now(UTC),
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("entity_type",)

    def test_invalid_operation_rejected(self):
        """Test that invalid operation is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ChangeItem(
                entity_type="aquarium",
                entity_id=uuid4(),
                operation="merge",
                data={},
                client_updated_at=datetime.now(UTC),
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("operation",)

    def test_invalid_entity_id_rejected(self):
        """Test that invalid entity_id is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ChangeItem(
                entity_type="aquarium",
                entity_id="not-a-uuid",
                operation="create",
                data={},
                client_updated_at=datetime.now(UTC),
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("entity_id",)

    def test_data_defaults_to_empty_dict(self):
        """Test that data defaults to empty dict."""
        change = ChangeItem(
            entity_type="aquarium",
            entity_id=uuid4(),
            operation="delete",
            client_updated_at=datetime.now(UTC),
        )

        assert change.data == {}

    def test_json_serialization(self):
        """Test ChangeItem can be serialized to JSON."""
        change = ChangeItem(
            entity_type="aquarium",
            entity_id=uuid4(),
            operation="create",
            data={"name": "Test"},
            client_updated_at=datetime.now(UTC),
        )

        data = change.model_dump()
        assert data["entity_type"] == "aquarium"
        assert data["operation"] == "create"


class TestSyncRequest:
    """Tests for SyncRequest schema."""

    def test_valid_sync_request_with_changes(self):
        """Test valid SyncRequest with changes."""
        now = datetime.now(UTC)
        change = ChangeItem(
            entity_type="aquarium",
            entity_id=uuid4(),
            operation="create",
            data={"name": "Aquarium"},
            client_updated_at=now,
        )

        request = SyncRequest(
            changes=[change],
            last_sync_at=now,
        )

        assert len(request.changes) == 1
        assert request.last_sync_at == now

    def test_initial_sync_with_null_last_sync_at(self):
        """Test initial sync with None last_sync_at."""
        request = SyncRequest(
            changes=[],
            last_sync_at=None,
        )

        assert request.changes == []
        assert request.last_sync_at is None

    def test_default_values(self):
        """Test SyncRequest default values."""
        request = SyncRequest()

        assert request.changes == []
        assert request.last_sync_at is None

    def test_multiple_changes(self):
        """Test SyncRequest with multiple changes."""
        now = datetime.now(UTC)
        changes = [
            ChangeItem(
                entity_type="aquarium",
                entity_id=uuid4(),
                operation="create",
                data={"name": "Aquarium 1"},
                client_updated_at=now,
            ),
            ChangeItem(
                entity_type="fish",
                entity_id=uuid4(),
                operation="update",
                data={"name": "Nemo"},
                client_updated_at=now,
            ),
            ChangeItem(
                entity_type="feeding_log",
                entity_id=uuid4(),
                operation="delete",
                client_updated_at=now,
            ),
        ]

        request = SyncRequest(changes=changes, last_sync_at=now)

        assert len(request.changes) == 3
        assert request.changes[0].entity_type == "aquarium"
        assert request.changes[1].entity_type == "fish"
        assert request.changes[2].entity_type == "feeding_log"

    def test_json_serialization(self):
        """Test SyncRequest can be serialized to JSON."""
        now = datetime.now(UTC)
        change = ChangeItem(
            entity_type="aquarium",
            entity_id=uuid4(),
            operation="create",
            data={"name": "Test"},
            client_updated_at=now,
        )

        request = SyncRequest(changes=[change], last_sync_at=now)
        data = request.model_dump()

        assert len(data["changes"]) == 1
        assert data["changes"][0]["entity_type"] == "aquarium"

    def test_json_deserialization(self):
        """Test SyncRequest can be deserialized from JSON."""
        entity_id = uuid4()
        now = datetime.now(UTC)

        json_data = {
            "changes": [
                {
                    "entity_type": "aquarium",
                    "entity_id": str(entity_id),
                    "operation": "create",
                    "data": {"name": "Test Aquarium"},
                    "client_updated_at": now.isoformat(),
                }
            ],
            "last_sync_at": now.isoformat(),
        }

        request = SyncRequest.model_validate(json_data)

        assert len(request.changes) == 1
        assert request.changes[0].entity_id == entity_id
        assert request.last_sync_at is not None


class TestConflictItem:
    """Tests for ConflictItem schema."""

    def test_valid_conflict_item(self):
        """Test valid ConflictItem creation."""
        entity_id = uuid4()
        client_time = datetime.now(UTC)
        server_time = datetime.now(UTC)

        conflict = ConflictItem(
            entity_type="aquarium",
            entity_id=entity_id,
            client_data={"name": "Client Aquarium"},
            server_data={"name": "Server Aquarium"},
            client_updated_at=client_time,
            server_updated_at=server_time,
            resolution="server_wins",
        )

        assert conflict.entity_type == "aquarium"
        assert conflict.entity_id == entity_id
        assert conflict.client_data == {"name": "Client Aquarium"}
        assert conflict.server_data == {"name": "Server Aquarium"}
        assert conflict.resolution == "server_wins"

    def test_conflict_with_different_resolutions(self):
        """Test ConflictItem with different resolution types."""
        resolutions = ["server_wins", "client_wins", "merged", "manual_required"]

        for resolution in resolutions:
            conflict = ConflictItem(
                entity_type="fish",
                entity_id=uuid4(),
                client_data={"name": "A"},
                server_data={"name": "B"},
                client_updated_at=datetime.now(UTC),
                server_updated_at=datetime.now(UTC),
                resolution=resolution,
            )
            assert conflict.resolution == resolution

    def test_json_serialization(self):
        """Test ConflictItem can be serialized to JSON."""
        conflict = ConflictItem(
            entity_type="feeding_log",
            entity_id=uuid4(),
            client_data={"status": "completed"},
            server_data={"status": "pending"},
            client_updated_at=datetime.now(UTC),
            server_updated_at=datetime.now(UTC),
            resolution="server_wins",
        )

        data = conflict.model_dump()
        assert data["entity_type"] == "feeding_log"
        assert data["resolution"] == "server_wins"


class TestServerState:
    """Tests for ServerState schema."""

    def test_empty_server_state(self):
        """Test empty ServerState."""
        state = ServerState()

        assert state.aquariums == []
        assert state.fish == []
        assert state.feeding_logs == []

    def test_server_state_with_data(self):
        """Test ServerState with data."""
        aquarium_id = uuid4()
        fish_id = uuid4()
        log_id = uuid4()

        state = ServerState(
            aquariums=[{"id": str(aquarium_id), "name": "My Aquarium"}],
            fish=[{"id": str(fish_id), "name": "Nemo"}],
            feeding_logs=[{"id": str(log_id), "action": "fed"}],
        )

        assert len(state.aquariums) == 1
        assert len(state.fish) == 1
        assert len(state.feeding_logs) == 1

    def test_default_values(self):
        """Test ServerState default values."""
        state = ServerState()

        assert state.aquariums == []
        assert state.fish == []
        assert state.feeding_logs == []

    def test_json_serialization(self):
        """Test ServerState can be serialized to JSON."""
        state = ServerState(
            aquariums=[{"id": str(uuid4()), "name": "Test"}],
            fish=[],
            feeding_logs=[],
        )

        data = state.model_dump()
        assert len(data["aquariums"]) == 1
        assert data["fish"] == []
        assert data["feeding_logs"] == []


class TestSyncResponse:
    """Tests for SyncResponse schema."""

    def test_valid_sync_response(self):
        """Test valid SyncResponse creation."""
        state = ServerState(
            aquariums=[{"id": str(uuid4()), "name": "Aquarium"}],
            fish=[],
            feeding_logs=[],
        )

        response = SyncResponse(
            server_state=state,
            conflicts=[],
            sync_token="abc123",
        )

        assert response.server_state.aquariums[0]["name"] == "Aquarium"
        assert response.conflicts == []
        assert response.sync_token == "abc123"

    def test_sync_response_with_conflicts(self):
        """Test SyncResponse with conflicts."""
        conflict = ConflictItem(
            entity_type="aquarium",
            entity_id=uuid4(),
            client_data={"name": "Client"},
            server_data={"name": "Server"},
            client_updated_at=datetime.now(UTC),
            server_updated_at=datetime.now(UTC),
            resolution="server_wins",
        )

        response = SyncResponse(
            server_state=ServerState(),
            conflicts=[conflict],
            sync_token="def456",
        )

        assert len(response.conflicts) == 1
        assert response.conflicts[0].resolution == "server_wins"

    def test_default_conflicts_list(self):
        """Test SyncResponse default conflicts list."""
        response = SyncResponse(
            server_state=ServerState(),
            conflicts=[],
            sync_token="token",
        )

        assert response.conflicts == []

    def test_json_serialization(self):
        """Test SyncResponse can be serialized to JSON."""
        state = ServerState(
            aquariums=[{"id": str(uuid4()), "name": "Test"}],
            fish=[{"id": str(uuid4()), "name": "Nemo"}],
            feeding_logs=[],
        )

        response = SyncResponse(
            server_state=state,
            conflicts=[],
            sync_token="test-token-123",
        )

        data = response.model_dump()
        assert len(data["server_state"]["aquariums"]) == 1
        assert len(data["server_state"]["fish"]) == 1
        assert data["sync_token"] == "test-token-123"

    def test_json_deserialization(self):
        """Test SyncResponse can be deserialized from JSON."""
        json_data = {
            "server_state": {
                "aquariums": [{"id": str(uuid4()), "name": "Aquarium"}],
                "fish": [],
                "feeding_logs": [],
            },
            "conflicts": [],
            "sync_token": "my-sync-token",
        }

        response = SyncResponse.model_validate(json_data)

        assert len(response.server_state.aquariums) == 1
        assert response.sync_token == "my-sync-token"

    def test_full_round_trip(self):
        """Test full serialization/deserialization round trip."""
        entity_id = uuid4()
        now = datetime.now(UTC)

        original = SyncResponse(
            server_state=ServerState(
                aquariums=[{"id": str(entity_id), "name": "Test"}],
                fish=[],
                feeding_logs=[],
            ),
            conflicts=[
                ConflictItem(
                    entity_type="fish",
                    entity_id=entity_id,
                    client_data={"name": "A"},
                    server_data={"name": "B"},
                    client_updated_at=now,
                    server_updated_at=now,
                    resolution="merged",
                )
            ],
            sync_token="round-trip-token",
        )

        json_str = original.model_dump_json()
        restored = SyncResponse.model_validate_json(json_str)

        assert len(restored.server_state.aquariums) == 1
        assert len(restored.conflicts) == 1
        assert restored.sync_token == "round-trip-token"
