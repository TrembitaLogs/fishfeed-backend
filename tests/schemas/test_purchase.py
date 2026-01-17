"""Tests for purchase Pydantic schemas."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.purchase import (
    RestorePurchaseRequest,
    SubscriptionStatus,
    WebhookEntitlement,
    WebhookEvent,
    WebhookEventData,
    WebhookResponse,
    WebhookTransaction,
)


class TestWebhookEvent:
    """Tests for WebhookEvent schema."""

    def test_valid_initial_purchase_event(self):
        """Test validation of valid INITIAL_PURCHASE webhook payload."""
        payload = {
            "event": {
                "type": "INITIAL_PURCHASE",
                "app_user_id": "user123",
                "original_app_user_id": "user123",
                "transaction_id": "2000000123456789",
                "purchase_id": "PURCHASE_ID",
                "observer_mode": False,
                "created_at": "2023-10-10T10:00:00Z",
                "entitlements": [
                    {
                        "product_identifier": "com.example.premium",
                        "expires_at": "2023-11-10T10:00:00Z",
                        "starts_at": "2023-10-10T10:00:00Z",
                    }
                ],
                "transaction": {
                    "type": "IAP",
                    "original_purchase_date": "2023-10-10T10:00:00Z",
                    "purchase_date": "2023-10-10T10:00:00Z",
                    "original_transaction_id": "2000000123456789",
                    "transaction_id": "2000000123456789",
                    "currency": "USD",
                    "price": 4.99,
                    "product_id": "com.example.premium",
                },
            },
            "web_hook_version": "1.0",
        }

        event = WebhookEvent.model_validate(payload)
        assert event.event.type == "INITIAL_PURCHASE"
        assert event.event.app_user_id == "user123"
        assert event.api_version == "1.0"
        assert len(event.event.entitlements) == 1
        assert event.event.entitlements[0].product_identifier == "com.example.premium"
        assert event.event.transaction is not None
        assert event.event.transaction.price == 4.99

    def test_valid_renewal_event(self):
        """Test validation of valid RENEWAL webhook payload."""
        payload = {
            "event": {
                "type": "RENEWAL",
                "app_user_id": "user456",
                "entitlements": [
                    {
                        "product_identifier": "com.example.premium",
                        "expires_at": "2023-12-10T10:00:00Z",
                    }
                ],
            },
        }

        event = WebhookEvent.model_validate(payload)
        assert event.event.type == "RENEWAL"
        assert event.event.app_user_id == "user456"

    def test_valid_cancellation_event(self):
        """Test validation of valid CANCELLATION webhook payload."""
        payload = {
            "event": {
                "type": "CANCELLATION",
                "app_user_id": "user789",
            },
        }

        event = WebhookEvent.model_validate(payload)
        assert event.event.type == "CANCELLATION"

    def test_valid_expiration_event(self):
        """Test validation of valid EXPIRATION webhook payload."""
        payload = {
            "event": {
                "type": "EXPIRATION",
                "app_user_id": "user_expired",
                "transaction_id": None,
                "transaction": None,
            },
        }

        event = WebhookEvent.model_validate(payload)
        assert event.event.type == "EXPIRATION"
        assert event.event.transaction is None

    def test_valid_billing_issue_event(self):
        """Test validation of BILLING_ISSUE event."""
        payload = {
            "event": {
                "type": "BILLING_ISSUE",
                "app_user_id": "user_billing",
            },
        }

        event = WebhookEvent.model_validate(payload)
        assert event.event.type == "BILLING_ISSUE"

    def test_valid_product_change_event(self):
        """Test validation of PRODUCT_CHANGE event."""
        payload = {
            "event": {
                "type": "PRODUCT_CHANGE",
                "app_user_id": "user_change",
                "product_id": "com.example.premium_yearly",
            },
        }

        event = WebhookEvent.model_validate(payload)
        assert event.event.type == "PRODUCT_CHANGE"

    def test_invalid_event_type_rejected(self):
        """Test that unknown event_type is rejected."""
        payload = {
            "event": {
                "type": "UNKNOWN_EVENT",
                "app_user_id": "user123",
            },
        }

        with pytest.raises(ValidationError) as exc_info:
            WebhookEvent.model_validate(payload)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("event", "type")

    def test_missing_app_user_id_rejected(self):
        """Test that missing app_user_id is rejected."""
        payload = {
            "event": {
                "type": "INITIAL_PURCHASE",
            },
        }

        with pytest.raises(ValidationError) as exc_info:
            WebhookEvent.model_validate(payload)

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("event", "app_user_id") for e in errors)

    def test_optional_fields_with_none(self):
        """Test that optional fields accept None values."""
        payload = {
            "event": {
                "type": "EXPIRATION",
                "app_user_id": "user123",
                "original_app_user_id": None,
                "transaction_id": None,
                "purchase_id": None,
                "transaction": None,
                "device": None,
                "app": None,
                "subscriber": None,
            },
        }

        event = WebhookEvent.model_validate(payload)
        assert event.event.original_app_user_id is None
        assert event.event.transaction_id is None
        assert event.event.transaction is None

    def test_environment_literal_values(self):
        """Test environment field accepts valid literal values."""
        for env in ["SANDBOX", "PRODUCTION"]:
            payload = {
                "event": {
                    "type": "INITIAL_PURCHASE",
                    "app_user_id": "user123",
                    "environment": env,
                },
            }
            event = WebhookEvent.model_validate(payload)
            assert event.event.environment == env

    def test_store_literal_values(self):
        """Test store field accepts valid literal values."""
        for store in ["APP_STORE", "PLAY_STORE", "STRIPE", "AMAZON"]:
            payload = {
                "event": {
                    "type": "INITIAL_PURCHASE",
                    "app_user_id": "user123",
                    "store": store,
                },
            }
            event = WebhookEvent.model_validate(payload)
            assert event.event.store == store


class TestWebhookEventData:
    """Tests for WebhookEventData schema."""

    def test_minimal_valid_event(self):
        """Test minimal valid event data."""
        event_data = WebhookEventData(
            type="INITIAL_PURCHASE",
            app_user_id="user123",
        )
        assert event_data.type == "INITIAL_PURCHASE"
        assert event_data.app_user_id == "user123"
        assert event_data.entitlements == []
        assert event_data.observer_mode is False

    def test_all_event_types(self):
        """Test all supported event types."""
        event_types = [
            "INITIAL_PURCHASE",
            "RENEWAL",
            "CANCELLATION",
            "EXPIRATION",
            "BILLING_ISSUE",
            "PRODUCT_CHANGE",
            "UNCANCELLATION",
            "SUBSCRIBER_ALIAS",
            "TRANSFER",
        ]

        for event_type in event_types:
            event_data = WebhookEventData(
                type=event_type,
                app_user_id="user123",
            )
            assert event_data.type == event_type


class TestWebhookEntitlement:
    """Tests for WebhookEntitlement schema."""

    def test_valid_entitlement(self):
        """Test valid entitlement data."""
        entitlement = WebhookEntitlement(
            product_identifier="com.example.premium",
            expires_at=datetime(2023, 12, 10, 10, 0, 0, tzinfo=UTC),
            starts_at=datetime(2023, 11, 10, 10, 0, 0, tzinfo=UTC),
        )
        assert entitlement.product_identifier == "com.example.premium"
        assert entitlement.expires_at is not None

    def test_optional_fields(self):
        """Test entitlement with only required fields."""
        entitlement = WebhookEntitlement(
            product_identifier="com.example.premium",
        )
        assert entitlement.product_identifier == "com.example.premium"
        assert entitlement.expires_at is None
        assert entitlement.starts_at is None
        assert entitlement.grace_period_expires_at is None


class TestWebhookTransaction:
    """Tests for WebhookTransaction schema."""

    def test_valid_transaction(self):
        """Test valid transaction data."""
        transaction = WebhookTransaction(
            type="IAP",
            original_purchase_date=datetime(2023, 10, 10, 10, 0, 0, tzinfo=UTC),
            purchase_date=datetime(2023, 10, 10, 10, 0, 0, tzinfo=UTC),
            original_transaction_id="2000000123456789",
            transaction_id="2000000123456789",
            currency="USD",
            price=4.99,
            product_id="com.example.premium",
        )
        assert transaction.currency == "USD"
        assert transaction.price == 4.99

    def test_all_optional_fields(self):
        """Test transaction with all fields as None."""
        transaction = WebhookTransaction()
        assert transaction.type is None
        assert transaction.currency is None
        assert transaction.price is None


class TestRestorePurchaseRequest:
    """Tests for RestorePurchaseRequest schema."""

    def test_valid_ios_restore_request(self):
        """Test valid restore request for iOS."""
        user_id = uuid4()
        request = RestorePurchaseRequest(
            user_id=user_id,
            receipt="SGVsbG8gV29ybGQh",
            platform="ios",
        )
        assert request.user_id == user_id
        assert request.receipt == "SGVsbG8gV29ybGQh"
        assert request.platform == "ios"

    def test_valid_android_restore_request(self):
        """Test valid restore request for Android."""
        request = RestorePurchaseRequest(
            user_id=uuid4(),
            receipt="YW5kcm9pZF9yZWNlaXB0",
            platform="android",
        )
        assert request.platform == "android"

    def test_invalid_uuid_rejected(self):
        """Test that invalid UUID is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RestorePurchaseRequest(
                user_id="not-a-uuid",
                receipt="receipt_data",
                platform="ios",
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("user_id",) for e in errors)

    def test_invalid_platform_rejected(self):
        """Test that invalid platform is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RestorePurchaseRequest(
                user_id=uuid4(),
                receipt="receipt_data",
                platform="windows",
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("platform",)

    def test_empty_receipt_rejected(self):
        """Test that empty receipt is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RestorePurchaseRequest(
                user_id=uuid4(),
                receipt="",
                platform="ios",
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("receipt",) for e in errors)


