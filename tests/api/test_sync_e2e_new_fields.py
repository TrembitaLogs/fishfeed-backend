"""E2E integration tests for sync flow with new entity fields.

Test strategy:
    These tests simulate multi-device sync scenarios by exercising the full
    POST /api/v1/sync endpoint pipeline (validation -> apply changes -> get
    server state -> pagination).  Each test registers a fresh user, creates
    entities via sync, then performs a delta sync (with ``last_sync_at``) to
    verify that the new fields (notes, water_type, capacity, photo_key,
    aquarium_id move) propagate correctly between "devices".

    The tests follow the same patterns established in ``test_sync.py``:
    - ``register_and_login`` / ``auth_headers`` helpers for authentication
    - ``_setup_aquarium_with_schedule`` helper for entity scaffolding
    - ``ensure_species`` fixture for test-betta species
    - ``@pytest.mark.asyncio(loop_scope="session")`` for all async tests
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.species import Species

# ---------------------------------------------------------------------------
# Helpers (mirrors test_sync.py)
# ---------------------------------------------------------------------------


async def register_and_login(client: AsyncClient, email: str) -> dict:
    """Helper to register and login a user, returns tokens."""
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "SecurePass123"},
    )
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "SecurePass123"},
    )
    return response.json()


def auth_headers(tokens: dict) -> dict:
    """Helper to create auth headers."""
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _setup_aquarium_with_schedule(
    client: AsyncClient, tokens: dict
) -> tuple[str, str, str]:
    """Helper: create aquarium, add fish, create schedule via sync.

    Uses two sync calls because access validation runs BEFORE changes are
    applied -- so the aquarium must exist before fish/schedule can reference it.
    Uses ``test-betta`` species which is never deleted by other test modules.

    Returns (aquarium_id, fish_id, schedule_id).
    """
    now = datetime.now(UTC)
    aquarium_id = str(uuid.uuid4())
    fish_id = str(uuid.uuid4())
    schedule_id = str(uuid.uuid4())
    hdrs = auth_headers(tokens)

    # Step 1: Create aquarium (adds user as owner/member)
    resp1 = await client.post(
        "/api/v1/sync",
        json={
            "changes": [
                {
                    "entity_type": "aquarium",
                    "entity_id": aquarium_id,
                    "operation": "create",
                    "data": {"name": f"E2E Tank {uuid.uuid4().hex[:6]}"},
                    "client_updated_at": now.isoformat(),
                }
            ]
        },
        headers=hdrs,
    )
    assert resp1.status_code == 200, (
        f"Aquarium setup failed: {resp1.status_code} {resp1.text}"
    )

    # Step 2: Create fish + schedule (aquarium now exists in DB)
    resp2 = await client.post(
        "/api/v1/sync",
        json={
            "changes": [
                {
                    "entity_type": "fish",
                    "entity_id": fish_id,
                    "operation": "create",
                    "data": {
                        "aquarium_id": aquarium_id,
                        "species_id": "test-betta",
                        "quantity": 1,
                    },
                    "client_updated_at": now.isoformat(),
                },
                {
                    "entity_type": "schedule",
                    "entity_id": schedule_id,
                    "operation": "create",
                    "data": {
                        "aquarium_id": aquarium_id,
                        "fish_id": fish_id,
                        "time": "09:00",
                        "interval_days": 1,
                        "anchor_date": now.strftime("%Y-%m-%d"),
                        "food_type": "flakes",
                        "active": True,
                    },
                    "client_updated_at": now.isoformat(),
                },
            ]
        },
        headers=hdrs,
    )
    assert resp2.status_code == 200, (
        f"Fish/schedule setup failed: {resp2.status_code} {resp2.text}"
    )

    return aquarium_id, fish_id, schedule_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def ensure_species(async_session: AsyncSession) -> None:
    """Ensure test species exist -- other modules may delete them."""
    result = await async_session.execute(
        select(Species).where(Species.id == "test-betta")
    )
    if result.scalar_one_or_none() is None:
        async_session.add(
            Species(
                id="test-betta",
                common_name="Test Betta",
                scientific_name="Betta splendens",
                food_types=["pellets", "live"],
                feeding_frequency=2,
                care_level="beginner",
                water_type="freshwater",
            )
        )
        await async_session.commit()


# ---------------------------------------------------------------------------
# Test: Push fish notes -> receive via delta sync
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("ensure_species")
@pytest.mark.asyncio(loop_scope="session")
class TestSyncFishNotes:
    """Verify that fish ``notes`` field syncs correctly between devices."""

    async def test_push_notes_then_delta_sync_returns_notes(
        self, client: AsyncClient
    ):
        """Device A pushes notes; Device B receives them via delta sync."""
        email = f"sync-notes-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id, fish_id, _ = await _setup_aquarium_with_schedule(
            client, tokens
        )
        hdrs = auth_headers(tokens)

        # Record timestamp before the update
        t_before = datetime.now(UTC) - timedelta(seconds=1)

        # Device A: update fish with notes
        resp = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "fish",
                        "entity_id": fish_id,
                        "operation": "update",
                        "data": {"notes": "Test note from device A"},
                        "client_updated_at": (
                            datetime.now(UTC) + timedelta(seconds=5)
                        ).isoformat(),
                    }
                ]
            },
            headers=hdrs,
        )
        assert resp.status_code == 200
        assert resp.json()["conflicts"] == []

        # Device B: delta sync since t_before -> should see updated fish
        resp_delta = await client.post(
            "/api/v1/sync",
            json={
                "changes": [],
                "last_sync_at": t_before.isoformat(),
            },
            headers=hdrs,
        )
        assert resp_delta.status_code == 200
        fish_list = resp_delta.json()["server_state"]["fish"]
        fish_entity = next(
            (f for f in fish_list if f["id"] == fish_id), None
        )
        assert fish_entity is not None, "Fish should appear in delta sync"
        assert fish_entity["notes"] == "Test note from device A"

    async def test_push_null_notes_clears_notes(self, client: AsyncClient):
        """Setting notes to null clears the field."""
        email = f"sync-notes-null-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id, fish_id, _ = await _setup_aquarium_with_schedule(
            client, tokens
        )
        hdrs = auth_headers(tokens)
        now_ts = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()

        # First set notes
        await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "fish",
                        "entity_id": fish_id,
                        "operation": "update",
                        "data": {"notes": "Temporary note"},
                        "client_updated_at": now_ts,
                    }
                ]
            },
            headers=hdrs,
        )

        # Then clear notes
        clear_ts = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
        resp = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "fish",
                        "entity_id": fish_id,
                        "operation": "update",
                        "data": {"notes": None},
                        "client_updated_at": clear_ts,
                    }
                ]
            },
            headers=hdrs,
        )
        assert resp.status_code == 200
        fish_list = resp.json()["server_state"]["fish"]
        fish_entity = next(
            (f for f in fish_list if f["id"] == fish_id), None
        )
        assert fish_entity is not None
        assert fish_entity["notes"] is None


# ---------------------------------------------------------------------------
# Test: Fish move -> schedules updated -> consistent sync
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("ensure_species")
@pytest.mark.asyncio(loop_scope="session")
class TestSyncFishMove:
    """Verify that moving a fish to a different aquarium atomically updates
    the fish's ``aquarium_id`` and all associated ``FeedingSchedule.aquarium_id``.
    """

    async def test_fish_move_updates_schedule_aquarium_id(
        self, client: AsyncClient
    ):
        """Move fish from aquarium A to aquarium B.

        After the move:
        - fish.aquarium_id == aquarium_b_id
        - schedule.aquarium_id == aquarium_b_id (atomic update)
        """
        email = f"sync-move-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        hdrs = auth_headers(tokens)

        # Create aquarium A with fish and schedule
        aq_a_id, fish_id, schedule_id = await _setup_aquarium_with_schedule(
            client, tokens
        )

        # Create aquarium B
        aq_b_id = str(uuid.uuid4())
        resp_b = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aq_b_id,
                        "operation": "create",
                        "data": {"name": "Target Tank B"},
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=hdrs,
        )
        assert resp_b.status_code == 200

        # Record timestamp before the move
        t_before = datetime.now(UTC) - timedelta(seconds=1)

        # Move fish to aquarium B
        move_ts = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
        resp_move = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "fish",
                        "entity_id": fish_id,
                        "operation": "update",
                        "data": {"aquarium_id": aq_b_id},
                        "client_updated_at": move_ts,
                    }
                ]
            },
            headers=hdrs,
        )
        assert resp_move.status_code == 200
        assert resp_move.json()["conflicts"] == []

        # Full sync to verify final state
        resp_full = await client.post(
            "/api/v1/sync",
            json={"changes": []},
            headers=hdrs,
        )
        assert resp_full.status_code == 200
        data = resp_full.json()["server_state"]

        # Verify fish.aquarium_id changed
        fish_entity = next(
            (f for f in data["fish"] if f["id"] == fish_id), None
        )
        assert fish_entity is not None, "Fish should exist in server state"
        assert fish_entity["aquarium_id"] == aq_b_id

        # Verify schedule.aquarium_id also changed (atomic)
        schedule_entity = next(
            (s for s in data["schedules"] if s["id"] == schedule_id), None
        )
        assert schedule_entity is not None, "Schedule should exist in server state"
        assert schedule_entity["aquarium_id"] == aq_b_id

    async def test_fish_move_delta_sync_returns_both(
        self, client: AsyncClient
    ):
        """Delta sync after fish move returns both updated fish and schedule."""
        email = f"sync-move-delta-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        hdrs = auth_headers(tokens)

        # Create aquarium A with fish and schedule
        aq_a_id, fish_id, schedule_id = await _setup_aquarium_with_schedule(
            client, tokens
        )

        # Create aquarium B
        aq_b_id = str(uuid.uuid4())
        await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aq_b_id,
                        "operation": "create",
                        "data": {"name": "Delta Target Tank B"},
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=hdrs,
        )

        # Record time before move
        t_before = datetime.now(UTC) - timedelta(seconds=1)

        # Move fish
        move_ts = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
        await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "fish",
                        "entity_id": fish_id,
                        "operation": "update",
                        "data": {"aquarium_id": aq_b_id},
                        "client_updated_at": move_ts,
                    }
                ]
            },
            headers=hdrs,
        )

        # Delta sync
        resp_delta = await client.post(
            "/api/v1/sync",
            json={
                "changes": [],
                "last_sync_at": t_before.isoformat(),
            },
            headers=hdrs,
        )
        assert resp_delta.status_code == 200
        state = resp_delta.json()["server_state"]

        # Fish should appear with new aquarium_id
        fish_entity = next(
            (f for f in state["fish"] if f["id"] == fish_id), None
        )
        assert fish_entity is not None, "Moved fish should appear in delta"
        assert fish_entity["aquarium_id"] == aq_b_id

        # Schedule should appear with new aquarium_id
        schedule_entity = next(
            (s for s in state["schedules"] if s["id"] == schedule_id), None
        )
        assert schedule_entity is not None, "Schedule should appear in delta"
        assert schedule_entity["aquarium_id"] == aq_b_id


# ---------------------------------------------------------------------------
# Test: Aquarium water_type / capacity sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
class TestSyncAquariumWaterTypeCapacity:
    """Verify that aquarium ``water_type`` and ``capacity`` round-trip
    correctly through the sync endpoint.
    """

    async def test_create_aquarium_with_water_type_and_capacity(
        self, client: AsyncClient
    ):
        """Create an aquarium via sync with water_type and capacity, then
        read back via full sync.
        """
        email = f"sync-wt-cap-create-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        hdrs = auth_headers(tokens)
        aq_id = str(uuid.uuid4())

        resp = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aq_id,
                        "operation": "create",
                        "data": {
                            "name": "Saltwater Reef",
                            "water_type": "saltwater",
                            "capacity": 100.5,
                        },
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=hdrs,
        )
        assert resp.status_code == 200
        data = resp.json()["server_state"]
        aq = next((a for a in data["aquariums"] if a["id"] == aq_id), None)
        assert aq is not None
        assert aq["water_type"] == "saltwater"
        # capacity must come back as float (not string or Decimal)
        assert aq["capacity"] == 100.5
        assert isinstance(aq["capacity"], float)

    async def test_update_aquarium_water_type_and_capacity_via_sync(
        self, client: AsyncClient
    ):
        """Update water_type and capacity via sync, then verify via delta sync."""
        email = f"sync-wt-cap-update-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        hdrs = auth_headers(tokens)

        # Create aquarium (freshwater, no capacity)
        aq_id = str(uuid.uuid4())
        await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aq_id,
                        "operation": "create",
                        "data": {"name": "Plain Tank"},
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=hdrs,
        )

        # Record time before update
        t_before = datetime.now(UTC) - timedelta(seconds=1)

        # Update water_type and capacity
        update_ts = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
        resp = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aq_id,
                        "operation": "update",
                        "data": {
                            "water_type": "brackish",
                            "capacity": 75.25,
                        },
                        "client_updated_at": update_ts,
                    }
                ]
            },
            headers=hdrs,
        )
        assert resp.status_code == 200
        assert resp.json()["conflicts"] == []

        # Delta sync
        resp_delta = await client.post(
            "/api/v1/sync",
            json={
                "changes": [],
                "last_sync_at": t_before.isoformat(),
            },
            headers=hdrs,
        )
        assert resp_delta.status_code == 200
        aq_list = resp_delta.json()["server_state"]["aquariums"]
        aq = next((a for a in aq_list if a["id"] == aq_id), None)
        assert aq is not None, "Aquarium should appear in delta sync"
        assert aq["water_type"] == "brackish"
        assert aq["capacity"] == 75.25
        assert isinstance(aq["capacity"], float)

    async def test_invalid_water_type_falls_back_to_freshwater(
        self, client: AsyncClient
    ):
        """Invalid water_type falls back to 'freshwater' (not rejected)."""
        email = f"sync-wt-invalid-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        hdrs = auth_headers(tokens)
        aq_id = str(uuid.uuid4())

        resp = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aq_id,
                        "operation": "create",
                        "data": {
                            "name": "Invalid WT Tank",
                            "water_type": "lava",
                        },
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=hdrs,
        )
        assert resp.status_code == 200
        aq = next(
            (
                a
                for a in resp.json()["server_state"]["aquariums"]
                if a["id"] == aq_id
            ),
            None,
        )
        assert aq is not None
        assert aq["water_type"] == "freshwater"

    async def test_null_capacity_is_preserved(self, client: AsyncClient):
        """Capacity can be set to null (cleared)."""
        email = f"sync-cap-null-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        hdrs = auth_headers(tokens)
        aq_id = str(uuid.uuid4())

        # Create with capacity
        await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aq_id,
                        "operation": "create",
                        "data": {
                            "name": "Null Cap Tank",
                            "capacity": 50.0,
                        },
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=hdrs,
        )

        # Clear capacity
        clear_ts = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
        resp = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aq_id,
                        "operation": "update",
                        "data": {"capacity": None},
                        "client_updated_at": clear_ts,
                    }
                ]
            },
            headers=hdrs,
        )
        assert resp.status_code == 200
        aq = next(
            (
                a
                for a in resp.json()["server_state"]["aquariums"]
                if a["id"] == aq_id
            ),
            None,
        )
        assert aq is not None
        assert aq["capacity"] is None


# ---------------------------------------------------------------------------
# Test: Photo deletion sync
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("ensure_species")
@pytest.mark.asyncio(loop_scope="session")
class TestSyncPhotoKeyDeletion:
    """Verify that setting ``photo_key`` to null via sync clears the photo."""

    async def test_fish_photo_key_set_to_null(self, client: AsyncClient):
        """Create fish with photo_key, then set to null and verify."""
        email = f"sync-photo-null-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        hdrs = auth_headers(tokens)

        # Create aquarium
        aq_id = str(uuid.uuid4())
        await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aq_id,
                        "operation": "create",
                        "data": {"name": "Photo Test Tank"},
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=hdrs,
        )

        # Create fish with photo_key
        fish_id = str(uuid.uuid4())
        photo_key = f"fish/{fish_id}/{uuid.uuid4().hex[:8]}.webp"
        create_ts = datetime.now(UTC).isoformat()
        resp_create = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "fish",
                        "entity_id": fish_id,
                        "operation": "create",
                        "data": {
                            "aquarium_id": aq_id,
                            "species_id": "test-betta",
                            "quantity": 1,
                            "photo_key": photo_key,
                        },
                        "client_updated_at": create_ts,
                    }
                ]
            },
            headers=hdrs,
        )
        assert resp_create.status_code == 200
        fish_entity = next(
            (
                f
                for f in resp_create.json()["server_state"]["fish"]
                if f["id"] == fish_id
            ),
            None,
        )
        assert fish_entity is not None
        assert fish_entity["photo_key"] == photo_key

        # Record time before deletion
        t_before = datetime.now(UTC) - timedelta(seconds=1)

        # Delete photo: set photo_key to null
        delete_ts = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
        resp_delete = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "fish",
                        "entity_id": fish_id,
                        "operation": "update",
                        "data": {"photo_key": None},
                        "client_updated_at": delete_ts,
                    }
                ]
            },
            headers=hdrs,
        )
        assert resp_delete.status_code == 200
        assert resp_delete.json()["conflicts"] == []

        # Delta sync to verify
        resp_delta = await client.post(
            "/api/v1/sync",
            json={
                "changes": [],
                "last_sync_at": t_before.isoformat(),
            },
            headers=hdrs,
        )
        assert resp_delta.status_code == 200
        fish_list = resp_delta.json()["server_state"]["fish"]
        fish_entity = next(
            (f for f in fish_list if f["id"] == fish_id), None
        )
        assert fish_entity is not None, "Fish should appear in delta sync"
        assert fish_entity["photo_key"] is None

    async def test_aquarium_photo_key_set_to_null(self, client: AsyncClient):
        """Create aquarium with photo_key, then set to null."""
        email = f"sync-aq-photo-null-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        hdrs = auth_headers(tokens)

        aq_id = str(uuid.uuid4())
        photo_key = f"aquariums/{aq_id}/{uuid.uuid4().hex[:8]}.webp"

        # Create aquarium with photo_key
        await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aq_id,
                        "operation": "create",
                        "data": {
                            "name": "Photo Aquarium",
                            "photo_key": photo_key,
                        },
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=hdrs,
        )

        # Record time before photo deletion
        t_before = datetime.now(UTC) - timedelta(seconds=1)

        # Clear photo
        clear_ts = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
        resp = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aq_id,
                        "operation": "update",
                        "data": {"photo_key": None},
                        "client_updated_at": clear_ts,
                    }
                ]
            },
            headers=hdrs,
        )
        assert resp.status_code == 200

        # Delta sync
        resp_delta = await client.post(
            "/api/v1/sync",
            json={
                "changes": [],
                "last_sync_at": t_before.isoformat(),
            },
            headers=hdrs,
        )
        assert resp_delta.status_code == 200
        aq_list = resp_delta.json()["server_state"]["aquariums"]
        aq = next((a for a in aq_list if a["id"] == aq_id), None)
        assert aq is not None, "Aquarium should appear in delta sync"
        assert aq["photo_key"] is None
