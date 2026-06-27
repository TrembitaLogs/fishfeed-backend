"""Tests for DB-level /sync pagination (get_paginated_server_state).

These lock the cursor math (completeness / no-overlap across pages that span
multiple entity types) and prove the perf fix: feeding_logs are fetched with a
DB-level LIMIT instead of being fully materialized and sliced in Python.
"""

import math
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.sync import SyncRequest, SyncResponse
from app.services.sync import get_paginated_server_state, get_server_state, process_sync
from tests.services.test_sync import (
    cleanup_sync_test_data,
    create_test_aquarium,
    create_test_feeding_log,
    create_test_fish,
    create_test_schedule,
    create_test_user,
)


async def _collect_pages(
    session: AsyncSession,
    user_id: uuid.UUID,
    since: datetime | None,
    page_size: int,
) -> list[SyncResponse]:
    """Drive process_sync page-by-page until has_more is False."""
    pages: list[SyncResponse] = []
    cursor: str | None = None
    for _ in range(10_000):
        request = SyncRequest(changes=[], last_sync_at=since, cursor=cursor, page_size=page_size)
        response = await process_sync(session, user_id, request)
        pages.append(response)
        if not response.has_more:
            break
        cursor = response.next_cursor
    else:  # pragma: no cover - guard against an infinite pagination loop
        raise AssertionError("pagination did not terminate")
    return pages


def _page_item_count(response: SyncResponse) -> int:
    state = response.server_state
    return len(state.aquariums) + len(state.fish) + len(state.feeding_logs) + len(state.schedules)