class TestSubscriptionStatus:
    """Tests for SubscriptionStatus schema."""

    def test_free_status(self):
        """Test free subscription status."""
        status = SubscriptionStatus(status="free")
        assert status.status == "free"
        assert status.expires_at is None
        assert status.is_trial is False
        assert status.will_renew is False

    def test_premium_status(self):
        """Test premium subscription status."""
        expires = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)
        status = SubscriptionStatus(
            status="premium",
            expires_at=expires,
            product_id="com.example.premium_monthly",
            is_trial=False,
            will_renew=True,
            original_purchase_date=datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        assert status.status == "premium"
        assert status.expires_at == expires
        assert status.will_renew is True

    def test_expired_status(self):
        """Test expired subscription status."""
        status = SubscriptionStatus(
            status="expired",
            expires_at=datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        assert status.status == "expired"

    def test_cancelled_status(self):
        """Test cancelled subscription status."""
        status = SubscriptionStatus(
            status="cancelled",
            expires_at=datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC),
            will_renew=False,
        )
        assert status.status == "cancelled"
        assert status.will_renew is False

    def test_invalid_status_rejected(self):
        """Test that invalid status is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SubscriptionStatus(status="invalid_status")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("status",)

    def test_serialization_to_json(self):
        """Test SubscriptionStatus serializes correctly to JSON."""
        expires = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)
        original = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

        status = SubscriptionStatus(
            status="premium",
            expires_at=expires,
            product_id="com.example.premium",
            is_trial=True,
            will_renew=True,
            original_purchase_date=original,
        )

        data = status.model_dump()
        assert data["status"] == "premium"
        assert data["expires_at"] == expires
        assert data["product_id"] == "com.example.premium"
        assert data["is_trial"] is True
        assert data["will_renew"] is True
        assert data["original_purchase_date"] == original

    def test_json_serialization(self):
        """Test JSON serialization works correctly."""
        status = SubscriptionStatus(
            status="premium",
            expires_at=datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC),
        )

        json_str = status.model_dump_json()
        assert "premium" in json_str
        assert "2024-12-31" in json_str

    def test_from_attributes(self):
        """Test SubscriptionStatus can be created from ORM-like object."""

        class MockSubscription:
            def __init__(self):
                self.status = "premium"
                self.expires_at = datetime(2024, 12, 31, tzinfo=UTC)
                self.product_id = "com.example.premium"
                self.is_trial = False
                self.will_renew = True
                self.original_purchase_date = None

        mock = MockSubscription()
        status = SubscriptionStatus.model_validate(mock)

        assert status.status == "premium"
        assert status.product_id == "com.example.premium"


class TestWebhookResponse:
    """Tests for WebhookResponse schema."""

    def test_success_response(self):
        """Test successful webhook response."""
        response = WebhookResponse(success=True)
        assert response.success is True
        assert response.message is None

    def test_success_with_message(self):
        """Test successful response with message."""
        response = WebhookResponse(success=True, message="Event processed")
        assert response.success is True
        assert response.message == "Event processed"

    def test_failure_response(self):
        """Test failure webhook response."""
        response = WebhookResponse(success=False, message="Invalid signature")
        assert response.success is False
        assert response.message == "Invalid signature"

    def test_serialization(self):
        """Test WebhookResponse serializes correctly."""
        response = WebhookResponse(success=True, message="OK")
        data = response.model_dump()
        assert data == {"success": True, "message": "OK"}


class TestFullWebhookPayloads:
    """Integration tests with full RevenueCat webhook payloads."""

    def test_complete_initial_purchase_payload(self):
        """Test complete INITIAL_PURCHASE payload from RevenueCat docs."""
        payload = {
            "event": {
                "type": "INITIAL_PURCHASE",
                "app_user_id": "user123",
                "original_app_user_id": "user123",
                "transaction_id": "2000000123456789",
                "purchase_id": "PURCHASE_ID",
                "observer_mode": False,
                "created_at": "2023-10-10T10:00:00Z",
                "entitlements": [
                    {
                        "product_identifier": "com.example.consumable",
                        "expires_at": None,
                        "starts_at": None,
                        "grace_period_expires_at": None,
                    }
                ],
                "transaction": {
                    "type": "IAP",
                    "original_purchase_date": "2023-10-10T10:00:00Z",
                    "purchase_date": "2023-10-10T10:00:00Z",
                    "original_transaction_id": "2000000123456789",
                    "transaction_id": "2000000123456789",
                    "currency": "USD",
                    "price": 1.99,
                    "store_transaction_id": "2000000123456789",
                    "product_id": "com.example.consumable",
                    "offer_code_ref": None,
                    "intro_price": None,
                },
            },
            "web_hook_version": "1.0",
        }

        event = WebhookEvent.model_validate(payload)
        assert event.event.type == "INITIAL_PURCHASE"
        assert event.event.transaction.price == 1.99
        assert event.event.transaction.currency == "USD"

    def test_complete_expiration_payload(self):
        """Test complete EXPIRATION payload where transaction is null."""
        payload = {
            "event": {
                "type": "EXPIRATION",
                "app_user_id": "user123",
                "original_app_user_id": "user123",
                "transaction_id": None,
                "purchase_id": None,
                "observer_mode": False,
                "created_at": "2023-12-10T10:00:00Z",
                "entitlements": [],
                "transaction": None,
            },
            "web_hook_version": "1.0",
        }

        event = WebhookEvent.model_validate(payload)
        assert event.event.type == "EXPIRATION"
        assert event.event.transaction is None
        assert event.event.entitlements == []

    def test_idempotent_event_handling(self):
        """Test that same payload can be validated multiple times (idempotency)."""
        payload = {
            "event": {
                "type": "RENEWAL",
                "app_user_id": "user123",
                "id": "unique_event_id_123",
            },
        }

        event1 = WebhookEvent.model_validate(payload)
        event2 = WebhookEvent.model_validate(payload)

        assert event1.event.id == event2.event.id
        assert event1.event.type == event2.event.type
