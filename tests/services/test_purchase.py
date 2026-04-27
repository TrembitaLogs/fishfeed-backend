"""Tests for purchase service (RevenueCat webhook processing and subscription management)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.purchase import (
    WebhookEntitlement,
    WebhookEvent,
    WebhookEventData,
)
from app.services.purchase import (
    DuplicateWebhookError,
    InvalidSignatureError,
    PurchaseError,
    RevenueCatAPIError,
    RevenueCatNotConfiguredError,
    UserNotFoundError,
    check_idempotency,
    get_subscription_status,
    log_webhook_transaction,
    process_webhook,
    release_idempotency_lock,
    restore_purchases,
    revert_to_free,
    update_subscription_status,
    verify_webhook_signature,
)


async def cleanup_users(session: AsyncSession) -> None:
    """Helper to cleanup users and related data."""
    await session.execute(text("TRUNCATE TABLE users CASCADE"))
    await session.commit()


async def create_test_user(
    session: AsyncSession,
    email: str = "test@example.com",
    subscription_status: str = "free",
    subscription_expires_at: datetime | None = None,
) -> User:
    """Create a test user with specified subscription status."""
    user = User(
        email=email,
        password_hash="test_hash",
        subscription_status=subscription_status,
        subscription_expires_at=subscription_expires_at,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


class TestUpdateSubscriptionStatus:
    """Tests for update_subscription_status function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_updates_user_to_premium(self, async_session: AsyncSession):
        """Test updating user subscription to premium status."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(async_session)
            expires_at = datetime.now(UTC) + timedelta(days=30)

            await update_subscription_status(
                db=async_session,
                user_id=user.id,
                status="premium",
                expires_at=expires_at,
                product_id="com.example.premium_monthly",
                will_renew=True,
            )

            await async_session.refresh(user)
            assert user.subscription_status == "premium"
            assert user.subscription_expires_at == expires_at
            assert user.settings.get("subscription", {}).get("product_id") == "com.example.premium_monthly"
            assert user.settings.get("subscription", {}).get("will_renew") is True
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_raises_user_not_found_for_invalid_id(self, async_session: AsyncSession):
        """Test that UserNotFoundError is raised for non-existent user."""
        await cleanup_users(async_session)
        try:
            with pytest.raises(UserNotFoundError):
                await update_subscription_status(
                    db=async_session,
                    user_id=uuid4(),
                    status="premium",
                )
        finally:
            await cleanup_users(async_session)


class TestGetSubscriptionStatus:
    """Tests for get_subscription_status function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_free_status_for_free_user(self, async_session: AsyncSession):
        """Test getting subscription status for free user."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(async_session)

            status = await get_subscription_status(async_session, user.id)

            assert status.status == "free"
            assert status.expires_at is None
            assert status.will_renew is False
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_premium_status_for_active_subscription(self, async_session: AsyncSession):
        """Test getting subscription status for premium user with active subscription."""
        await cleanup_users(async_session)
        try:
            expires_at = datetime.now(UTC) + timedelta(days=30)
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=expires_at,
            )
            # Set additional settings
            user.settings = {"subscription": {"product_id": "premium_monthly", "will_renew": True}}
            await async_session.commit()

            status = await get_subscription_status(async_session, user.id)

            assert status.status == "premium"
            assert status.expires_at == expires_at
            assert status.product_id == "premium_monthly"
            assert status.will_renew is True
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_expired_and_reverts_for_expired_subscription(self, async_session: AsyncSession):
        """Test that expired subscription returns expired status and reverts user to free."""
        await cleanup_users(async_session)
        try:
            # Create user with expired subscription
            expired_at = datetime.now(UTC) - timedelta(days=1)
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=expired_at,
            )

            status = await get_subscription_status(async_session, user.id)

            assert status.status == "expired"
            assert status.will_renew is False

            # Verify user was reverted to free
            await async_session.refresh(user)
            assert user.subscription_status == "free"
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_raises_user_not_found_for_invalid_id(self, async_session: AsyncSession):
        """Test that UserNotFoundError is raised for non-existent user."""
        await cleanup_users(async_session)
        try:
            with pytest.raises(UserNotFoundError):
                await get_subscription_status(async_session, uuid4())
        finally:
            await cleanup_users(async_session)


class TestRevertToFree:
    """Tests for revert_to_free function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_reverts_premium_user_to_free(self, async_session: AsyncSession):
        """Test reverting premium user to free tier."""
        await cleanup_users(async_session)
        try:
            expires_at = datetime.now(UTC) + timedelta(days=30)
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=expires_at,
            )

            await revert_to_free(async_session, user.id)

            await async_session.refresh(user)
            assert user.subscription_status == "free"
            assert user.subscription_expires_at is None
            assert user.settings.get("subscription", {}).get("will_renew") is False
            assert "reverted_at" in user.settings.get("subscription", {})
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_raises_user_not_found_for_invalid_id(self, async_session: AsyncSession):
        """Test that UserNotFoundError is raised for non-existent user."""
        await cleanup_users(async_session)
        try:
            with pytest.raises(UserNotFoundError):
                await revert_to_free(async_session, uuid4())
        finally:
            await cleanup_users(async_session)


