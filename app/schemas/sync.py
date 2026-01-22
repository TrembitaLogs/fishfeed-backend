"""Pydantic schemas for data synchronization endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

EntityType = Literal["aquarium", "fish", "event"]
OperationType = Literal["create", "update", "delete"]


class ChangeItem(BaseModel):
    """Schema for a single change item in sync request."""

    entity_type: EntityType = Field(
        description="Type of entity being changed"
    )
    entity_id: UUID = Field(
        description="Unique identifier of the entity"
    )
    operation: OperationType = Field(
        description="Type of operation performed on the entity"
    )
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Entity data for create/update operations"
    )
    client_updated_at: datetime = Field(
        description="Timestamp when the change was made on the client"
    )


class SyncRequest(BaseModel):
    """Schema for sync request from client."""

    changes: list[ChangeItem] = Field(
        default_factory=list,
        description="List of changes made on the client since last sync"
    )
    last_sync_at: datetime | None = Field(
        default=None,
        description="Timestamp of the last successful sync (None for initial sync)"
    )
    cursor: str | None = Field(
        default=None,
        description="Pagination cursor from previous response (for fetching next page)"
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=500,
        description="Number of items per page (1-500, default 100)"
    )


class ConflictItem(BaseModel):
    """Schema for a conflict detected during sync."""

    entity_type: EntityType = Field(
        description="Type of entity with conflict"
    )
    entity_id: UUID = Field(
        description="Unique identifier of the conflicting entity"
    )
    client_data: dict[str, Any] = Field(
        description="Data from the client"
    )
    server_data: dict[str, Any] = Field(
        description="Data from the server"
    )
    client_updated_at: datetime = Field(
        description="Timestamp of client's version"
    )
    server_updated_at: datetime = Field(
        description="Timestamp of server's version"
    )
    resolution: str = Field(
        description="Description of how the conflict was resolved (e.g., 'server_wins', 'client_wins', 'merged')"
    )


class DeletedEntities(BaseModel):
    """Schema for tracking deleted entities in delta sync."""

    aquariums: list[UUID] = Field(
        default_factory=list,
        description="List of deleted aquarium IDs"
    )
    fish: list[UUID] = Field(
        default_factory=list,
        description="List of deleted fish IDs"
    )


class ServerState(BaseModel):
    """Schema for server state in sync response."""

    aquariums: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of aquarium entities"
    )
    fish: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of fish entities"
    )
    events: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of feeding event entities"
    )
    deleted: DeletedEntities = Field(
        default_factory=DeletedEntities,
        description="Deleted entity IDs for delta sync (soft-deleted entities only)"
    )


class SyncResponse(BaseModel):
    """Schema for sync response to client."""

    server_state: ServerState = Field(
        description="Current server state (delta or full depending on request)"
    )
    conflicts: list[ConflictItem] = Field(
        default_factory=list,
        description="List of conflicts detected during sync"
    )
    sync_token: str = Field(
        description="Token to use for the next sync request"
    )
    has_more: bool = Field(
        default=False,
        description="Whether there are more items to fetch (pagination)"
    )
    next_cursor: str | None = Field(
        default=None,
        description="Cursor for fetching next page (None if no more pages)"
    )


# ============ Mobile App Sync Schemas ============
# These schemas support the mobile app's simplified sync format


class MobileFeedingEvent(BaseModel):
    """Schema for a feeding event from mobile app."""

    id: str = Field(description="Event ID (UUID as string)")
    local_id: str | None = Field(default=None, description="Local ID on device")
    fish_id: str | None = Field(default=None, description="Fish ID")
    aquarium_id: str | None = Field(default=None, description="Aquarium ID")
    feeding_time: datetime = Field(description="When the feeding occurred")
    amount: float | None = Field(default=None, description="Amount of food")
    food_type: str | None = Field(default=None, description="Type of food")
    notes: str | None = Field(default=None, description="Notes")
    created_at: datetime = Field(description="When the event was created")
    updated_at: datetime | None = Field(default=None, description="Last update time")
    completed_by: str | None = Field(default=None, description="User ID who completed")
    completed_by_name: str | None = Field(default=None, description="Name of who completed")
    completed_by_avatar: str | None = Field(default=None, description="Avatar URL")


class MobileSyncRequest(BaseModel):
    """Schema for sync request from mobile app.

    Mobile app sends feeding events in a simplified format.
    """

    events: list[MobileFeedingEvent] = Field(
        default_factory=list,
        description="List of feeding events to sync"
    )
    client_timestamp: datetime = Field(
        description="Client timestamp when sync was initiated"
    )


class MobileSyncResponse(BaseModel):
    """Schema for sync response to mobile app.

    Mobile app expects synced_ids and optional server_events.
    """

    synced_ids: list[str] = Field(
        default_factory=list,
        description="List of event IDs that were successfully synced"
    )
    server_events: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Events from server that client should apply"
    )
