"""API endpoints for RevenueCat webhook processing and subscription management."""

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.redis import get_redis
from app.schemas.purchase import (
    RestorePurchaseRequest,
    SubscriptionStatus,
    WebhookEvent,
    WebhookResponse,
)
from app.services.purchase import (
    PurchaseError,
    check_idempotency,
    get_subscription_status,
    log_webhook_transaction,
    process_webhook,
    release_idempotency_lock,
    restore_purchases,
    verify_webhook_signature,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/purchases", tags=["purchases"])


@router.post("/webhook", response_model=WebhookResponse)
async def handle_webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    x_revenuecat_signature: Annotated[str | None, Header()] = None,
) -> WebhookResponse:
    """Process RevenueCat webhook events.

    This endpoint handles subscription lifecycle events from RevenueCat:
    - INITIAL_PURCHASE: New subscription created
    - RENEWAL: Subscription renewed
    - CANCELLATION: Subscription cancelled (still active until expiry)
    - EXPIRATION: Subscription expired
    - BILLING_ISSUE: Payment failed
    - PRODUCT_CHANGE: Subscription plan changed
    - UNCANCELLATION: Cancellation reversed

    Security:
    - Validates X-RevenueCat-Signature header using HMAC-SHA256
    - Implements idempotency to handle duplicate webhooks
    - Uses Redis lock for race condition protection

    Note: Always returns 200 OK to prevent RevenueCat retries.
    Errors are logged but don't cause HTTP error responses.
    """
    settings = get_settings()
    correlation_id = str(uuid.uuid4())
    lock_key: str | None = None

    # Read raw body for signature verification
    body = await request.body()

    # Validate signature if webhook secret is configured
    if settings.REVENUECAT_WEBHOOK_SECRET:
        if not x_revenuecat_signature:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing X-RevenueCat-Signature header",
            )

        if not verify_webhook_signature(
            payload=body,
            signature=x_revenuecat_signature,
            secret=settings.REVENUECAT_WEBHOOK_SECRET,
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook signature",
            )

    # Parse webhook event
    try:
        event = WebhookEvent.model_validate_json(body)
    except (ValidationError, ValueError) as e:
        # Log invalid payload but return 200 to prevent retries
        await log_webhook_transaction(
            db=db,
            transaction_id=f"invalid_{correlation_id}",
            event_type="PARSE_ERROR",
            user_id=None,
            payload={"raw_body": body.decode("utf-8", errors="replace")[:10000]},
            correlation_id=correlation_id,
            processing_result="error",
            error_message=f"Failed to parse webhook: {e}",
        )
        return WebhookResponse(success=False, message="Invalid webhook payload")

    event_data = event.event
    transaction_id = event_data.transaction_id or event_data.id or correlation_id

    try:
        # Check idempotency
        is_duplicate, lock_key = await check_idempotency(
            db=db,
            redis=redis,
            transaction_id=transaction_id,
        )

        if is_duplicate:
            return WebhookResponse(success=True, message="Already processed")

        # Process the webhook
        await process_webhook(db, event)

        # Log successful transaction
        await log_webhook_transaction(
            db=db,
            transaction_id=transaction_id,
            event_type=event_data.type,
            user_id=event_data.app_user_id,
            payload=event.model_dump(mode="json"),
            correlation_id=correlation_id,
            processing_result="success",
        )

        return WebhookResponse(success=True, message="Webhook processed successfully")

    except PurchaseError as e:
        # Log purchase-related errors
        await log_webhook_transaction(
            db=db,
            transaction_id=transaction_id,
            event_type=event_data.type,
            user_id=event_data.app_user_id,
            payload=event.model_dump(mode="json"),
            correlation_id=correlation_id,
            processing_result="error",
            error_message=e.message,
        )
        # Still return 200 to prevent retries
        return WebhookResponse(success=False, message=e.message)

    except RedisError as e:
        logger.error("Redis error during webhook processing", error=str(e), correlation_id=correlation_id)
        await log_webhook_transaction(
            db=db,
            transaction_id=transaction_id,
            event_type=event_data.type,
            user_id=event_data.app_user_id,
            payload=event.model_dump(mode="json"),
            correlation_id=correlation_id,
            processing_result="error",
            error_message=f"Redis error: {e}",
        )
        return WebhookResponse(success=False, message="Internal processing error")

    except SQLAlchemyError as e:
        logger.error("Database error during webhook processing", error=str(e), correlation_id=correlation_id)
        return WebhookResponse(success=False, message="Internal processing error")

    finally:
        # Always release the lock
        await release_idempotency_lock(redis, lock_key)


@router.post("/restore", response_model=SubscriptionStatus)
async def restore_user_purchases(
    request_data: RestorePurchaseRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> SubscriptionStatus:
    """Restore purchases from app store receipt.

    Used for cross-device subscription restoration when a user
    signs in on a new device.

    Args:
        request_data: Contains receipt data and platform info.
        db: Database session.
        current_user: Authenticated user.

    Returns:
        Current subscription status after restore attempt.

    Raises:
        400: Invalid receipt data.
        401: User not authenticated.
        500: RevenueCat not configured.
        502: RevenueCat API error.
    """
    # Verify the request is for the current user
    if request_data.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot restore purchases for another user",
        )

    try:
        return await restore_purchases(
            db=db,
            user_id=current_user.id,
            receipt=request_data.receipt,
            platform=request_data.platform,
        )
    except PurchaseError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=e.message,
        ) from None


@router.get("/subscription", response_model=SubscriptionStatus)
async def get_user_subscription(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> SubscriptionStatus:
    """Get current user's subscription status.

    Returns the current subscription status including:
    - status: free, premium, expired, or cancelled
    - expires_at: When the subscription expires (if applicable)
    - product_id: The purchased product identifier
    - will_renew: Whether auto-renewal is enabled

    Args:
        db: Database session.
        current_user: Authenticated user.

    Returns:
        Current subscription status.
    """
    try:
        return await get_subscription_status(db, current_user.id)
    except PurchaseError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=e.message,
        ) from None
