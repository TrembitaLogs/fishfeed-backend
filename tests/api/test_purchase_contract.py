"""Exercise RevenueCat's flat webhook contract through the real API and DB.

Field layout: https://www.revenuecat.com/docs/integrations/webhooks/sample-events
Synthetic IDs and fixed timestamps deliberately do not use our schema as a builder.
"""

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.purchase import WebhookTransaction
from app.models.user import User


@pytest.fixture
def revenuecat_event():
    return {
        "api_version": "1.0",
        "event": {
            "id": str(uuid4()),
            "type": "INITIAL_PURCHASE",
            "app_user_id": str(uuid4()),
            "original_app_user_id": "$RCAnonymousID:contract-test",
            "aliases": ["$RCAnonymousID:contract-test"],
            "product_id": "fishfeed_premium_monthly:monthly-base",
            "entitlement_ids": ["premium"],
            "purchased_at_ms": 1893456000000,
            "expiration_at_ms": 1896134400123,
            "event_timestamp_ms": 1893456001000,
            "transaction_id": str(uuid4()),
            "store": "PLAY_STORE",
            "environment": "SANDBOX",
            "period_type": "NORMAL",
        },
    }


@pytest.fixture
def webhook_secret():
    with patch("app.api.purchase.get_settings") as settings:
        settings.return_value.REVENUECAT_WEBHOOK_SECRET = "contract-test-secret"
        yield {"Authorization": "contract-test-secret"}


@pytest_asyncio.fixture(loop_scope="session")
async def contract_user(async_session, revenuecat_event):
    user = User(
        id=UUID(revenuecat_event["event"]["app_user_id"]),
        email=f"{uuid4()}@example.com",
        password_hash="unused",
        subscription_status="free",
    )
    async_session.add(user)
    await async_session.commit()
    return user


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("event_type", ["INITIAL_PURCHASE", "RENEWAL"])
async def test_flat_purchase_persists_expiry_and_audit_fields(
    client,
    async_session,
    contract_user,
    revenuecat_event,
    webhook_secret,
    event_type,
):
    revenuecat_event["event"]["type"] = event_type
    response = await client.post("/api/v1/purchases/webhook", json=revenuecat_event, headers=webhook_secret)
    assert response.json()["success"] is True
    await async_session.refresh(contract_user)
    assert contract_user.subscription_status == "premium"
    assert contract_user.subscription_expires_at == datetime(2030, 2, 1, 0, 0, 0, 123000, tzinfo=UTC)
    assert contract_user.settings["subscription"]["product_id"] == "fishfeed_premium_monthly:monthly-base"
    audit = (
        await async_session.execute(
            select(WebhookTransaction).where(WebhookTransaction.user_id == str(contract_user.id))
        )
    ).scalar_one()
    assert audit.payload["api_version"] == "1.0"
    assert audit.payload["event"]["expiration_at_ms"] == 1896134400123
    assert audit.payload["event"]["entitlement_ids"] == ["premium"]


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("expiry", [None, "missing"])
async def test_purchase_without_expiry_never_grants_permanent_premium(
    client,
    async_session,
    contract_user,
    revenuecat_event,
    webhook_secret,
    expiry,
):
    if expiry == "missing":
        revenuecat_event["event"].pop("expiration_at_ms")
    else:
        revenuecat_event["event"]["expiration_at_ms"] = expiry
    response = await client.post("/api/v1/purchases/webhook", json=revenuecat_event, headers=webhook_secret)
    assert response.json()["success"] is False
    await async_session.refresh(contract_user)
    assert contract_user.subscription_status == "free"


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("entitlements", [[], None, ["remove_ads"], "missing"])
async def test_unrelated_entitlements_do_not_grant_premium(
    client,
    async_session,
    contract_user,
    revenuecat_event,
    webhook_secret,
    entitlements,
):
    if entitlements == "missing":
        revenuecat_event["event"].pop("entitlement_ids")
    else:
        revenuecat_event["event"]["entitlement_ids"] = entitlements
    response = await client.post("/api/v1/purchases/webhook", json=revenuecat_event, headers=webhook_secret)
    assert response.status_code == 200
    await async_session.refresh(contract_user)
    assert contract_user.subscription_status == "free"


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("expiry", [-1, True, "1896134400123", 1896134400123.5, 253402300800000])
async def test_invalid_expiry_is_rejected(
    client,
    async_session,
    contract_user,
    revenuecat_event,
    webhook_secret,
    expiry,
):
    revenuecat_event["event"]["expiration_at_ms"] = expiry
    response = await client.post("/api/v1/purchases/webhook", json=revenuecat_event, headers=webhook_secret)
    assert response.json()["success"] is False
    await async_session.refresh(contract_user)
    assert contract_user.subscription_status == "free"


