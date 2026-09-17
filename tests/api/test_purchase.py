"""Webhook route regressions for authenticated reconciliation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.purchase import WebhookTransaction
from app.models.user import User
from app.services.purchase import PurchaseError, Reconciliation, RevenueCatAPIError, SubscriptionSnapshot


async def clear_webhooks(session: AsyncSession) -> None:
    await session.execute(text("TRUNCATE TABLE webhook_transactions, users CASCADE"))
    await session.commit()


async def webhook_user(session: AsyncSession) -> User:
    user = User(email=f"{uuid4()}@example.com", password_hash="unused")
    session.add(user)
    await session.commit()
    return user


def payload(user_id: str | None, event_id: str = "event-1") -> dict:
    event: dict[str, object] = {
        "id": event_id,
        "type": "INITIAL_PURCHASE",
        "environment": "PRODUCTION",
        "entitlement_ids": ["premium"],
    }
    if user_id is not None:
        event["app_user_id"] = user_id
    return {"event": event}


def settings(*, environment: str = "development", secret: str | None = "secret") -> SimpleNamespace:
    return SimpleNamespace(ENVIRONMENT=environment, REVENUECAT_WEBHOOK_SECRET=secret)


@pytest.mark.asyncio(loop_scope="session")
async def test_production_without_webhook_secret_returns_503_before_parsing(
    client: AsyncClient, async_session: AsyncSession
):
    await clear_webhooks(async_session)
    try:
        with patch("app.api.purchase.get_settings", return_value=settings(environment="production", secret=None)):
            response = await client.post("/api/v1/purchases/webhook", content=b"not json")

        assert response.status_code == 503
        assert await async_session.scalar(select(WebhookTransaction)) is None
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_wrong_authorization_returns_401_before_parsing(client: AsyncClient, async_session: AsyncSession):
    await clear_webhooks(async_session)
    try:
        with patch("app.api.purchase.get_settings", return_value=settings()):
            response = await client.post(
                "/api/v1/purchases/webhook", content=b"not json", headers={"Authorization": "wrong"}
            )

        assert response.status_code == 401
        assert await async_session.scalar(select(WebhookTransaction)) is None
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_transfer_without_app_user_id_is_audited_as_skipped(client: AsyncClient, async_session: AsyncSession):
    await clear_webhooks(async_session)
    try:
        transfer = {
            "event": {
                "id": "transfer-route",
                "type": "TRANSFER",
                "transferred_from": ["$RCAnonymousID:old"],
                "transferred_to": [str(uuid4())],
                "environment": "SANDBOX",
            }
        }
        with (
            patch("app.api.purchase.get_settings", return_value=settings()),
            patch("app.api.purchase.process_webhook", new=AsyncMock(return_value=("skipped", []))),
        ):
            response = await client.post(
                "/api/v1/purchases/webhook", json=transfer, headers={"Authorization": "secret"}
            )

        assert response.status_code == 200
        audit = await async_session.scalar(
            select(WebhookTransaction).where(WebhookTransaction.transaction_id == "transfer-route")
        )
        assert audit is not None and audit.processing_result == "skipped"
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_failed_provider_event_retries_and_updates_its_audit(client: AsyncClient, async_session: AsyncSession):
    await clear_webhooks(async_session)
    try:
        user = await webhook_user(async_session)
        event = payload(str(user.id), "retry-event")
        process = AsyncMock(side_effect=[RevenueCatAPIError("upstream timeout"), ("success", [])])
        with (
            patch("app.api.purchase.get_settings", return_value=settings()),
            patch("app.api.purchase.process_webhook", new=process),
        ):
            first = await client.post("/api/v1/purchases/webhook", json=event, headers={"Authorization": "secret"})
            second = await client.post("/api/v1/purchases/webhook", json=event, headers={"Authorization": "secret"})

        assert first.status_code == 502
        assert second.status_code == 200
        audit = await async_session.scalar(
            select(WebhookTransaction).where(WebhookTransaction.transaction_id == "retry-event")
        )
        assert audit is not None and audit.processing_result == "success"
        assert process.await_count == 2
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_committed_success_is_deduplicated_without_reprocessing(client: AsyncClient, async_session: AsyncSession):
    await clear_webhooks(async_session)
    try:
        user = await webhook_user(async_session)
        event = payload(str(user.id), "duplicate-event")
        process = AsyncMock(return_value=("success", []))
        with (
            patch("app.api.purchase.get_settings", return_value=settings()),
            patch("app.api.purchase.process_webhook", new=process),
        ):
            await client.post("/api/v1/purchases/webhook", json=event, headers={"Authorization": "secret"})
            response = await client.post("/api/v1/purchases/webhook", json=event, headers={"Authorization": "secret"})

        assert response.status_code == 200
        assert response.json()["message"] == "Already processed"
        assert process.await_count == 1
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_busy_lock_returns_503_without_error_audit(client: AsyncClient, async_session: AsyncSession):
    await clear_webhooks(async_session)
    try:
        user = await webhook_user(async_session)
        with (
            patch("app.api.purchase.get_settings", return_value=settings()),
            patch(
                "app.api.purchase.check_idempotency",
                new=AsyncMock(side_effect=PurchaseError("busy", status_code=503)),
            ),
        ):
            response = await client.post(
                "/api/v1/purchases/webhook",
                json=payload(str(user.id), "busy-event"),
                headers={"Authorization": "secret"},
            )

        assert response.status_code == 503
        assert await async_session.scalar(select(WebhookTransaction)) is None
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_cache_invalidation_runs_after_committed_webhook_audit(client: AsyncClient, async_session: AsyncSession):
    await clear_webhooks(async_session)
    try:
        user = await webhook_user(async_session)
        result = Reconciliation(
            user_id=user.id,
            before_status="free",
            before_expires_at=None,
            before_verified_at=None,
            before_subscription={},
            snapshot=SubscriptionSnapshot("premium", None, "premium.lifetime", False, False),
            verified_at=datetime.now(UTC),
            outcome="changed",
        )

        async def invalidate(_, __):
            audit = await async_session.scalar(
                select(WebhookTransaction).where(WebhookTransaction.transaction_id == "cache-after-commit")
            )
            assert audit is not None and audit.processing_result == "success"

        with (
            patch("app.api.purchase.get_settings", return_value=settings()),
            patch("app.api.purchase.process_webhook", new=AsyncMock(return_value=("success", [result]))),
            patch("app.api.purchase.invalidate_premium_cache", side_effect=invalidate) as cache,
        ):
            response = await client.post(
                "/api/v1/purchases/webhook",
                json=payload(str(user.id), "cache-after-commit"),
                headers={"Authorization": "secret"},
            )

        assert response.status_code == 200
        cache.assert_awaited_once()
    finally:
        await clear_webhooks(async_session)
