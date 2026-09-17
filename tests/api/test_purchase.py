"""Purchase endpoint regressions and authenticated webhook reconciliation."""

from asyncio import CancelledError
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient
from redis.exceptions import RedisError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.purchase import WebhookTransaction
from app.models.user import User
from app.schemas.purchase import SubscriptionStatus
from app.services.purchase import (
    PurchaseError,
    Reconciliation,
    RevenueCatAPIError,
    SubscriptionSnapshot,
    WebhookAuditConflict,
    WebhookRetryableError,
    apply_reconciliation,
)


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
async def test_missing_authorization_returns_401_before_parsing(client: AsyncClient, async_session: AsyncSession):
    await clear_webhooks(async_session)
    try:
        with patch("app.api.purchase.get_settings", return_value=settings()):
            response = await client.post("/api/v1/purchases/webhook", content=b"not json")

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


@pytest.mark.asyncio(loop_scope="session")
async def test_post_commit_cache_and_release_failures_do_not_replace_success(
    client: AsyncClient, async_session: AsyncSession, redis_client
):
    await clear_webhooks(async_session)
    try:
        record = await webhook_user(async_session)
        result = Reconciliation(
            user_id=record.id,
            before_status="free",
            before_expires_at=None,
            before_verified_at=None,
            before_subscription={},
            snapshot=SubscriptionSnapshot("premium", None, "premium.lifetime", False, False),
            verified_at=datetime.now(UTC),
            outcome="changed",
        )
        original_delete = redis_client.delete
        original_command = redis_client.execute_command

        async def delete_or_fail(key: str, *keys: str):
            if key.startswith("premium_status:"):
                raise RedisError("cache unavailable")
            return await original_delete(key, *keys)

        async def command_or_fail(command: str, *arguments: object, **kwargs: object):
            if command == "EVAL":
                raise RedisError("release unavailable")
            return await original_command(command, *arguments, **kwargs)

        with (
            patch("app.api.purchase.get_settings", return_value=settings()),
            patch("app.api.purchase.process_webhook", new=AsyncMock(return_value=("success", [result]))),
            patch.object(redis_client, "delete", side_effect=delete_or_fail),
            patch.object(redis_client, "execute_command", side_effect=command_or_fail),
        ):
            response = await client.post(
                "/api/v1/purchases/webhook",
                json=payload(str(record.id), "post-commit-cleanup-failure"),
                headers={"Authorization": "secret"},
            )

        assert response.status_code == 200
        audit = await async_session.scalar(
            select(WebhookTransaction).where(WebhookTransaction.transaction_id == "post-commit-cleanup-failure")
        )
        assert audit is not None and audit.processing_result == "success"
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        (RevenueCatAPIError("provider unavailable"), 502),
        (RevenueCatAPIError("redis unavailable", failure_kind="storage"), 503),
        (WebhookRetryableError("participant disappeared"), 503),
    ],
)
async def test_webhook_failure_status_uses_structured_cause(
    client: AsyncClient,
    async_session: AsyncSession,
    failure: PurchaseError,
    expected_status: int,
):
    await clear_webhooks(async_session)
    try:
        record = await webhook_user(async_session)
        with (
            patch("app.api.purchase.get_settings", return_value=settings()),
            patch("app.api.purchase.process_webhook", new=AsyncMock(side_effect=failure)),
        ):
            response = await client.post(
                "/api/v1/purchases/webhook",
                json=payload(str(record.id), f"cause-{expected_status}-{type(failure).__name__}"),
                headers={"Authorization": "secret"},
            )
        assert response.status_code == expected_status
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(("winner_result", "expected_status"), [("success", 200), ("error", 503)])
async def test_error_audit_unique_conflict_rereads_the_committed_winner(
    client: AsyncClient,
    async_session: AsyncSession,
    winner_result: str,
    expected_status: int,
):
    await clear_webhooks(async_session)
    event_id = f"error-audit-winner-{winner_result}"
    try:
        record = await webhook_user(async_session)
        winner = WebhookTransaction(
            transaction_id=event_id,
            event_type="INITIAL_PURCHASE",
            user_id=str(record.id),
            payload={"event": {"id": event_id}},
            processing_result=winner_result,
        )
        async_session.add(winner)
        await async_session.commit()
        with (
            patch("app.api.purchase.get_settings", return_value=settings()),
            patch("app.api.purchase.check_idempotency", new=AsyncMock(return_value=(False, ("unused", "token")))),
            patch("app.api.purchase.process_webhook", new=AsyncMock(side_effect=RevenueCatAPIError("provider failed"))),
            patch("app.api.purchase.log_webhook_transaction", new=AsyncMock(side_effect=WebhookAuditConflict)),
        ):
            response = await client.post(
                "/api/v1/purchases/webhook",
                json=payload(str(record.id), event_id),
                headers={"Authorization": "secret"},
            )
        assert response.status_code == expected_status
        unchanged = await async_session.scalar(
            select(WebhookTransaction).where(WebhookTransaction.transaction_id == event_id)
        )
        assert unchanged is not None and unchanged.processing_result == winner_result
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_error_audit_normal_terminal_winner_is_acknowledged(client: AsyncClient, async_session: AsyncSession):
    await clear_webhooks(async_session)
    event_id = "error-audit-normal-winner"
    try:
        record = await webhook_user(async_session)
        async_session.add(
            WebhookTransaction(
                transaction_id=event_id,
                event_type="INITIAL_PURCHASE",
                user_id=str(record.id),
                payload={"event": {"id": event_id}},
                processing_result="success",
            )
        )
        await async_session.commit()
        with (
            patch("app.api.purchase.get_settings", return_value=settings()),
            patch("app.api.purchase.check_idempotency", new=AsyncMock(return_value=(False, ("unused", "token")))),
            patch("app.api.purchase.process_webhook", new=AsyncMock(side_effect=RevenueCatAPIError("provider failed"))),
        ):
            response = await client.post(
                "/api/v1/purchases/webhook",
                json=payload(str(record.id), event_id),
                headers={"Authorization": "secret"},
            )

        assert response.status_code == 200
        assert response.json()["message"] == "Already processed"
        winner = await async_session.scalar(
            select(WebhookTransaction).where(WebhookTransaction.transaction_id == event_id)
        )
        assert winner is not None and winner.processing_result == "success"
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_unsupported_app_environment_returns_503_without_remove_ads_write(
    client: AsyncClient, async_session: AsyncSession
):
    await clear_webhooks(async_session)
    try:
        record = await webhook_user(async_session)
        remove_ads = {
            "event": {
                "id": "unsupported-environment",
                "type": "NON_RENEWING_PURCHASE",
                "app_user_id": str(record.id),
                "environment": "PRODUCTION",
                "entitlement_ids": ["remove_ads"],
            }
        }
        unsupported = settings(environment="staging")
        with (
            patch("app.api.purchase.get_settings", return_value=unsupported),
            patch("app.services.purchase.get_settings", return_value=unsupported),
        ):
            response = await client.post(
                "/api/v1/purchases/webhook", json=remove_ads, headers={"Authorization": "secret"}
            )

        assert response.status_code == 503
        await async_session.refresh(record)
        assert record.settings == {}
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_transfer_cancellation_rolls_back_partial_projection_and_audit(
    client: AsyncClient, async_session: AsyncSession, async_engine
):
    await clear_webhooks(async_session)
    try:
        first, second = await webhook_user(async_session), await webhook_user(async_session)
        proposals = [
            Reconciliation(
                user_id=user_id,
                before_status="free",
                before_expires_at=None,
                before_verified_at=None,
                before_subscription={},
                snapshot=SubscriptionSnapshot("premium", None, "premium.transfer", False, False),
                verified_at=datetime.now(UTC),
                outcome="changed",
            )
            for user_id in sorted([first.id, second.id], key=str)
        ]
        applies = 0

        async def apply_then_cancel(db: AsyncSession, candidate: Reconciliation) -> Reconciliation:
            nonlocal applies
            applies += 1
            if applies == 2:
                raise CancelledError
            return await apply_reconciliation(db, candidate)

        transfer = {
            "event": {
                "id": "cancelled-transfer",
                "type": "TRANSFER",
                "transferred_from": [str(first.id)],
                "transferred_to": [str(second.id)],
                "environment": "PRODUCTION",
            }
        }
        with (
            patch("app.api.purchase.get_settings", return_value=settings()),
            patch("app.services.purchase.get_settings", return_value=settings()),
            patch("app.services.purchase.read_reconciliation", new=AsyncMock(side_effect=proposals)),
            patch("app.services.purchase.apply_reconciliation", side_effect=apply_then_cancel),
            pytest.raises(CancelledError),
        ):
            await client.post(
                "/api/v1/purchases/webhook",
                json=transfer,
                headers={"Authorization": "secret"},
            )
        sessions = async_sessionmaker(async_engine, expire_on_commit=False)
        async with sessions() as verify:
            statuses = list(
                (await verify.scalars(select(User.subscription_status).where(User.id.in_([first.id, second.id])))).all()
            )
            audit = await verify.scalar(
                select(WebhookTransaction).where(WebhookTransaction.transaction_id == "cancelled-transfer")
            )
        assert statuses == ["free", "free"]
        assert audit is None
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_invalid_payload_commits_error_audit_and_returns_200(client: AsyncClient, async_session: AsyncSession):
    await clear_webhooks(async_session)
    try:
        with patch("app.api.purchase.get_settings", return_value=settings(secret=None)):
            response = await client.post("/api/v1/purchases/webhook", content=b'{"invalid": nope}')

        assert response.status_code == 200
        assert response.json()["success"] is False
        audit = await async_session.scalar(
            select(WebhookTransaction).where(WebhookTransaction.event_type == "PARSE_ERROR")
        )
        assert audit is not None and audit.processing_result == "error"
        assert audit.error_message is not None and "Failed to parse" in audit.error_message
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_invalid_payload_returns_503_when_mandatory_audit_fails(client: AsyncClient, async_session: AsyncSession):
    from sqlalchemy.exc import SQLAlchemyError

    await clear_webhooks(async_session)
    try:
        with (
            patch("app.api.purchase.get_settings", return_value=settings(secret=None)),
            patch(
                "app.api.purchase.log_webhook_transaction", new=AsyncMock(side_effect=SQLAlchemyError("audit failed"))
            ),
        ):
            response = await client.post("/api/v1/purchases/webhook", content=b'{"invalid": nope}')

        assert response.status_code == 503
        assert await async_session.scalar(select(WebhookTransaction)) is None
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_subscription_endpoint_requires_authentication(client: AsyncClient):
    response = await client.get("/api/v1/purchases/subscription")
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_subscription_endpoint_returns_current_status(client: AsyncClient, async_session: AsyncSession):
    await clear_webhooks(async_session)
    try:
        record = await webhook_user(async_session)
        record.subscription_status = "premium"
        record.subscription_expires_at = datetime.now(UTC)
        await async_session.commit()
        from app.utils.jwt import create_access_token

        response = await client.get(
            "/api/v1/purchases/subscription",
            headers={"Authorization": f"Bearer {create_access_token(str(record.id))}"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "premium"
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_restore_endpoint_delegates_for_current_user(client: AsyncClient, async_session: AsyncSession):
    await clear_webhooks(async_session)
    try:
        record = await webhook_user(async_session)
        from app.utils.jwt import create_access_token

        restored = SubscriptionStatus(status="premium", product_id="premium.monthly", will_renew=True)
        with patch("app.api.purchase.restore_purchases", new=AsyncMock(return_value=restored)) as restore:
            response = await client.post(
                "/api/v1/purchases/restore",
                json={"user_id": str(record.id), "receipt": "receipt", "platform": "ios"},
                headers={"Authorization": f"Bearer {create_access_token(str(record.id))}"},
            )
        assert response.status_code == 200
        assert response.json()["product_id"] == "premium.monthly"
        restore.assert_awaited_once()
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_promotional_webhooks_grant_then_revoke_only_from_provider_snapshots(
    client: AsyncClient, async_session: AsyncSession
):
    await clear_webhooks(async_session)
    try:
        record = await webhook_user(async_session)
        granted_at = datetime.now(UTC)
        grant = Reconciliation(
            user_id=record.id,
            before_status="free",
            before_expires_at=None,
            before_verified_at=None,
            before_subscription={},
            snapshot=SubscriptionSnapshot("premium", None, "premium.promo", False, False),
            verified_at=granted_at,
            outcome="changed",
        )
        revoke = Reconciliation(
            user_id=record.id,
            before_status="premium",
            before_expires_at=None,
            before_verified_at=granted_at,
            before_subscription={"product_id": "premium.promo", "will_renew": False, "is_trial": False},
            snapshot=SubscriptionSnapshot("free", None, None, False, False),
            verified_at=datetime.now(UTC),
            outcome="changed",
        )
        promo = {
            "event": {
                "type": "NON_RENEWING_PURCHASE",
                "app_user_id": str(record.id),
                "store": "PROMOTIONAL",
                "environment": "PRODUCTION",
                "entitlement_ids": ["premium"],
            }
        }
        with (
            patch("app.api.purchase.get_settings", return_value=settings()),
            patch("app.services.purchase.get_settings", return_value=settings()),
            patch("app.services.purchase.read_reconciliation", new=AsyncMock(side_effect=[grant, revoke])),
        ):
            granted = await client.post(
                "/api/v1/purchases/webhook",
                json={"event": {"id": "promo-grant", **promo["event"]}},
                headers={"Authorization": "secret"},
            )
            revoked = await client.post(
                "/api/v1/purchases/webhook",
                json={"event": {"id": "promo-revoke", **promo["event"]}},
                headers={"Authorization": "secret"},
            )

        assert granted.status_code == revoked.status_code == 200
        await async_session.refresh(record)
        assert record.subscription_status == "free"
    finally:
        await clear_webhooks(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_second_transfer_apply_failure_rolls_back_route_projection(
    client: AsyncClient, async_session: AsyncSession
):
    await clear_webhooks(async_session)
    try:
        first, second = await webhook_user(async_session), await webhook_user(async_session)
        proposals = [
            Reconciliation(
                user_id=user_id,
                before_status="free",
                before_expires_at=None,
                before_verified_at=None,
                before_subscription={},
                snapshot=SubscriptionSnapshot("premium", None, "premium.transfer", False, False),
                verified_at=datetime.now(UTC),
                outcome="changed",
            )
            for user_id in sorted([first.id, second.id], key=str)
        ]
        calls = 0

        async def apply_or_conflict(db: AsyncSession, candidate: Reconciliation) -> Reconciliation:
            nonlocal calls
            calls += 1
            if calls == 2:
                return replace(candidate, outcome="conflict")
            return await apply_reconciliation(db, candidate)

        transfer = {
            "event": {
                "id": "transfer-second-failure",
                "type": "TRANSFER",
                "transferred_from": [str(first.id)],
                "transferred_to": [str(second.id)],
                "environment": "PRODUCTION",
            }
        }
        with (
            patch("app.api.purchase.get_settings", return_value=settings()),
            patch("app.services.purchase.get_settings", return_value=settings()),
            patch("app.services.purchase.read_reconciliation", new=AsyncMock(side_effect=proposals)),
            patch("app.services.purchase.apply_reconciliation", side_effect=apply_or_conflict),
        ):
            response = await client.post(
                "/api/v1/purchases/webhook", json=transfer, headers={"Authorization": "secret"}
            )

        assert response.status_code == 503
        await async_session.refresh(first)
        await async_session.refresh(second)
        assert first.subscription_status == second.subscription_status == "free"
    finally:
        await clear_webhooks(async_session)