@pytest.mark.asyncio(loop_scope="session")
async def test_unrelated_expiration_does_not_revoke_premium(
    client,
    async_session,
    contract_user,
    revenuecat_event,
    webhook_secret,
):
    contract_user.subscription_status = "premium"
    contract_user.subscription_expires_at = datetime(2030, 2, 1, tzinfo=UTC)
    await async_session.commit()
    revenuecat_event["event"].update(type="EXPIRATION", entitlement_ids=["another_entitlement"])
    response = await client.post("/api/v1/purchases/webhook", json=revenuecat_event, headers=webhook_secret)
    assert response.json()["success"] is True
    await async_session.refresh(contract_user)
    assert contract_user.subscription_status == "premium"


@pytest.mark.asyncio(loop_scope="session")
async def test_lifecycle_events_share_transaction_but_retries_share_event_id(
    client,
    async_session,
    contract_user,
    revenuecat_event,
    webhook_secret,
):
    # Pre-existing purchase models a row written before event-ID deduplication.
    contract_user.subscription_status = "premium"
    contract_user.subscription_expires_at = datetime(2030, 2, 1, tzinfo=UTC)
    contract_user.settings = {"subscription": {"will_renew": True}}
    async_session.add(
        WebhookTransaction(
            transaction_id=revenuecat_event["event"]["transaction_id"],
            event_type="INITIAL_PURCHASE",
            user_id=str(contract_user.id),
            payload=revenuecat_event,
            processing_result="success",
        )
    )
    await async_session.commit()
    for event_type, expected_status in [("CANCELLATION", "premium"), ("EXPIRATION", "free")]:
        revenuecat_event["event"].update(id=str(uuid4()), type=event_type)
        response = await client.post("/api/v1/purchases/webhook", json=revenuecat_event, headers=webhook_secret)
        assert response.json()["success"] is True
        await async_session.refresh(contract_user)
        assert contract_user.subscription_status == expected_status
        assert contract_user.settings["subscription"]["will_renew"] is False
        retry = await client.post("/api/v1/purchases/webhook", json=revenuecat_event, headers=webhook_secret)
        assert retry.json()["message"] == "Already processed"
    audits = (
        (
            await async_session.execute(
                select(WebhookTransaction).where(WebhookTransaction.user_id == str(contract_user.id))
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_retry_of_legacy_audit_does_not_overwrite_newer_subscription(
    client,
    async_session,
    contract_user,
    revenuecat_event,
    webhook_secret,
):
    newer_expiry = datetime(2030, 3, 1, tzinfo=UTC)
    contract_user.subscription_status = "premium"
    contract_user.subscription_expires_at = newer_expiry
    async_session.add(
        WebhookTransaction(
            transaction_id=revenuecat_event["event"]["transaction_id"],
            event_type="INITIAL_PURCHASE",
            user_id=str(contract_user.id),
            payload=revenuecat_event,
            processing_result="success",
        )
    )
    await async_session.commit()
    response = await client.post("/api/v1/purchases/webhook", json=revenuecat_event, headers=webhook_secret)
    assert response.json()["message"] == "Already processed"
    await async_session.refresh(contract_user)
    assert contract_user.subscription_expires_at == newer_expiry


@pytest.mark.asyncio(loop_scope="session")
async def test_flat_remove_ads_preserves_premium_and_saves_entitlement(
    client,
    async_session,
    contract_user,
    revenuecat_event,
    webhook_secret,
):
    contract_user.subscription_status = "premium"
    contract_user.subscription_expires_at = datetime(2030, 2, 1, tzinfo=UTC)
    await async_session.commit()
    revenuecat_event["event"].update(
        type="NON_RENEWING_PURCHASE",
        entitlement_ids=["remove_ads"],
        product_id="fishfeed_remove_ads",
        expiration_at_ms=None,
    )
    response = await client.post("/api/v1/purchases/webhook", json=revenuecat_event, headers=webhook_secret)
    assert response.json()["success"] is True
    await async_session.refresh(contract_user)
    assert contract_user.subscription_status == "premium"
    assert contract_user.settings["non_subscriptions"]["entitlements"] == ["remove_ads"]
