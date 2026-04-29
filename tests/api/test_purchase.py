"""Tests for purchase API endpoints (webhook security, idempotency, logging)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.purchase import WebhookTransaction
from app.models.user import User


async def cleanup_test_data(session: AsyncSession) -> None:
    """Helper to cleanup test data."""
    await session.execute(text("TRUNCATE TABLE webhook_transactions CASCADE"))
    await session.execute(text("TRUNCATE TABLE users CASCADE"))
    await session.commit()


async def create_test_user(
    session: AsyncSession,
    email: str = "webhook_test@example.com",
) -> User:
    """Create a test user."""
    user = User(
        email=email,
        password_hash="test_hash",
        subscription_status="free",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def create_webhook_payload(
    event_type: str,
    app_user_id: str,
    transaction_id: str = "txn_test_123",
) -> dict:
    """Create a RevenueCat webhook payload."""
    return {
        "event": {
            "type": event_type,
            "app_user_id": app_user_id,
            "transaction_id": transaction_id,
            "entitlements": [
                {
                    "product_identifier": "com.example.premium",
                    "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                }
            ],
        }
    }


class TestWebhookAuthorization:
    """Tests for webhook Authorization header validation."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_valid_authorization_processes_webhook(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """Test webhook with matching Authorization header is processed successfully."""
        await cleanup_test_data(async_session)
        await redis_client.flushdb()

        try:
            user = await create_test_user(async_session)
            payload = create_webhook_payload(
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
                transaction_id="txn_valid_auth_001",
            )
            payload_bytes = json.dumps(payload).encode("utf-8")
            secret = "Bearer test_webhook_secret"

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = secret

                response = await client.post(
                    "/api/v1/purchases/webhook",
                    content=payload_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": secret,
                    },
                )

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True

            # Verify user was updated
            await async_session.refresh(user)
            assert user.subscription_status == "premium"

        finally:
            await cleanup_test_data(async_session)
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_invalid_authorization_returns_401(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Test webhook with mismatching Authorization header returns 401 Unauthorized."""
        await cleanup_test_data(async_session)

        try:
            user = await create_test_user(async_session)
            payload = create_webhook_payload(
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
            )
            payload_bytes = json.dumps(payload).encode("utf-8")
            secret = "Bearer test_webhook_secret"

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = secret

                response = await client.post(
                    "/api/v1/purchases/webhook",
                    content=payload_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer wrong_value",
                    },
                )

            assert response.status_code == 401
            assert "Invalid Authorization" in response.json()["detail"]

        finally:
            await cleanup_test_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_missing_authorization_returns_401(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Test webhook with missing Authorization header returns 401 Unauthorized."""
        await cleanup_test_data(async_session)

        try:
            user = await create_test_user(async_session)
            payload = create_webhook_payload(
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
            )
            payload_bytes = json.dumps(payload).encode("utf-8")
            secret = "Bearer test_webhook_secret"

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = secret

                response = await client.post(
                    "/api/v1/purchases/webhook",
                    content=payload_bytes,
                    headers={"Content-Type": "application/json"},
                )

            assert response.status_code == 401
            assert "Missing Authorization" in response.json()["detail"]

        finally:
            await cleanup_test_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_webhook_processes_without_secret_configured(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """Test webhook processes normally when no secret is configured (dev mode)."""
        await cleanup_test_data(async_session)
        await redis_client.flushdb()

        try:
            user = await create_test_user(async_session)
            payload = create_webhook_payload(
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
                transaction_id="txn_no_secret_001",
            )
            payload_bytes = json.dumps(payload).encode("utf-8")

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = None

                response = await client.post(
                    "/api/v1/purchases/webhook",
                    content=payload_bytes,
                    headers={"Content-Type": "application/json"},
                )

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True

        finally:
            await cleanup_test_data(async_session)
            await redis_client.flushdb()


class TestWebhookIdempotency:
    """Tests for webhook idempotency."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_duplicate_webhook_returns_200_without_reprocessing(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """Test that duplicate webhook with same transaction_id returns 200 without reprocessing."""
        await cleanup_test_data(async_session)
        await redis_client.flushdb()

        try:
            user = await create_test_user(async_session)
            transaction_id = "txn_duplicate_test_001"
            payload = create_webhook_payload(
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
                transaction_id=transaction_id,
            )
            payload_bytes = json.dumps(payload).encode("utf-8")

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = None

                # First request
                response1 = await client.post(
                    "/api/v1/purchases/webhook",
                    content=payload_bytes,
                    headers={"Content-Type": "application/json"},
                )
                assert response1.status_code == 200
                assert response1.json()["success"] is True
                assert "processed successfully" in response1.json()["message"]

                # Second request (duplicate)
                response2 = await client.post(
                    "/api/v1/purchases/webhook",
                    content=payload_bytes,
                    headers={"Content-Type": "application/json"},
                )
                assert response2.status_code == 200
                assert response2.json()["success"] is True
                assert "Already processed" in response2.json()["message"]

            # Verify only one transaction record exists
            stmt = select(WebhookTransaction).where(
                WebhookTransaction.transaction_id == transaction_id
            )
            result = await async_session.execute(stmt)
            transactions = result.scalars().all()
            assert len(transactions) == 1

        finally:
            await cleanup_test_data(async_session)
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_concurrent_webhooks_processed_only_once(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """Test that concurrent webhooks with same transaction_id are processed only once."""
        await cleanup_test_data(async_session)
        await redis_client.flushdb()

        try:
            user = await create_test_user(async_session)
            transaction_id = "txn_concurrent_test_001"
            payload = create_webhook_payload(
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
                transaction_id=transaction_id,
            )
            payload_bytes = json.dumps(payload).encode("utf-8")

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = None

                # Send concurrent requests
                async def send_webhook():
                    return await client.post(
                        "/api/v1/purchases/webhook",
                        content=payload_bytes,
                        headers={"Content-Type": "application/json"},
                    )

                responses = await asyncio.gather(
                    send_webhook(),
                    send_webhook(),
                    send_webhook(),
                )

            # All should return 200
            for response in responses:
                assert response.status_code == 200
                assert response.json()["success"] is True

            # Count how many were "processed successfully" vs "Already processed"
            processed_count = sum(
                1 for r in responses
                if "processed successfully" in r.json().get("message", "")
            )
            already_processed_count = sum(
                1 for r in responses
                if "Already processed" in r.json().get("message", "")
            )

            # At least one should be processed, the rest should be duplicates or locked
            assert processed_count >= 1

            # Verify only one transaction record exists
            stmt = select(WebhookTransaction).where(
                WebhookTransaction.transaction_id == transaction_id
            )
            result = await async_session.execute(stmt)
            transactions = result.scalars().all()
            assert len(transactions) == 1

        finally:
            await cleanup_test_data(async_session)
            await redis_client.flushdb()


class TestWebhookTransactionLogging:
    """Tests for webhook transaction logging."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_successful_webhook_logged_to_database(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """Test that successful webhook events are logged to WebhookTransaction table."""
        await cleanup_test_data(async_session)
        await redis_client.flushdb()

        try:
            user = await create_test_user(async_session)
            transaction_id = "txn_logging_test_001"
            payload = create_webhook_payload(
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
                transaction_id=transaction_id,
            )
            payload_bytes = json.dumps(payload).encode("utf-8")

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = None

                response = await client.post(
                    "/api/v1/purchases/webhook",
                    content=payload_bytes,
                    headers={"Content-Type": "application/json"},
                )

            assert response.status_code == 200

            # Verify transaction was logged
            stmt = select(WebhookTransaction).where(
                WebhookTransaction.transaction_id == transaction_id
            )
            result = await async_session.execute(stmt)
            transaction = result.scalar_one_or_none()

            assert transaction is not None
            assert transaction.event_type == "INITIAL_PURCHASE"
            assert transaction.user_id == str(user.id)
            assert transaction.processing_result == "success"
            assert transaction.error_message is None
            assert transaction.correlation_id is not None
            assert transaction.payload is not None

        finally:
            await cleanup_test_data(async_session)
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_invalid_payload_logged_with_error(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """Test that invalid webhook payloads are logged with error details."""
        await cleanup_test_data(async_session)
        await redis_client.flushdb()

        try:
            invalid_payload = b'{"invalid": "json", not valid}'

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = None

                response = await client.post(
                    "/api/v1/purchases/webhook",
                    content=invalid_payload,
                    headers={"Content-Type": "application/json"},
                )

            # Should still return 200 to prevent retries
            assert response.status_code == 200
            assert response.json()["success"] is False
            assert "Invalid webhook payload" in response.json()["message"]

            # Verify error was logged
            stmt = select(WebhookTransaction).where(
                WebhookTransaction.event_type == "PARSE_ERROR"
            )
            result = await async_session.execute(stmt)
            transaction = result.scalar_one_or_none()

            assert transaction is not None
            assert transaction.processing_result == "error"
            assert transaction.error_message is not None
            assert "Failed to parse" in transaction.error_message

        finally:
            await cleanup_test_data(async_session)
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_webhook_with_unknown_user_logged(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """Test that webhooks for unknown users are still logged."""
        await cleanup_test_data(async_session)
        await redis_client.flushdb()

        try:
            transaction_id = "txn_unknown_user_001"
            payload = create_webhook_payload(
                event_type="INITIAL_PURCHASE",
                app_user_id="00000000-0000-0000-0000-000000000000",
                transaction_id=transaction_id,
            )
            payload_bytes = json.dumps(payload).encode("utf-8")

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = None

                response = await client.post(
                    "/api/v1/purchases/webhook",
                    content=payload_bytes,
                    headers={"Content-Type": "application/json"},
                )

            assert response.status_code == 200

            # Verify transaction was logged even for unknown user
            stmt = select(WebhookTransaction).where(
                WebhookTransaction.transaction_id == transaction_id
            )
            result = await async_session.execute(stmt)
            transaction = result.scalar_one_or_none()

            assert transaction is not None
            assert transaction.event_type == "INITIAL_PURCHASE"
            assert transaction.processing_result == "success"

        finally:
            await cleanup_test_data(async_session)
            await redis_client.flushdb()


class TestWebhookEventTypes:
    """Tests for each RevenueCat event type's effect on user subscription state."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_initial_purchase_sets_premium(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """INITIAL_PURCHASE: status=premium, expires_at set, will_renew=True, product_id stored."""
        await cleanup_test_data(async_session)
        await redis_client.flushdb()

        try:
            user = await create_test_user(async_session)
            expires_at = datetime.now(UTC) + timedelta(days=30)
            payload = {
                "event": {
                    "type": "INITIAL_PURCHASE",
                    "app_user_id": str(user.id),
                    "transaction_id": "txn_initial_001",
                    "entitlements": [
                        {
                            "product_identifier": "fishfeed_premium_monthly",
                            "expires_at": expires_at.isoformat(),
                        }
                    ],
                }
            }
            payload_bytes = json.dumps(payload).encode("utf-8")

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = None

                response = await client.post(
                    "/api/v1/purchases/webhook",
                    content=payload_bytes,
                    headers={"Content-Type": "application/json"},
                )

            assert response.status_code == 200
            assert response.json()["success"] is True

            await async_session.refresh(user)
            assert user.subscription_status == "premium"
            assert user.subscription_expires_at is not None
            subscription = user.settings.get("subscription", {})
            assert subscription.get("will_renew") is True
            assert subscription.get("product_id") == "fishfeed_premium_monthly"

        finally:
            await cleanup_test_data(async_session)
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_renewal_extends_expiry(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """RENEWAL: expires_at moves forward, status stays premium, will_renew=True."""
        await cleanup_test_data(async_session)
        await redis_client.flushdb()

        try:
            old_expiry = datetime.now(UTC) + timedelta(days=1)
            user = User(
                email="renewal_test@example.com",
                password_hash="test_hash",
                subscription_status="premium",
                subscription_expires_at=old_expiry,
                settings={
                    "subscription": {
                        "product_id": "fishfeed_premium_monthly",
                        "will_renew": True,
                    }
                },
            )
            async_session.add(user)
            await async_session.commit()
            await async_session.refresh(user)

            new_expiry = datetime.now(UTC) + timedelta(days=31)
            payload = {
                "event": {
                    "type": "RENEWAL",
                    "app_user_id": str(user.id),
                    "transaction_id": "txn_renewal_001",
                    "entitlements": [
                        {
                            "product_identifier": "fishfeed_premium_monthly",
                            "expires_at": new_expiry.isoformat(),
                        }
                    ],
                }
            }
            payload_bytes = json.dumps(payload).encode("utf-8")

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = None

                response = await client.post(
                    "/api/v1/purchases/webhook",
                    content=payload_bytes,
                    headers={"Content-Type": "application/json"},
                )

            assert response.status_code == 200
            await async_session.refresh(user)
            assert user.subscription_status == "premium"
            assert user.subscription_expires_at is not None
            assert user.subscription_expires_at > old_expiry
            assert user.settings["subscription"]["will_renew"] is True

        finally:
            await cleanup_test_data(async_session)
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_cancellation_keeps_premium_until_expiry(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """CANCELLATION: status=premium (still), will_renew=False, expires_at unchanged."""
        await cleanup_test_data(async_session)
        await redis_client.flushdb()

        try:
            expires_at = datetime.now(UTC) + timedelta(days=15)
            user = User(
                email="cancel_test@example.com",
                password_hash="test_hash",
                subscription_status="premium",
                subscription_expires_at=expires_at,
                settings={
                    "subscription": {
                        "product_id": "fishfeed_premium_monthly",
                        "will_renew": True,
                    }
                },
            )
            async_session.add(user)
            await async_session.commit()
            await async_session.refresh(user)

            payload = {
                "event": {
                    "type": "CANCELLATION",
                    "app_user_id": str(user.id),
                    "transaction_id": "txn_cancel_001",
                    "entitlements": [
                        {
                            "product_identifier": "fishfeed_premium_monthly",
                            "expires_at": expires_at.isoformat(),
                        }
                    ],
                }
            }
            payload_bytes = json.dumps(payload).encode("utf-8")

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = None

                response = await client.post(
                    "/api/v1/purchases/webhook",
                    content=payload_bytes,
                    headers={"Content-Type": "application/json"},
                )

            assert response.status_code == 200
            await async_session.refresh(user)
            assert user.subscription_status == "premium"
            assert user.subscription_expires_at == expires_at
            assert user.settings["subscription"]["will_renew"] is False

        finally:
            await cleanup_test_data(async_session)
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_expiration_reverts_to_free(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """EXPIRATION: status=free, expires_at cleared, will_renew=False."""
        await cleanup_test_data(async_session)
        await redis_client.flushdb()

        try:
            past_expiry = datetime.now(UTC) - timedelta(hours=1)
            user = User(
                email="expire_test@example.com",
                password_hash="test_hash",
                subscription_status="premium",
                subscription_expires_at=past_expiry,
                settings={
                    "subscription": {
                        "product_id": "fishfeed_premium_monthly",
                        "will_renew": False,
                    }
                },
            )
            async_session.add(user)
            await async_session.commit()
            await async_session.refresh(user)

            payload = {
                "event": {
                    "type": "EXPIRATION",
                    "app_user_id": str(user.id),
                    "transaction_id": "txn_expire_001",
                    "entitlements": [
                        {
                            "product_identifier": "fishfeed_premium_monthly",
                            "expires_at": past_expiry.isoformat(),
                        }
                    ],
                }
            }
            payload_bytes = json.dumps(payload).encode("utf-8")

            with patch("app.api.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_WEBHOOK_SECRET = None

                response = await client.post(
                    "/api/v1/purchases/webhook",
                    content=payload_bytes,
                    headers={"Content-Type": "application/json"},
                )

            assert response.status_code == 200
            await async_session.refresh(user)
            assert user.subscription_status == "free"
            assert user.subscription_expires_at is None
            assert user.settings["subscription"]["will_renew"] is False

        finally:
            await cleanup_test_data(async_session)
            await redis_client.flushdb()


class TestSubscriptionEndpoint:
    """Tests for GET /purchases/subscription endpoint."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_get_subscription_requires_auth(
        self,
        client: AsyncClient,
    ):
        """Test that subscription endpoint requires authentication."""
        response = await client.get("/api/v1/purchases/subscription")
        assert response.status_code == 401

    @pytest.mark.asyncio(loop_scope="session")
    async def test_get_subscription_returns_status(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Test getting subscription status for authenticated user."""
        await cleanup_test_data(async_session)

        try:
            # Create user with premium subscription
            user = User(
                email="premium_test@example.com",
                password_hash="$2b$12$test_hash_for_auth",
                subscription_status="premium",
                subscription_expires_at=datetime.now(UTC) + timedelta(days=30),
            )
            async_session.add(user)
            await async_session.commit()
            await async_session.refresh(user)

            # Get a real token for the user
            from app.utils.jwt import create_access_token

            token = create_access_token(str(user.id))

            response = await client.get(
                "/api/v1/purchases/subscription",
                headers={"Authorization": f"Bearer {token}"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "premium"
            assert data["expires_at"] is not None

        finally:
            await cleanup_test_data(async_session)
