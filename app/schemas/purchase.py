"""Pydantic schemas for RevenueCat purchase and subscription endpoints."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Type aliases for better readability
WebhookEventType = Literal[
    "INITIAL_PURCHASE",
    "RENEWAL",
    "CANCELLATION",
    "EXPIRATION",
    "BILLING_ISSUE",
    "PRODUCT_CHANGE",
    "UNCANCELLATION",
    "SUBSCRIBER_ALIAS",
    "TRANSFER",
    "NON_RENEWING_PURCHASE",
]

StoreType = Literal["APP_STORE", "PLAY_STORE", "STRIPE", "AMAZON"]
EnvironmentType = Literal["SANDBOX", "PRODUCTION"]
PlatformType = Literal["ios", "android"]
SubscriptionStatusType = Literal["free", "premium", "expired", "cancelled"]


# RevenueCat Webhook nested schemas
class WebhookEntitlement(BaseModel):
    """Schema for entitlement data in RevenueCat webhook."""

    product_identifier: str
    expires_at: datetime | None = None
    starts_at: datetime | None = None
    grace_period_expires_at: datetime | None = None


class WebhookTransaction(BaseModel):
    """Schema for transaction data in RevenueCat webhook."""

    type: str | None = None
    original_purchase_date: datetime | None = None
    purchase_date: datetime | None = None
    original_transaction_id: str | None = None
    transaction_id: str | None = None
    currency: str | None = None
    price: float | None = None
    store_transaction_id: str | None = None
    product_id: str | None = None
    offer_code_ref: str | None = None
    intro_price: float | None = None


class WebhookDevice(BaseModel):
    """Schema for device info in RevenueCat webhook."""

    platform: str | None = None
    version: str | None = None
    os_version: str | None = None


class WebhookApp(BaseModel):
    """Schema for app info in RevenueCat webhook."""

    version: str | None = None
    build: str | None = None


class WebhookSubscriber(BaseModel):
    """Schema for subscriber data in RevenueCat webhook."""

    first_seen: datetime | None = None
    original_application_version: str | None = None
    original_app_user_id: str | None = None
    last_seen: datetime | None = None
    management_url: str | None = None
    original_purchase_date: datetime | None = None
    non_subscriptions: dict | None = None
    entitlements: dict | None = None
    entitlement_ids: list[str] | None = None


class WebhookEventData(BaseModel):
    """Schema for the event object inside RevenueCat webhook payload."""

    type: WebhookEventType
    app_user_id: str
    original_app_user_id: str | None = None
    transaction_id: str | None = None
    purchase_id: str | None = None
    observer_mode: bool = False
    created_at: datetime | None = None
    entitlements: list[WebhookEntitlement] = Field(default_factory=list)
    transaction: WebhookTransaction | None = None
    device: WebhookDevice | None = None
    app: WebhookApp | None = None
    subscriber: WebhookSubscriber | None = None
    id: str | None = None
    product_id: str | None = None
    environment: EnvironmentType | None = None
    store: StoreType | None = None


class WebhookEvent(BaseModel):
    """Schema for RevenueCat webhook payload.

    RevenueCat sends webhook events with a nested structure containing
    an 'event' object with all the event details.
    """

    event: WebhookEventData
    api_version: str | None = Field(default=None, alias="web_hook_version")


class RestorePurchaseRequest(BaseModel):
    """Request schema for restoring purchases from app stores."""

    user_id: UUID
    receipt: str = Field(min_length=1, description="Base64 encoded receipt data")
    platform: PlatformType


class SubscriptionStatus(BaseModel):
    """Response schema for user subscription status."""

    model_config = ConfigDict(from_attributes=True)

    status: SubscriptionStatusType
    expires_at: datetime | None = None
    product_id: str | None = None
    is_trial: bool = False
    will_renew: bool = False
    original_purchase_date: datetime | None = None


class WebhookResponse(BaseModel):
    """Response schema for webhook processing confirmation."""

    success: bool
    message: str | None = None


class UserLimits(BaseModel):
    """User feature limits based on subscription tier."""

    model_config = ConfigDict(from_attributes=True)

    # AI scan limits
    ai_scans_per_month: int = Field(
        description="Monthly AI scan limit (-1 for unlimited)"
    )

    # Aquarium limits
    max_aquariums: int = Field(description="Maximum number of aquariums")
    max_fish_per_aquarium: int = Field(description="Maximum fish per aquarium")

    # Subscription info
    is_premium: bool = Field(default=False, description="Whether user has premium")


# Default limits for each tier
FREE_USER_LIMITS = UserLimits(
    ai_scans_per_month=3,
    max_aquariums=2,
    max_fish_per_aquarium=10,
    is_premium=False,
)

PREMIUM_USER_LIMITS = UserLimits(
    ai_scans_per_month=-1,  # Unlimited
    max_aquariums=20,
    max_fish_per_aquarium=100,
    is_premium=True,
)
