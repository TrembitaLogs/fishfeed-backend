"""RevenueCat webhook service contracts."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.purchase import WebhookTransaction
from app.models.user import User
from app.schemas.purchase import WebhookEvent
from app.services.purchase import (
    PurchaseError,
    Reconciliation,
    SubscriptionSnapshot,
    UserNotFoundError,
    WebhookAuditConflict,
    WebhookRetryableError,
    apply_reconciliation,
    check_idempotency,
    log_webhook_transaction,
    process_webhook,
    release_idempotency_lock,
    verify_webhook_authorization,
)


async def clear_purchase_state(session: AsyncSession) -> None:
    await session.execute(text("TRUNCATE TABLE webhook_transactions, users CASCADE"))
    await session.commit()


async def user(session: AsyncSession) -> User:
    record = User(email=f"{uuid4()}@example.com", password_hash="unused")
    session.add(record)
    await session.commit()
    return record


def proposal(user_id: UUID, *, status: str = "premium") -> Reconciliation:
    return Reconciliation(
        user_id=user_id,
        before_status="free",
        before_expires_at=None,
        before_verified_at=None,
        before_subscription={},
        snapshot=SubscriptionSnapshot(
            status=status,  # type: ignore[arg-type]
            expires_at=None,
            product_id="premium.lifetime" if status == "premium" else None,
            will_renew=False,
            is_trial=False,
        ),
        verified_at=datetime.now(UTC),
        outcome="changed",
    )


def event(event_type: str, **values: object) -> WebhookEvent:
    return WebhookEvent.model_validate({"event": {"id": "service-event", "type": event_type, **values}})


@pytest.mark.asyncio(loop_scope="session")
async def test_promotional_premium_purchase_uses_authoritative_reconciliation(
    async_session: AsyncSession, redis_client
):
    await clear_purchase_state(async_session)
    try:
        record = await user(async_session)
        fetched = proposal(record.id)
        webhook = event(
            "NON_RENEWING_PURCHASE",
            app_user_id=str(record.id),
            store="PROMOTIONAL",
            environment="PRODUCTION",
            entitlement_ids=["premium"],
        )
        with patch("app.services.purchase.read_reconciliation", new=AsyncMock(return_value=fetched)):
            disposition, results = await process_webhook(async_session, webhook, redis_client)

        assert disposition == "success"
        assert results == [fetched]
        await async_session.refresh(record)
        assert record.subscription_status == "premium"
        assert record.subscription_verified_at == fetched.verified_at
    finally:
        await clear_purchase_state(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_raw_webhook_expiry_cannot_grant_lifetime_without_provider_snapshot(
    async_session: AsyncSession, redis_client
):
    await clear_purchase_state(async_session)
    try:
        record = await user(async_session)
        webhook = event(
            "INITIAL_PURCHASE",
            app_user_id=str(record.id),
            environment="PRODUCTION",
            entitlement_ids=["premium"],
            expiration_at_ms=253402300799999,
        )
        with patch(
            "app.services.purchase.read_reconciliation", new=AsyncMock(return_value=proposal(record.id, status="free"))
        ):
            await process_webhook(async_session, webhook, redis_client)

        await async_session.refresh(record)
        assert record.subscription_status == "free"
        assert record.subscription_expires_at is None
    finally:
        await clear_purchase_state(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_production_sandbox_skips_before_identity_or_provider(async_session: AsyncSession, redis_client):
    webhook = event("CANCELLATION", app_user_id=str(uuid4()), environment="SANDBOX")
    with (
        patch("app.services.purchase.get_settings", return_value=SimpleNamespace(ENVIRONMENT="production")),
        patch("app.services.purchase.read_reconciliation", new=AsyncMock()) as reader,
    ):
        disposition, results = await process_webhook(async_session, webhook, redis_client)

    assert disposition == "skipped"
    assert results == []
    reader.assert_not_awaited()


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_ads_uses_fresh_locked_user_without_provider(async_session: AsyncSession, redis_client):
    await clear_purchase_state(async_session)
    try:
        record = await user(async_session)
        record.subscription_status = "premium"
        await async_session.commit()
        webhook = event(
            "NON_RENEWING_PURCHASE",
            app_user_id=str(record.id),
            environment="PRODUCTION",
            product_id="fishfeed_remove_ads",
            entitlement_ids=["remove_ads"],
        )
        with (
            patch("app.services.purchase.get_settings", return_value=SimpleNamespace(ENVIRONMENT="production")),
            patch("app.services.purchase.read_reconciliation", new=AsyncMock()) as reader,
        ):
            disposition, results = await process_webhook(async_session, webhook, redis_client)

        assert disposition == "success" and results == []
        reader.assert_not_awaited()
        await async_session.refresh(record)
        assert record.subscription_status == "premium"
        assert record.settings["non_subscriptions"]["products"] == ["fishfeed_remove_ads"]
    finally:
        await clear_purchase_state(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_ads_uses_exact_app_user_id_not_an_alias(async_session: AsyncSession, redis_client):
    await clear_purchase_state(async_session)
    try:
        purchaser, alias = await user(async_session), await user(async_session)
        webhook = event(
            "NON_RENEWING_PURCHASE",
            app_user_id=str(purchaser.id),
            aliases=[str(alias.id)],
            environment="PRODUCTION",
            product_id="fishfeed_remove_ads",
            entitlement_ids=["remove_ads"],
        )
        with patch("app.services.purchase.get_settings", return_value=SimpleNamespace(ENVIRONMENT="production")):
            await process_webhook(async_session, webhook, redis_client)

        await async_session.refresh(purchaser)
        await async_session.refresh(alias)
        assert purchaser.settings["non_subscriptions"]["products"] == ["fishfeed_remove_ads"]
        assert "non_subscriptions" not in alias.settings
    finally:
        await clear_purchase_state(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_alias_and_unrelated_events_skip_without_ads_write(async_session: AsyncSession, redis_client):
    await clear_purchase_state(async_session)
    try:
        record = await user(async_session)
        webhook = event("SUBSCRIBER_ALIAS", app_user_id=str(record.id), aliases=[str(uuid4())])
        with patch("app.services.purchase.read_reconciliation", new=AsyncMock()) as reader:
            disposition, results = await process_webhook(async_session, webhook, redis_client)

        assert (disposition, results) == ("skipped", [])
        reader.assert_not_awaited()
        await async_session.refresh(record)
        assert record.settings == {}
    finally:
        await clear_purchase_state(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_unsupported_application_environment_cannot_write_remove_ads(async_session: AsyncSession, redis_client):
    await clear_purchase_state(async_session)
    try:
        record = await user(async_session)
        webhook = event(
            "NON_RENEWING_PURCHASE",
            app_user_id=str(record.id),
            environment="PRODUCTION",
            entitlement_ids=["remove_ads"],
        )
        with patch("app.services.purchase.get_settings", return_value=SimpleNamespace(ENVIRONMENT="staging")):
            with pytest.raises(PurchaseError, match="unsupported environment") as error:
                await process_webhook(async_session, webhook, redis_client)
        assert error.value.status_code == 503
        await async_session.refresh(record)
        assert record.settings == {}
    finally:
        await clear_purchase_state(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_redis_reconciliation_failure_has_storage_cause(async_session: AsyncSession):
    record = await user(async_session)
    webhook = event("INITIAL_PURCHASE", app_user_id=str(record.id), environment="PRODUCTION")
    unavailable_redis = SimpleNamespace(ttl=AsyncMock(side_effect=RedisError("unavailable")))
    try:
        with patch(
            "app.services.purchase.get_settings",
            return_value=SimpleNamespace(ENVIRONMENT="production", REVENUECAT_API_KEY="key"),
        ):
            with pytest.raises(PurchaseError) as error:
                await process_webhook(async_session, webhook, unavailable_redis)
        assert getattr(error.value, "failure_kind", None) == "storage"
    finally:
        await clear_purchase_state(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_disappearing_reconciliation_participant_is_retryable(async_session: AsyncSession, redis_client):
    await clear_purchase_state(async_session)
    try:
        record = await user(async_session)
        webhook = event("INITIAL_PURCHASE", app_user_id=str(record.id), environment="PRODUCTION")
        with (
            patch("app.services.purchase.get_settings", return_value=SimpleNamespace(ENVIRONMENT="production")),
            patch(
                "app.services.purchase.read_reconciliation",
                side_effect=UserNotFoundError(record.id),
            ),
            pytest.raises(WebhookRetryableError, match="participant disappeared"),
        ):
            await process_webhook(async_session, webhook, redis_client)
    finally:
        await clear_purchase_state(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_unresolved_ads_environment_is_retryable(async_session: AsyncSession, redis_client):
    await clear_purchase_state(async_session)
    try:
        record = await user(async_session)
        webhook = event("NON_RENEWING_PURCHASE", app_user_id=str(record.id), entitlement_ids=["remove_ads"])
        with patch("app.services.purchase.get_settings", return_value=SimpleNamespace(ENVIRONMENT="production")):
            with pytest.raises(PurchaseError, match="cannot authorize") as error:
                await process_webhook(async_session, webhook, redis_client)
        assert error.value.status_code == 503
    finally:
        await clear_purchase_state(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_transfer_fetches_every_snapshot_before_apply_and_rolls_back_group(async_engine, redis_client):
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    async with sessions() as setup:
        first, second = await user(setup), await user(setup)
        first_id, second_id = first.id, second.id
    webhook = event(
        "TRANSFER",
        transferred_from=[str(first_id)],
        transferred_to=[str(second_id)],
        environment="PRODUCTION",
    )
    reads: list[UUID] = []

    async def read(_, __, user_id: UUID) -> Reconciliation:
        reads.append(user_id)
        return proposal(user_id)

    async with sessions() as session:
        applies = 0

        async def apply(db, fetched: Reconciliation) -> Reconciliation:
            nonlocal applies
            assert reads == sorted([first_id, second_id], key=str)
            applies += 1
            if applies == 2:
                return replace(fetched, outcome="conflict")
            return await apply_reconciliation(db, fetched)

        with (
            patch("app.services.purchase.get_settings", return_value=SimpleNamespace(ENVIRONMENT="production")),
            patch("app.services.purchase.read_reconciliation", side_effect=read),
            patch("app.services.purchase.apply_reconciliation", side_effect=apply),
            pytest.raises(PurchaseError, match="changed during reconciliation"),
        ):
            await process_webhook(session, webhook, redis_client)
        await session.rollback()

    async with sessions() as verify:
        statuses = list(
            (await verify.scalars(select(User.subscription_status).where(User.id.in_([first_id, second_id])))).all()
        )
    assert statuses == ["free", "free"]


@pytest.mark.asyncio(loop_scope="session")
async def test_failed_audit_is_retryable_but_terminal_audit_is_not_overwritten(
    async_session: AsyncSession, redis_client
):
    await clear_purchase_state(async_session)
    await redis_client.flushdb()
    try:
        await log_webhook_transaction(
            async_session,
            "retry-audit",
            "INITIAL_PURCHASE",
            None,
            {"event": {"id": "retry-audit"}},
            processing_result="error",
            error_message="timeout",
        )
        await async_session.commit()
        duplicate, handle = await check_idempotency(async_session, redis_client, "retry-audit")
        assert not duplicate and handle is not None
        await release_idempotency_lock(redis_client, handle)

        terminal = await log_webhook_transaction(
            async_session,
            "retry-audit",
            "INITIAL_PURCHASE",
            None,
            {"event": {"id": "retry-audit"}},
            processing_result="success",
        )
        await async_session.commit()
        duplicate, handle = await check_idempotency(async_session, redis_client, "retry-audit")
        assert duplicate and handle is None

        unchanged = await log_webhook_transaction(
            async_session,
            "retry-audit",
            "INITIAL_PURCHASE",
            None,
            {"event": {"id": "retry-audit"}},
            processing_result="error",
            error_message="late failure",
        )
        assert terminal.processing_result == unchanged.processing_result == "success"
        assert (
            await async_session.scalar(
                select(func.count())
                .select_from(WebhookTransaction)
                .where(WebhookTransaction.transaction_id == "retry-audit")
            )
            == 1
        )
    finally:
        await clear_purchase_state(async_session)
        await redis_client.flushdb()


@pytest.mark.asyncio(loop_scope="session")
async def test_legacy_dedup_fallback_requires_the_matching_event_id(async_session: AsyncSession, redis_client):
    await clear_purchase_state(async_session)
    await redis_client.flushdb()
    try:
        await log_webhook_transaction(
            async_session,
            "legacy-store-id",
            "INITIAL_PURCHASE",
            None,
            {"event": {"id": "event-id"}},
            processing_result="success",
        )
        await async_session.commit()
        duplicate, handle = await check_idempotency(
            async_session, redis_client, "event-id", legacy_transaction_id="legacy-store-id"
        )
        assert duplicate and handle is None

        duplicate, handle = await check_idempotency(
            async_session, redis_client, "different-event", legacy_transaction_id="legacy-store-id"
        )
        assert not duplicate and handle is not None
        await release_idempotency_lock(redis_client, handle)
    finally:
        await clear_purchase_state(async_session)
        await redis_client.flushdb()


@pytest.mark.asyncio(loop_scope="session")
async def test_lookup_failure_releases_acquired_lock(redis_client):
    await redis_client.flushdb()
    database = SimpleNamespace(scalar=AsyncMock(side_effect=RuntimeError("lookup failed")))
    with pytest.raises(RuntimeError, match="lookup failed"):
        await check_idempotency(database, redis_client, "lookup-failure")
    assert await redis_client.get("webhook_lock:lookup-failure") is None
    await redis_client.flushdb()


@pytest.mark.asyncio(loop_scope="session")
async def test_expired_owner_cannot_delete_replacement_lock(redis_client):
    await redis_client.flushdb()
    try:
        await redis_client.set("webhook_lock:replacement", "replacement", ex=30)
        await release_idempotency_lock(redis_client, ("webhook_lock:replacement", "expired-owner"))
        assert await redis_client.get("webhook_lock:replacement") == "replacement"
    finally:
        await redis_client.flushdb()


@pytest.mark.asyncio(loop_scope="session")
async def test_lock_release_failure_is_nonfatal():
    unavailable_redis = SimpleNamespace(execute_command=AsyncMock(side_effect=RedisError("unavailable")))
    await release_idempotency_lock(unavailable_redis, ("webhook_lock:failure", "token"))


@pytest.mark.asyncio(loop_scope="session")
async def test_two_sessions_keep_a_terminal_audit_when_a_late_writer_loses(async_engine, redis_client):
    """The unique audit winner stays terminal after a concurrent losing insert."""
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    async with sessions() as first, sessions() as late:
        await log_webhook_transaction(
            first,
            "audit-race",
            "INITIAL_PURCHASE",
            None,
            {"event": {"id": "audit-race"}},
            processing_result="success",
        )
        loser = asyncio.create_task(
            log_webhook_transaction(
                late,
                "audit-race",
                "INITIAL_PURCHASE",
                None,
                {"event": {"id": "audit-race"}},
                processing_result="error",
                error_message="late",
            )
        )
        await asyncio.sleep(0.05)
        assert not loser.done()
        await first.commit()
        with pytest.raises(WebhookAuditConflict):
            await loser
        await late.rollback()

    async with sessions() as verify:
        winner = await verify.scalar(
            select(WebhookTransaction).where(WebhookTransaction.transaction_id == "audit-race")
        )
        assert winner is not None and winner.processing_result == "success"
        duplicate, handle = await check_idempotency(verify, redis_client, "audit-race")
        assert duplicate and handle is None


@pytest.mark.asyncio(loop_scope="session")
async def test_stale_ads_session_preserves_newer_subscription_settings(async_engine, redis_client):
    """The ads writer refreshes its locked row instead of writing a stale JSON snapshot."""
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    async with sessions() as setup:
        record = await user(setup)
        user_id = record.id
    async with sessions() as stale, sessions() as writer:
        stale_user = await stale.get(User, user_id)
        assert stale_user is not None and stale_user.settings == {}
        current = await writer.get(User, user_id)
        assert current is not None
        current.settings = {"subscription": {"product_id": "premium.yearly", "will_renew": True}}
        await writer.commit()

        webhook = event(
            "NON_RENEWING_PURCHASE",
            app_user_id=str(user_id),
            environment="PRODUCTION",
            product_id="fishfeed_remove_ads",
            entitlement_ids=["remove_ads"],
        )
        with patch("app.services.purchase.get_settings", return_value=SimpleNamespace(ENVIRONMENT="production")):
            await process_webhook(stale, webhook, redis_client)
        await stale.commit()

    async with sessions() as verify:
        merged = await verify.get(User, user_id)
        assert merged is not None
        assert merged.settings["subscription"] == {"product_id": "premium.yearly", "will_renew": True}
        assert merged.settings["non_subscriptions"]["products"] == ["fishfeed_remove_ads"]


def test_authorization_uses_exact_constant_time_value():
    assert verify_webhook_authorization("secret", "secret")
    assert not verify_webhook_authorization("wrong", "secret")