# ============================================================================
# A) Completeness / no-overlap across pages spanning multiple entity types
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_pagination_complete_and_no_overlap_across_types(
    async_session: AsyncSession,
):
    """Paging across [aquarium, fish, schedule, feeding_logs] is complete and non-overlapping."""
    await cleanup_sync_test_data(async_session)
    try:
        n_logs = 6
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)

        base = datetime(2024, 6, 15, 9, 0)
        log_ids: set[str] = set()
        for i in range(n_logs):
            log = await create_test_feeding_log(
                async_session,
                schedule.id,
                fish.id,
                aquarium.id,
                user.id,
                scheduled_for=base + timedelta(minutes=i),
            )
            log_ids.add(str(log.id))

        # Active item universe: 1 aquarium + 1 fish + 1 schedule + n_logs feeding_logs
        total_items = 1 + 1 + 1 + n_logs
        page_size = 2

        pages = await _collect_pages(async_session, user.id, since=None, page_size=page_size)

        # Page count is exactly ceil(total / page_size).
        assert len(pages) == math.ceil(total_items / page_size)

        # Every page (except possibly the last) is full; none exceeds page_size.
        for page in pages:
            assert _page_item_count(page) <= page_size
        for page in pages[:-1]:
            assert page.has_more is True
            assert page.next_cursor is not None
        assert pages[-1].has_more is False
        assert pages[-1].next_cursor is None

        # Union of ids equals what was created, with NO duplicates across pages.
        seen_aquariums: list[str] = []
        seen_fish: list[str] = []
        seen_schedules: list[str] = []
        seen_logs: list[str] = []
        for page in pages:
            seen_aquariums += [a["id"] for a in page.server_state.aquariums]
            seen_fish += [f["id"] for f in page.server_state.fish]
            seen_schedules += [s["id"] for s in page.server_state.schedules]
            seen_logs += [log["id"] for log in page.server_state.feeding_logs]

        # No duplicates across pages.
        assert len(seen_aquariums) == len(set(seen_aquariums))
        assert len(seen_fish) == len(set(seen_fish))
        assert len(seen_schedules) == len(set(seen_schedules))
        assert len(seen_logs) == len(set(seen_logs))

        # Completeness.
        assert set(seen_aquariums) == {str(aquarium.id)}
        assert set(seen_fish) == {str(fish.id)}
        assert set(seen_schedules) == {str(schedule.id)}
        assert set(seen_logs) == log_ids

        # Total items across all pages equals the universe (no loss, no dupes).
        assert (
            len(seen_aquariums) + len(seen_fish) + len(seen_schedules) + len(seen_logs)
            == total_items
        )
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_pagination_parity_with_get_server_state(
    async_session: AsyncSession,
):
    """All paginated ids together equal the full get_server_state id sets (no data loss)."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)

        base = datetime(2024, 7, 1, 9, 0)
        for i in range(5):
            await create_test_feeding_log(
                async_session,
                schedule.id,
                fish.id,
                aquarium.id,
                user.id,
                scheduled_for=base + timedelta(minutes=i),
            )

        full = await get_server_state(async_session, user.id, since=None)

        pages = await _collect_pages(async_session, user.id, since=None, page_size=2)
        paged_aq = {a["id"] for page in pages for a in page.server_state.aquariums}
        paged_fish = {f["id"] for page in pages for f in page.server_state.fish}
        paged_sched = {s["id"] for page in pages for s in page.server_state.schedules}
        paged_logs = {log["id"] for page in pages for log in page.server_state.feeding_logs}

        assert paged_aq == {a["id"] for a in full.aquariums}
        assert paged_fish == {f["id"] for f in full.fish}
        assert paged_sched == {s["id"] for s in full.schedules}
        assert paged_logs == {log["id"] for log in full.feeding_logs}
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# B) Bounded-fetch — feeding_logs are fetched with a DB LIMIT, not materialized
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_logs_fetched_with_db_limit(
    async_session: AsyncSession,
):
    """The feeding_logs SELECT must carry a LIMIT (proof of the perf fix).

    Against the old get_server_state + _apply_pagination path the feeding_logs
    data SELECT has NO LIMIT (full materialization), so this test fails there.
    Against get_paginated_server_state every feeding_logs data SELECT is windowed
    with OFFSET/LIMIT, so it passes.
    """
    await cleanup_sync_test_data(async_session)
    try:
        n_logs = 6
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)

        base = datetime(2024, 8, 1, 9, 0)
        for i in range(n_logs):
            await create_test_feeding_log(
                async_session,
                schedule.id,
                fish.id,
                aquarium.id,
                user.id,
                scheduled_for=base + timedelta(minutes=i),
            )

        captured: list[str] = []

        def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            captured.append(statement)

        # Attach AFTER setup so only the sync read statements are captured.
        # Drive every page: the feeding_logs window is only queried on pages
        # whose global offset overlaps the logs range, so we must page through.
        sync_engine = async_session.bind.sync_engine  # type: ignore[union-attr]
        event.listen(sync_engine, "after_cursor_execute", _capture)
        try:
            await _collect_pages(async_session, user.id, since=None, page_size=2)
        finally:
            event.remove(sync_engine, "after_cursor_execute", _capture)

        # Data SELECTs against feeding_logs (exclude the COUNT(*) query, which
        # does not select the feeding_logs columns).
        feeding_data_selects = [
            s for s in captured if "feeding_logs.id" in s and "FROM feeding_logs" in s
        ]
        assert feeding_data_selects, "expected at least one feeding_logs data SELECT"
        # Every feeding_logs data SELECT must be bounded by a LIMIT.
        assert all("LIMIT" in s for s in feeding_data_selects), (
            "feeding_logs data SELECT was not bounded by a DB LIMIT: "
            f"{feeding_data_selects}"
        )
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# C) Delta pagination sanity — only logs created after `since`, paginated
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_delta_pagination_excludes_old_logs(
    async_session: AsyncSession,
):
    """Delta sync paginates only feeding_logs created after `since`."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)

        base = datetime(2024, 9, 1, 9, 0)

        # Two "old" logs created before the delta cutoff.
        old_ids: set[str] = set()
        last_old = None
        for i in range(2):
            last_old = await create_test_feeding_log(
                async_session,
                schedule.id,
                fish.id,
                aquarium.id,
                user.id,
                scheduled_for=base + timedelta(minutes=i),
            )
            old_ids.add(str(last_old.id))

        assert last_old is not None
        # Cutoff strictly after the last old log's created_at; the new logs are
        # inserted in subsequent (real-time-later) transactions, so created_at > since.
        since = last_old.created_at + timedelta(microseconds=1)

        # Four "new" logs created after the cutoff.
        new_ids: set[str] = set()
        for i in range(4):
            log = await create_test_feeding_log(
                async_session,
                schedule.id,
                fish.id,
                aquarium.id,
                user.id,
                scheduled_for=base + timedelta(hours=1, minutes=i),
            )
            new_ids.add(str(log.id))

        # Aquarium/fish/schedule were created before `since`, so the only
        # paginated active items are the 4 new feeding_logs.
        pages = await _collect_pages(async_session, user.id, since=since, page_size=2)

        assert len(pages) == 2  # ceil(4 / 2)
        seen_logs: list[str] = []
        for page in pages:
            assert len(page.server_state.feeding_logs) <= 2
            # No active aquarium/fish/schedule re-sent on a delta with no edits.
            assert page.server_state.aquariums == []
            assert page.server_state.fish == []
            assert page.server_state.schedules == []
            seen_logs += [log["id"] for log in page.server_state.feeding_logs]

        assert len(seen_logs) == len(set(seen_logs))  # no overlap
        assert set(seen_logs) == new_ids
        assert old_ids.isdisjoint(set(seen_logs))
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# Empty-aquariums early return parity
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_paginated_state_no_aquariums_returns_empty(
    async_session: AsyncSession,
):
    """User with no aquariums gets an empty single page (has_more False)."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)

        state, has_more, next_cursor = await get_paginated_server_state(
            async_session, user.id, since=None, page_size=100, cursor=None
        )

        assert state.aquariums == []
        assert state.fish == []
        assert state.feeding_logs == []
        assert state.schedules == []
        assert state.deleted.aquariums == []
        assert state.deleted.fish == []
        assert has_more is False
        assert next_cursor is None
    finally:
        await cleanup_sync_test_data(async_session)
