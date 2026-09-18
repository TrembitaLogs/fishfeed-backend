"""Provider-shaped webhook contracts kept independent of Pydantic builders."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.purchase import WebhookTransaction
from app.schemas.purchase import WebhookEvent


def test_transfer_does_not_require_app_user_id():
    event = WebhookEvent.model_validate(
        {
            "event": {
                "id": "transfer-contract",
                "type": "TRANSFER",
                "transferred_from": ["$RCAnonymousID:old"],
                "transferred_to": ["11111111-1111-4111-8111-111111111111"],
            }
        }
    )
    assert event.event.app_user_id is None
    assert len(event.event.transferred_to) == 1


def test_promotional_purchase_is_parseable():
    event = WebhookEvent.model_validate(
        {
            "event": {
                "id": "promo-contract",
                "type": "NON_RENEWING_PURCHASE",
                "app_user_id": "11111111-1111-4111-8111-111111111111",
                "store": "PROMOTIONAL",
                "environment": "PRODUCTION",
                "entitlement_ids": ["premium"],
            }
        }
    )
    assert event.event.store == "PROMOTIONAL"


@pytest.mark.asyncio(loop_scope="session")
async def test_production_sandbox_event_is_audited_skipped_before_provider(client: AsyncClient, async_session):
    payload = {
        "event": {
            "id": "sandbox-contract",
            "type": "CANCELLATION",
            "app_user_id": str(uuid4()),
            "environment": "SANDBOX",
        }
    }
    route_settings = SimpleNamespace(ENVIRONMENT="production", REVENUECAT_WEBHOOK_SECRET="secret")
    with (
        patch("app.api.purchase.get_settings", return_value=route_settings),
        patch("app.services.purchase.get_settings", return_value=route_settings),
        patch("app.services.purchase.read_reconciliation", new=AsyncMock()) as reader,
    ):
        response = await client.post("/api/v1/purchases/webhook", json=payload, headers={"Authorization": "secret"})

    assert response.status_code == 200
    reader.assert_not_awaited()
    audit = await async_session.scalar(
        select(WebhookTransaction).where(WebhookTransaction.transaction_id == "sandbox-contract")
    )
    assert audit is not None and audit.processing_result == "skipped"


@pytest.mark.asyncio(loop_scope="session")
async def test_production_sandbox_remove_ads_is_audited_skipped_before_mutation(client: AsyncClient, async_session):
    from app.models.user import User

    record = User(email=f"{uuid4()}@example.com", password_hash="unused")
    async_session.add(record)
    await async_session.commit()
    payload = {
        "event": {
            "id": "sandbox-ads-contract",
            "type": "NON_RENEWING_PURCHASE",
            "app_user_id": str(record.id),
            "environment": "SANDBOX",
            "entitlement_ids": ["remove_ads"],
        }
    }
    route_settings = SimpleNamespace(ENVIRONMENT="production", REVENUECAT_WEBHOOK_SECRET="secret")
    with (
        patch("app.api.purchase.get_settings", return_value=route_settings),
        patch("app.services.purchase.get_settings", return_value=route_settings),
    ):
        response = await client.post("/api/v1/purchases/webhook", json=payload, headers={"Authorization": "secret"})

    assert response.status_code == 200
    await async_session.refresh(record)
    assert record.settings == {}
    audit = await async_session.scalar(
        select(WebhookTransaction).where(WebhookTransaction.transaction_id == "sandbox-ads-contract")
    )
    assert audit is not None and audit.processing_result == "skipped"
