"""Purchase service for RevenueCat webhook processing and subscription management."""

import hmac
from datetime import UTC, datetime
from uuid import UUID

import httpx
import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.purchase import WebhookTransaction
from app.models.user import User
from app.schemas.purchase import (
    PREMIUM_USER_LIMITS,
    SubscriptionStatus,
    WebhookEvent,
)

logger = structlog.get_logger(__name__)


class PurchaseError(Exception):
    """Base exception for purchase-related errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class UserNotFoundError(PurchaseError):
    """Raised when user is not found."""

    def __init__(self, user_id: str | UUID):
        super().__init__(f"User not found: {user_id}", status_code=404)


class InvalidReceiptError(PurchaseError):
    """Raised when receipt validation fails."""

    def __init__(self, message: str = "Invalid or expired receipt"):
        super().__init__(message, status_code=400)


class RevenueCatAPIError(PurchaseError):
    """Raised when RevenueCat API call fails."""

    def __init__(self, message: str = "RevenueCat API error"):
        super().__init__(message, status_code=502)


class RevenueCatNotConfiguredError(PurchaseError):
    """Raised when RevenueCat is not configured."""

    def __init__(self) -> None:
        super().__init__("RevenueCat API key not configured", status_code=500)


class InvalidSignatureError(PurchaseError):
    """Raised when webhook signature validation fails."""

    def __init__(self, message: str = "Invalid webhook signature"):
        super().__init__(message, status_code=401)


class DuplicateWebhookError(PurchaseError):
    """Raised when a duplicate webhook is detected (for internal use)."""

    def __init__(self, transaction_id: str):
        super().__init__(f"Duplicate webhook: {transaction_id}", status_code=200)


async def _get_user_by_id(db: AsyncSession, user_id: UUID) -> User:
    """Get user by ID or raise UserNotFoundError."""
    stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise UserNotFoundError(user_id)

    return user


async def _get_user_by_app_user_id(db: AsyncSession, app_user_id: str) -> User | None:
    """Get user by RevenueCat app_user_id (which is our user UUID as string)."""
    try:
        user_id = UUID(app_user_id)
        stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    except ValueError:
        logger.warning("Invalid app_user_id format", app_user_id=app_user_id)
        return None


def _get_subscription_settings(user: User) -> dict:
    """Get subscription-related settings from user settings JSON.

    Returns a copy to allow safe modification without affecting the original.
    """
    subscription = user.settings.get("subscription", {})
    # Return a copy to avoid in-place mutations that SQLAlchemy won't detect
    return dict(subscription)


def _set_subscription_settings(user: User, subscription_data: dict) -> None:
    """Update subscription-related settings in user settings JSON."""
    settings = dict(user.settings)
    settings["subscription"] = subscription_data
    user.settings = settings


def verify_webhook_authorization(authorization: str, secret: str) -> bool:
    """Verify RevenueCat webhook Authorization header against configured secret.

    RevenueCat sends the Authorization header value verbatim (the exact string
    configured in the webhook integration's "Authorization header value" field),
    so the comparison is a constant-time string equality check, not HMAC.
    """
    if not authorization or not secret:
        return False

    return hmac.compare_digest(authorization, secret)


async def check_idempotency(
    db: AsyncSession,
    redis: Redis,
    transaction_id: str,
    lock_timeout: int = 30,
) -> tuple[bool, str | None]:
    """Check if webhook transaction has already been processed.

    Uses Redis lock for race condition protection and database for persistence.

    Args:
        db: Database session.
        redis: Redis client.
        transaction_id: Unique transaction ID from webhook.
        lock_timeout: Lock expiry in seconds.

    Returns:
        Tuple of (is_duplicate, lock_key). If is_duplicate is True, the webhook
        should not be processed. lock_key is returned for cleanup after processing.
    """
    lock_key = f"webhook_lock:{transaction_id}"

    # Try to acquire Redis lock
    lock_acquired = await redis.set(lock_key, "1", nx=True, ex=lock_timeout)

    if not lock_acquired:
        # Another process is handling this transaction
        logger.info("Webhook is being processed by another worker", transaction_id=transaction_id)
        return True, None

    # Check if transaction exists in database
    stmt = select(WebhookTransaction).where(
        WebhookTransaction.transaction_id == transaction_id
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        # Already processed, release lock
        await redis.delete(lock_key)
        logger.info("Webhook already processed", transaction_id=transaction_id, processed_at=existing.processed_at)
        return True, None

    return False, lock_key


async def release_idempotency_lock(redis: Redis, lock_key: str | None) -> None:
    """Release the idempotency lock after processing.

    Args:
        redis: Redis client.
        lock_key: Lock key to release (can be None if lock wasn't acquired).
    """
    if lock_key:
        await redis.delete(lock_key)


async def log_webhook_transaction(
    db: AsyncSession,
    transaction_id: str,
    event_type: str,
    user_id: str | None,
    payload: dict,
    correlation_id: str | None = None,
    processing_result: str = "success",
    error_message: str | None = None,
) -> WebhookTransaction:
    """Log webhook transaction to database for audit trail.

    Args:
        db: Database session.
        transaction_id: Unique transaction ID from webhook.
        event_type: Type of webhook event (e.g., INITIAL_PURCHASE).
        user_id: App user ID from webhook (may be None).
        payload: Raw webhook payload as dict.
        correlation_id: Optional correlation ID for request tracing.
        processing_result: Result of processing (success, error, skipped).
        error_message: Error message if processing failed.

    Returns:
        Created WebhookTransaction record.
    """
    transaction = WebhookTransaction(
        transaction_id=transaction_id,
        event_type=event_type,
        user_id=user_id,
        payload=payload,
        correlation_id=correlation_id,
        processing_result=processing_result,
        error_message=error_message,
    )
    db.add(transaction)
    await db.flush()

    log_kwargs = dict(
        transaction_id=transaction_id,
        event_type=event_type,
        user_id=user_id,
        result=processing_result,
        correlation_id=correlation_id,
    )
    if processing_result == "error":
        logger.error("Webhook logged", **log_kwargs)
    else:
        logger.info("Webhook logged", **log_kwargs)

    return transaction


async def _clear_downgrade_info(db: AsyncSession, user_id: UUID) -> None:
    """Clear downgrade-related info from user settings when upgrading.

    Args:
        db: Database session.
        user_id: User UUID.
    """
    stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        return

    settings_dict = dict(user.settings)
    changed = False

    if "limits_exceeded" in settings_dict:
        settings_dict.pop("limits_exceeded")
        changed = True

    if "downgraded_at" in settings_dict:
        settings_dict.pop("downgraded_at")
        changed = True

    if changed:
        user.settings = settings_dict
        # Reset AI scans to premium (unlimited represented as high number)
        user.free_ai_scans_remaining = PREMIUM_USER_LIMITS.ai_scans_per_month
        await db.flush()
        logger.info("Cleared downgrade info for user", user_id=user_id)


async def _grant_non_subscription_entitlement(
    db: AsyncSession,
    user: User,
    product_id: str | None,
    entitlement_ids: list[str],
    transaction_id: str | None,
) -> None:
    """Grant a permanent (non-subscription) entitlement to the user.

    Used for one-time purchases such as fishfeed_remove_ads. The entitlement
    is stored in user.settings.non_subscriptions as a deduplicated list of
    product IDs and as a flat list of entitlement IDs for fast lookup.
    """
    settings_dict = dict(user.settings)
    non_sub = dict(settings_dict.get("non_subscriptions", {}))

    products: list[str] = list(non_sub.get("products", []))
    if product_id and product_id not in products:
        products.append(product_id)
    non_sub["products"] = products

    entitlements: list[str] = list(non_sub.get("entitlements", []))
    for ent_id in entitlement_ids:
        if ent_id and ent_id not in entitlements:
            entitlements.append(ent_id)
    non_sub["entitlements"] = entitlements

    non_sub["updated_at"] = datetime.now(UTC).isoformat()
    if transaction_id:
        non_sub["last_transaction_id"] = transaction_id

    settings_dict["non_subscriptions"] = non_sub
    user.settings = settings_dict
    await db.flush()

    logger.info(
        "Granted non-subscription entitlement",
        user_id=user.id,
        product_id=product_id,
        entitlements=entitlement_ids,
        transaction_id=transaction_id,
    )


async def process_webhook(db: AsyncSession, event: WebhookEvent) -> None:
    """Process RevenueCat webhook event.

    Handles subscription lifecycle events:
    - INITIAL_PURCHASE: Set user to premium status
    - RENEWAL: Extend subscription expiry
    - CANCELLATION: Mark will_renew=False, keep premium until expiry
    - EXPIRATION: Revert user to free tier
    - BILLING_ISSUE: Log billing problem
    - PRODUCT_CHANGE: Update product_id
    - UNCANCELLATION: Restore will_renew=True
    - NON_RENEWING_PURCHASE: Grant permanent non-subscription entitlement

    Args:
        db: Database session.
        event: Validated webhook event from RevenueCat.
    """
    event_data = event.event
    event_type = event_data.type
    app_user_id = event_data.app_user_id

    logger.info(
        "Processing webhook event",
        type=event_type,
        app_user_id=app_user_id,
        transaction_id=event_data.transaction_id,
    )

    user = await _get_user_by_app_user_id(db, app_user_id)
    if user is None:
        logger.warning("User not found for app_user_id", app_user_id=app_user_id)
        return

    # Extract entitlement info
    expires_at: datetime | None = None
    product_id: str | None = None

    if event_data.entitlements:
        entitlement = event_data.entitlements[0]
        expires_at = entitlement.expires_at
        product_id = entitlement.product_identifier
    elif event_data.transaction:
        product_id = event_data.transaction.product_id

    if event_type == "INITIAL_PURCHASE":
        await update_subscription_status(
            db=db,
            user_id=user.id,
            status="premium",
            expires_at=expires_at,
            product_id=product_id,
            will_renew=True,
        )
        # Clear any limits exceeded from previous downgrade
        await _clear_downgrade_info(db, user.id)
        logger.info("User upgraded to premium via INITIAL_PURCHASE", user_id=user.id)

    elif event_type == "RENEWAL":
        await update_subscription_status(
            db=db,
            user_id=user.id,
            status="premium",
            expires_at=expires_at,
            product_id=product_id,
            will_renew=True,
        )
        # Clear any limits exceeded from previous downgrade (in case user resubscribed)
        await _clear_downgrade_info(db, user.id)
        logger.info("User subscription renewed", user_id=user.id, expires_at=expires_at)

    elif event_type == "CANCELLATION":
        # User cancelled but still has access until expires_at
        subscription_settings = _get_subscription_settings(user)
        subscription_settings["will_renew"] = False
        _set_subscription_settings(user, subscription_settings)
        await db.flush()
        logger.info("User cancelled subscription", user_id=user.id, active_until=user.subscription_expires_at)

    elif event_type == "EXPIRATION":
        await revert_to_free(db, user.id)
        logger.info("User subscription expired, reverted to free", user_id=user.id)

    elif event_type == "BILLING_ISSUE":
        logger.warning("Billing issue for user", user_id=user.id, transaction_id=event_data.transaction_id)
        subscription_settings = _get_subscription_settings(user)
        subscription_settings["billing_issue"] = True
        subscription_settings["billing_issue_at"] = datetime.now(UTC).isoformat()
        _set_subscription_settings(user, subscription_settings)
        await db.flush()

    elif event_type == "PRODUCT_CHANGE":
        subscription_settings = _get_subscription_settings(user)
        subscription_settings["product_id"] = product_id or event_data.product_id
        _set_subscription_settings(user, subscription_settings)
        await db.flush()
        logger.info("User changed product", user_id=user.id, product_id=product_id or event_data.product_id)

    elif event_type == "UNCANCELLATION":
        subscription_settings = _get_subscription_settings(user)
        subscription_settings["will_renew"] = True
        _set_subscription_settings(user, subscription_settings)
        await db.flush()
        logger.info("User uncancelled subscription", user_id=user.id)

    elif event_type == "SUBSCRIBER_ALIAS":
        # User alias event - typically for anonymous to identified user transitions
        logger.info("Subscriber alias event", app_user_id=app_user_id)

    elif event_type == "TRANSFER":
        # Transfer event - subscription transferred between users
        logger.info("Transfer event", app_user_id=app_user_id)

    elif event_type == "NON_RENEWING_PURCHASE":
        # One-time non-subscription purchase (e.g. fishfeed_remove_ads).
        # Grants a permanent entitlement with no expiry.
        # For non-subscriptions we want the store SKU (transaction.product_id /
        # event-level product_id), not entitlement.product_identifier — those
        # describe the granted entitlement, not the purchased product.
        nonsub_product_id = (
            (event_data.transaction.product_id if event_data.transaction else None)
            or event_data.product_id
            or product_id
        )
        await _grant_non_subscription_entitlement(
            db=db,
            user=user,
            product_id=nonsub_product_id,
            entitlement_ids=[e.product_identifier for e in event_data.entitlements],
            transaction_id=event_data.transaction_id,
        )

    else:
        logger.warning("Unhandled webhook event type", event_type=event_type)


async def update_subscription_status(
    db: AsyncSession,
    user_id: UUID,
    status: str,
    expires_at: datetime | None = None,
    product_id: str | None = None,
    will_renew: bool = False,
) -> None:
    """Update user subscription status.

    Args:
        db: Database session.
        user_id: User UUID.
        status: New subscription status (free, premium, expired, cancelled).
        expires_at: Subscription expiry datetime.
        product_id: Product identifier from app store.
        will_renew: Whether subscription will auto-renew.
    """
    user = await _get_user_by_id(db, user_id)

    user.subscription_status = status
    user.subscription_expires_at = expires_at

    # Store additional subscription data in settings JSON
    subscription_settings = _get_subscription_settings(user)
    if product_id:
        subscription_settings["product_id"] = product_id
    subscription_settings["will_renew"] = will_renew
    subscription_settings["updated_at"] = datetime.now(UTC).isoformat()
    _set_subscription_settings(user, subscription_settings)

    await db.flush()

    logger.info(
        "Updated subscription for user",
        user_id=user_id,
        status=status,
        expires_at=expires_at,
        product_id=product_id,
    )


async def restore_purchases(
    db: AsyncSession,
    user_id: UUID,
    receipt: str,
    platform: str,
) -> SubscriptionStatus:
    """Restore purchases from app store receipt.

    Validates the receipt with RevenueCat API and updates user subscription
    status if active entitlements are found.

    Args:
        db: Database session.
        user_id: User UUID.
        receipt: Base64 encoded receipt data from app store.
        platform: Platform identifier (ios or android).

    Returns:
        Current SubscriptionStatus after restore.

    Raises:
        RevenueCatNotConfiguredError: If RevenueCat API key is not set.
        RevenueCatAPIError: If API call fails.
        InvalidReceiptError: If receipt is invalid.
    """
    settings = get_settings()

    if not settings.REVENUECAT_API_KEY:
        raise RevenueCatNotConfiguredError()

    # Verify user exists before making API call
    await _get_user_by_id(db, user_id)

    # Call RevenueCat API to validate receipt and get subscriber info
    headers = {
        "Authorization": f"Bearer {settings.REVENUECAT_API_KEY}",
        "Content-Type": "application/json",
        "X-Platform": platform,
    }

    # RevenueCat POST receipts endpoint
    api_url = "https://api.revenuecat.com/v1/receipts"
    payload = {
        "app_user_id": str(user_id),
        "fetch_token": receipt,
        "product_id": "",  # RevenueCat will determine from receipt
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(api_url, json=payload, headers=headers)

            if response.status_code == 200:
                data = response.json()
                subscriber = data.get("subscriber", {})
                entitlements = subscriber.get("entitlements", {})

                # Check for active premium entitlement
                premium_entitlement = entitlements.get("premium", {})
                if premium_entitlement and premium_entitlement.get("expires_date"):
                    expires_str = premium_entitlement["expires_date"]
                    expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))

                    if expires_at > datetime.now(UTC):
                        product_id = premium_entitlement.get("product_identifier")
                        await update_subscription_status(
                            db=db,
                            user_id=user_id,
                            status="premium",
                            expires_at=expires_at,
                            product_id=product_id,
                            will_renew=not premium_entitlement.get("unsubscribe_detected_at"),
                        )
                        logger.info("Restored premium subscription for user", user_id=user_id)

            elif response.status_code == 400:
                raise InvalidReceiptError("Receipt validation failed")
            elif response.status_code == 401:
                logger.error("RevenueCat API authentication failed")
                raise RevenueCatAPIError("API authentication failed")
            else:
                logger.error("RevenueCat API error", status_code=response.status_code, response_text=response.text)
                raise RevenueCatAPIError(f"API returned status {response.status_code}")

    except httpx.TimeoutException:
        logger.error("RevenueCat API timeout")
        raise RevenueCatAPIError("API request timed out") from None
    except httpx.RequestError as e:
        logger.error("RevenueCat API request error", error=str(e))
        raise RevenueCatAPIError(f"API request failed: {e}") from None

    return await get_subscription_status(db, user_id)


async def get_subscription_status(db: AsyncSession, user_id: UUID) -> SubscriptionStatus:
    """Get current subscription status for user.

    Checks if subscription has expired and returns accurate status.

    Args:
        db: Database session.
        user_id: User UUID.

    Returns:
        Current SubscriptionStatus.

    Raises:
        UserNotFoundError: If user is not found.
    """
    user = await _get_user_by_id(db, user_id)
    subscription_settings = _get_subscription_settings(user)

    status = user.subscription_status
    expires_at = user.subscription_expires_at

    # Check if premium subscription has expired
    if status == "premium" and expires_at:
        if expires_at < datetime.now(UTC):
            # Subscription expired - update status
            await revert_to_free(db, user_id)
            return SubscriptionStatus(
                status="expired",
                expires_at=expires_at,
                product_id=subscription_settings.get("product_id"),
                is_trial=False,
                will_renew=False,
            )

    return SubscriptionStatus(
        status=status,  # type: ignore[arg-type]
        expires_at=expires_at,
        product_id=subscription_settings.get("product_id"),
        is_trial=subscription_settings.get("is_trial", False),
        will_renew=subscription_settings.get("will_renew", False),
        original_purchase_date=None,
    )


async def revert_to_free(db: AsyncSession, user_id: UUID) -> None:
    """Revert user to free tier.

    Clears subscription status and related settings.

    Args:
        db: Database session.
        user_id: User UUID.

    Raises:
        UserNotFoundError: If user is not found.
    """
    user = await _get_user_by_id(db, user_id)

    user.subscription_status = "free"
    user.subscription_expires_at = None

    # Clear subscription settings but keep history
    subscription_settings = _get_subscription_settings(user)
    subscription_settings["will_renew"] = False
    subscription_settings["reverted_at"] = datetime.now(UTC).isoformat()
    subscription_settings.pop("billing_issue", None)
    _set_subscription_settings(user, subscription_settings)

    await db.flush()

    logger.info("User reverted to free tier", user_id=user_id)