class TestProcessWebhook:
    """Tests for process_webhook function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_initial_purchase_sets_premium_status(self, async_session: AsyncSession):
        """Test INITIAL_PURCHASE webhook sets subscription_status to premium."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(async_session)
            expires_at = datetime.now(UTC) + timedelta(days=30)

            event = WebhookEvent(
                event=WebhookEventData(
                    type="INITIAL_PURCHASE",
                    app_user_id=str(user.id),
                    entitlements=[
                        WebhookEntitlement(
                            product_identifier="com.example.premium",
                            expires_at=expires_at,
                        )
                    ],
                )
            )

            await process_webhook(async_session, event)

            await async_session.refresh(user)
            assert user.subscription_status == "premium"
            assert user.subscription_expires_at is not None
            assert user.settings.get("subscription", {}).get("will_renew") is True
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_renewal_extends_expires_at(self, async_session: AsyncSession):
        """Test RENEWAL webhook extends subscription expiry date."""
        await cleanup_users(async_session)
        try:
            old_expires = datetime.now(UTC) + timedelta(days=5)
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=old_expires,
            )
            new_expires = datetime.now(UTC) + timedelta(days=35)

            event = WebhookEvent(
                event=WebhookEventData(
                    type="RENEWAL",
                    app_user_id=str(user.id),
                    entitlements=[
                        WebhookEntitlement(
                            product_identifier="com.example.premium",
                            expires_at=new_expires,
                        )
                    ],
                )
            )

            await process_webhook(async_session, event)

            await async_session.refresh(user)
            assert user.subscription_status == "premium"
            assert user.subscription_expires_at == new_expires
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_cancellation_keeps_premium_until_expiry(self, async_session: AsyncSession):
        """Test CANCELLATION webhook keeps premium active until expiry but sets will_renew=False."""
        await cleanup_users(async_session)
        try:
            expires_at = datetime.now(UTC) + timedelta(days=15)
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=expires_at,
            )
            user.settings = {"subscription": {"will_renew": True}}
            await async_session.commit()

            event = WebhookEvent(
                event=WebhookEventData(
                    type="CANCELLATION",
                    app_user_id=str(user.id),
                )
            )

            await process_webhook(async_session, event)

            await async_session.refresh(user)
            # Still premium
            assert user.subscription_status == "premium"
            # But will_renew is False
            assert user.settings.get("subscription", {}).get("will_renew") is False
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_expiration_reverts_to_free(self, async_session: AsyncSession):
        """Test EXPIRATION webhook reverts user to free status."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=datetime.now(UTC) - timedelta(hours=1),
            )

            event = WebhookEvent(
                event=WebhookEventData(
                    type="EXPIRATION",
                    app_user_id=str(user.id),
                )
            )

            await process_webhook(async_session, event)

            await async_session.refresh(user)
            assert user.subscription_status == "free"
            assert user.subscription_expires_at is None
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_billing_issue_logs_problem(self, async_session: AsyncSession):
        """Test BILLING_ISSUE webhook logs billing problem in user settings."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=datetime.now(UTC) + timedelta(days=5),
            )

            event = WebhookEvent(
                event=WebhookEventData(
                    type="BILLING_ISSUE",
                    app_user_id=str(user.id),
                    transaction_id="txn_12345",
                )
            )

            await process_webhook(async_session, event)

            await async_session.refresh(user)
            # Still premium during billing grace period
            assert user.subscription_status == "premium"
            # But billing issue is recorded
            assert user.settings.get("subscription", {}).get("billing_issue") is True
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_product_change_updates_product_id(self, async_session: AsyncSession):
        """Test PRODUCT_CHANGE webhook updates product_id."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(
                async_session,
                subscription_status="premium",
            )
            user.settings = {"subscription": {"product_id": "monthly"}}
            await async_session.commit()

            event = WebhookEvent(
                event=WebhookEventData(
                    type="PRODUCT_CHANGE",
                    app_user_id=str(user.id),
                    product_id="yearly",
                )
            )

            await process_webhook(async_session, event)

            await async_session.refresh(user)
            assert user.settings.get("subscription", {}).get("product_id") == "yearly"
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_uncancellation_restores_will_renew(self, async_session: AsyncSession):
        """Test UNCANCELLATION webhook restores will_renew to True."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(
                async_session,
                subscription_status="premium",
            )
            user.settings = {"subscription": {"will_renew": False}}
            await async_session.commit()

            event = WebhookEvent(
                event=WebhookEventData(
                    type="UNCANCELLATION",
                    app_user_id=str(user.id),
                )
            )

            await process_webhook(async_session, event)

            await async_session.refresh(user)
            assert user.settings.get("subscription", {}).get("will_renew") is True
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_non_renewing_purchase_grants_remove_ads(self, async_session: AsyncSession):
        """Test NON_RENEWING_PURCHASE stores entitlement and product in user settings."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(async_session)

            event = WebhookEvent(
                event=WebhookEventData(
                    type="NON_RENEWING_PURCHASE",
                    app_user_id=str(user.id),
                    transaction_id="txn_remove_ads_1",
                    product_id="fishfeed_remove_ads",
                    entitlements=[
                        WebhookEntitlement(product_identifier="remove_ads")
                    ],
                )
            )

            await process_webhook(async_session, event)

            await async_session.refresh(user)
            non_sub = user.settings.get("non_subscriptions", {})
            assert "fishfeed_remove_ads" in non_sub.get("products", [])
            assert "remove_ads" in non_sub.get("entitlements", [])
            assert non_sub.get("last_transaction_id") == "txn_remove_ads_1"
            # Subscription state must remain untouched.
            assert user.subscription_status == "free"
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_non_renewing_purchase_is_idempotent(self, async_session: AsyncSession):
        """Test NON_RENEWING_PURCHASE deduplicates products on repeated processing."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(async_session)

            event = WebhookEvent(
                event=WebhookEventData(
                    type="NON_RENEWING_PURCHASE",
                    app_user_id=str(user.id),
                    product_id="fishfeed_remove_ads",
                    entitlements=[
                        WebhookEntitlement(product_identifier="remove_ads")
                    ],
                )
            )

            await process_webhook(async_session, event)
            await process_webhook(async_session, event)

            await async_session.refresh(user)
            non_sub = user.settings.get("non_subscriptions", {})
            assert non_sub.get("products", []).count("fishfeed_remove_ads") == 1
            assert non_sub.get("entitlements", []).count("remove_ads") == 1
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_handles_unknown_user_gracefully(self, async_session: AsyncSession):
        """Test webhook processing handles unknown user without raising error."""
        await cleanup_users(async_session)
        try:
            event = WebhookEvent(
                event=WebhookEventData(
                    type="INITIAL_PURCHASE",
                    app_user_id=str(uuid4()),  # Non-existent user
                )
            )

            # Should not raise, just log warning
            await process_webhook(async_session, event)
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_handles_invalid_app_user_id_format(self, async_session: AsyncSession):
        """Test webhook processing handles invalid app_user_id format."""
        await cleanup_users(async_session)
        try:
            event = WebhookEvent(
                event=WebhookEventData(
                    type="INITIAL_PURCHASE",
                    app_user_id="not-a-valid-uuid",
                )
            )

            # Should not raise, just log warning
            await process_webhook(async_session, event)
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_idempotent_initial_purchase(self, async_session: AsyncSession):
        """Test that processing same INITIAL_PURCHASE twice doesn't cause issues."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(async_session)
            expires_at = datetime.now(UTC) + timedelta(days=30)

            event = WebhookEvent(
                event=WebhookEventData(
                    type="INITIAL_PURCHASE",
                    app_user_id=str(user.id),
                    transaction_id="txn_unique_123",
                    entitlements=[
                        WebhookEntitlement(
                            product_identifier="com.example.premium",
                            expires_at=expires_at,
                        )
                    ],
                )
            )

            # Process twice
            await process_webhook(async_session, event)
            await process_webhook(async_session, event)

            await async_session.refresh(user)
            # Should still be premium, not cause any errors
            assert user.subscription_status == "premium"
        finally:
            await cleanup_users(async_session)


class TestRestorePurchases:
    """Tests for restore_purchases function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_raises_not_configured_when_api_key_missing(self, async_session: AsyncSession):
        """Test that restore_purchases raises error when RevenueCat not configured."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(async_session)

            with patch("app.services.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_API_KEY = None

                with pytest.raises(RevenueCatNotConfiguredError):
                    await restore_purchases(
                        db=async_session,
                        user_id=user.id,
                        receipt="fake_receipt",
                        platform="ios",
                    )
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_restores_active_subscription(self, async_session: AsyncSession):
        """Test restoring active subscription from RevenueCat."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(async_session)
            expires_at = datetime.now(UTC) + timedelta(days=30)

            response_data = {
                "subscriber": {
                    "entitlements": {
                        "premium": {
                            "expires_date": expires_at.isoformat(),
                            "product_identifier": "com.example.premium_monthly",
                            "unsubscribe_detected_at": None,
                        }
                    }
                }
            }

            with patch("app.services.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_API_KEY = "test_api_key"

                with patch("httpx.AsyncClient") as mock_client_class:
                    mock_response = AsyncMock()
                    mock_response.status_code = 200
                    mock_response.json = lambda: response_data  # Sync method

                    mock_client = AsyncMock()
                    mock_client.post.return_value = mock_response
                    mock_client.__aenter__.return_value = mock_client
                    mock_client.__aexit__.return_value = None
                    mock_client_class.return_value = mock_client

                    status = await restore_purchases(
                        db=async_session,
                        user_id=user.id,
                        receipt="fake_receipt",
                        platform="ios",
                    )

            assert status.status == "premium"
            await async_session.refresh(user)
            assert user.subscription_status == "premium"
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_free_status_when_no_active_entitlements(self, async_session: AsyncSession):
        """Test restore returns free status when no active entitlements."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(async_session)

            response_data = {
                "subscriber": {
                    "entitlements": {}
                }
            }

            with patch("app.services.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_API_KEY = "test_api_key"

                with patch("httpx.AsyncClient") as mock_client_class:
                    mock_response = AsyncMock()
                    mock_response.status_code = 200
                    mock_response.json = lambda: response_data  # Sync method

                    mock_client = AsyncMock()
                    mock_client.post.return_value = mock_response
                    mock_client.__aenter__.return_value = mock_client
                    mock_client.__aexit__.return_value = None
                    mock_client_class.return_value = mock_client

                    status = await restore_purchases(
                        db=async_session,
                        user_id=user.id,
                        receipt="fake_receipt",
                        platform="ios",
                    )

            assert status.status == "free"
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_raises_user_not_found_for_invalid_id(self, async_session: AsyncSession):
        """Test that UserNotFoundError is raised for non-existent user."""
        await cleanup_users(async_session)
        try:
            with patch("app.services.purchase.get_settings") as mock_settings:
                mock_settings.return_value.REVENUECAT_API_KEY = "test_api_key"

                with pytest.raises(UserNotFoundError):
                    await restore_purchases(
                        db=async_session,
                        user_id=uuid4(),
                        receipt="fake_receipt",
                        platform="ios",
                    )
        finally:
            await cleanup_users(async_session)


class TestPurchaseExceptions:
    """Tests for purchase service exceptions."""

    def test_purchase_error_has_correct_attributes(self):
        """Test PurchaseError exception attributes."""
        error = PurchaseError("Test error", status_code=400)
        assert error.message == "Test error"
        assert error.status_code == 400
        assert str(error) == "Test error"

    def test_user_not_found_error(self):
        """Test UserNotFoundError exception."""
        user_id = uuid4()
        error = UserNotFoundError(user_id)
        assert str(user_id) in error.message
        assert error.status_code == 404

    def test_revenuecat_not_configured_error(self):
        """Test RevenueCatNotConfiguredError exception."""
        error = RevenueCatNotConfiguredError()
        assert "not configured" in error.message.lower()
        assert error.status_code == 500

    def test_revenuecat_api_error(self):
        """Test RevenueCatAPIError exception."""
        error = RevenueCatAPIError("API timeout")
        assert error.message == "API timeout"
        assert error.status_code == 502

    def test_invalid_signature_error(self):
        """Test InvalidSignatureError exception."""
        error = InvalidSignatureError()
        assert error.status_code == 401
        assert "signature" in error.message.lower()

    def test_duplicate_webhook_error(self):
        """Test DuplicateWebhookError exception."""
        error = DuplicateWebhookError("txn_123")
        assert error.status_code == 200
        assert "txn_123" in error.message


class TestVerifyWebhookSignature:
    """Tests for verify_webhook_signature function."""

    def test_valid_signature_returns_true(self):
        """Test that valid HMAC-SHA256 signature returns True."""
        import hashlib
        import hmac

        payload = b'{"test": "payload"}'
        secret = "test_secret_key"
        signature = hmac.new(
            key=secret.encode("utf-8"),
            msg=payload,
            digestmod=hashlib.sha256,
        ).hexdigest()

        result = verify_webhook_signature(payload, signature, secret)
        assert result is True

    def test_invalid_signature_returns_false(self):
        """Test that invalid signature returns False."""
        payload = b'{"test": "payload"}'
        secret = "test_secret_key"
        invalid_signature = "invalid_signature_value"

        result = verify_webhook_signature(payload, invalid_signature, secret)
        assert result is False

    def test_empty_signature_returns_false(self):
        """Test that empty signature returns False."""
        payload = b'{"test": "payload"}'
        secret = "test_secret_key"

        result = verify_webhook_signature(payload, "", secret)
        assert result is False

    def test_empty_secret_returns_false(self):
        """Test that empty secret returns False."""
        payload = b'{"test": "payload"}'
        signature = "some_signature"

        result = verify_webhook_signature(payload, signature, "")
        assert result is False

    def test_none_signature_returns_false(self):
        """Test that None signature returns False."""
        payload = b'{"test": "payload"}'
        secret = "test_secret_key"

        result = verify_webhook_signature(payload, None, secret)  # type: ignore
        assert result is False

    def test_timing_attack_safe(self):
        """Test that signature comparison is timing-safe (uses compare_digest)."""
        import hashlib
        import hmac

        payload = b'{"test": "payload"}'
        secret = "test_secret_key"
        correct_signature = hmac.new(
            key=secret.encode("utf-8"),
            msg=payload,
            digestmod=hashlib.sha256,
        ).hexdigest()

        # This should use compare_digest internally
        result = verify_webhook_signature(payload, correct_signature, secret)
        assert result is True


class TestCheckIdempotency:
    """Tests for check_idempotency function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_first_request_returns_not_duplicate(
        self, async_session: AsyncSession, redis_client
    ):
        """Test that first request for a transaction_id returns not duplicate."""
        await cleanup_users(async_session)
        await redis_client.flushdb()

        try:
            is_duplicate, lock_key = await check_idempotency(
                db=async_session,
                redis=redis_client,
                transaction_id="new_txn_001",
            )

            assert is_duplicate is False
            assert lock_key is not None
            assert "webhook_lock:new_txn_001" == lock_key

            # Clean up lock
            await release_idempotency_lock(redis_client, lock_key)

        finally:
            await cleanup_users(async_session)
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_duplicate_in_database_returns_duplicate(
        self, async_session: AsyncSession, redis_client
    ):
        """Test that existing transaction in database returns duplicate."""
        await cleanup_users(async_session)
        await redis_client.flushdb()

        try:
            # First, log a transaction
            await log_webhook_transaction(
                db=async_session,
                transaction_id="existing_txn_001",
                event_type="INITIAL_PURCHASE",
                user_id="test_user_id",
                payload={"test": "data"},
            )

            # Check idempotency
            is_duplicate, lock_key = await check_idempotency(
                db=async_session,
                redis=redis_client,
                transaction_id="existing_txn_001",
            )

            assert is_duplicate is True
            assert lock_key is None

        finally:
            await async_session.execute(text("TRUNCATE TABLE webhook_transactions CASCADE"))
            await async_session.commit()
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_redis_lock_prevents_concurrent_access(
        self, async_session: AsyncSession, redis_client
    ):
        """Test that Redis lock prevents concurrent processing."""
        await cleanup_users(async_session)
        await redis_client.flushdb()

        try:
            # Acquire lock manually
            lock_key = "webhook_lock:locked_txn_001"
            await redis_client.set(lock_key, "1", nx=True, ex=30)

            # Try to check idempotency - should be blocked by lock
            is_duplicate, returned_lock_key = await check_idempotency(
                db=async_session,
                redis=redis_client,
                transaction_id="locked_txn_001",
            )

            assert is_duplicate is True
            assert returned_lock_key is None

        finally:
            await cleanup_users(async_session)
            await redis_client.flushdb()


class TestLogWebhookTransaction:
    """Tests for log_webhook_transaction function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_logs_successful_transaction(self, async_session: AsyncSession):
        """Test that successful transactions are logged correctly."""
        await async_session.execute(text("TRUNCATE TABLE webhook_transactions CASCADE"))
        await async_session.commit()

        try:
            transaction = await log_webhook_transaction(
                db=async_session,
                transaction_id="log_test_001",
                event_type="INITIAL_PURCHASE",
                user_id="user_123",
                payload={"test": "payload", "nested": {"key": "value"}},
                correlation_id="corr_123",
                processing_result="success",
            )

            assert transaction.transaction_id == "log_test_001"
            assert transaction.event_type == "INITIAL_PURCHASE"
            assert transaction.user_id == "user_123"
            assert transaction.payload == {"test": "payload", "nested": {"key": "value"}}
            assert transaction.correlation_id == "corr_123"
            assert transaction.processing_result == "success"
            assert transaction.error_message is None
            assert transaction.processed_at is not None

        finally:
            await async_session.execute(text("TRUNCATE TABLE webhook_transactions CASCADE"))
            await async_session.commit()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_logs_failed_transaction_with_error(self, async_session: AsyncSession):
        """Test that failed transactions are logged with error message."""
        await async_session.execute(text("TRUNCATE TABLE webhook_transactions CASCADE"))
        await async_session.commit()

        try:
            transaction = await log_webhook_transaction(
                db=async_session,
                transaction_id="log_error_001",
                event_type="INITIAL_PURCHASE",
                user_id="user_123",
                payload={"test": "payload"},
                processing_result="error",
                error_message="User not found in database",
            )

            assert transaction.processing_result == "error"
            assert transaction.error_message == "User not found in database"

        finally:
            await async_session.execute(text("TRUNCATE TABLE webhook_transactions CASCADE"))
            await async_session.commit()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_logs_transaction_with_null_user_id(self, async_session: AsyncSession):
        """Test that transactions can be logged with null user_id."""
        await async_session.execute(text("TRUNCATE TABLE webhook_transactions CASCADE"))
        await async_session.commit()

        try:
            transaction = await log_webhook_transaction(
                db=async_session,
                transaction_id="log_null_user_001",
                event_type="PARSE_ERROR",
                user_id=None,
                payload={"raw": "invalid payload"},
                processing_result="error",
                error_message="Failed to parse JSON",
            )

            assert transaction.user_id is None
            assert transaction.event_type == "PARSE_ERROR"

        finally:
            await async_session.execute(text("TRUNCATE TABLE webhook_transactions CASCADE"))
            await async_session.commit()


class TestReleaseIdempotencyLock:
    """Tests for release_idempotency_lock function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_releases_existing_lock(self, redis_client):
        """Test that existing lock is released."""
        await redis_client.flushdb()

        try:
            lock_key = "webhook_lock:release_test"
            await redis_client.set(lock_key, "1")

            # Verify lock exists
            assert await redis_client.exists(lock_key)

            await release_idempotency_lock(redis_client, lock_key)

            # Verify lock is released
            assert not await redis_client.exists(lock_key)

        finally:
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_handles_none_lock_key(self, redis_client):
        """Test that None lock_key is handled gracefully."""
        await redis_client.flushdb()

        # Should not raise
        await release_idempotency_lock(redis_client, None)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_handles_nonexistent_lock(self, redis_client):
        """Test that releasing non-existent lock doesn't raise."""
        await redis_client.flushdb()

        # Should not raise
        await release_idempotency_lock(redis_client, "nonexistent_lock_key")
