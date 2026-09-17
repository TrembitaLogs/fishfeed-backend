"""API endpoints for RevenueCat webhook processing and subscription management."""

import uuid
from asyncio import CancelledError
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.models.purchase import WebhookTransaction
from app.redis import get_redis
from app.schemas.purchase import (
    RestorePurchaseRequest,
    SubscriptionStatus,
    WebhookEvent,
    WebhookResponse,
)
from app.services.premium import invalidate_premium_cache
from app.services.purchase import (
    PurchaseError,
    RevenueCatAPIError,
    RevenueCatNotConfiguredError,
    WebhookAuditConflict,
    check_idempotency,
    get_subscription_status,
    log_webhook_transaction,
    process_webhook,
    release_idempotency_lock,
    restore_purchases,
    verify_webhook_authorization,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/purchases", tags=["purchases"])


@router.post(
    "/webhook",
    response_model=WebhookResponse,
    responses={
        502: {
            "description": "RevenueCat provider error",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
        503: {
            "description": "Webhook processing is temporarily unavailable",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
    },
)
async def handle_webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    authorization: Annotated[str | None, Header()] = None,
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
    - Validates Authorization header against REVENUECAT_WEBHOOK_SECRET (constant-time compare)
    - Implements idempotency to handle duplicate webhooks
    - Uses Redis lock for race condition protection

    Accepted events return 200; retryable provider and storage failures return 502 or 503.
    Auth failures return 401 so RevenueCat retries (or surfaces) the misconfiguration.
    """
    settings = get_settings()
    correlation_id = str(uuid.uuid4())
    lock_handle: tuple[str, str] | None = None

    if not settings.REVENUECAT_WEBHOOK_SECRET and settings.ENVIRONMENT == "production":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Webhook secret is not configured")
    if settings.REVENUECAT_WEBHOOK_SECRET:
        if not authorization:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization header")
        if not verify_webhook_authorization(authorization, settings.REVENUECAT_WEBHOOK_SECRET):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Authorization header")

    body = await request.body()

    # Parse webhook event
    try:
        event = WebhookEvent.model_validate_json(body)
    except (ValidationError, ValueError) as e:
        # Log invalid payload but return 200 to prevent retries
        try:
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
            await db.commit()
        except (RedisError, SQLAlchemyError):
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Webhook audit is unavailable",
            ) from None
        return WebhookResponse(success=False, message="Invalid webhook payload")

    event_data = event.event
    # Lifecycle events can share a store transaction; only event IDs identify retries.
    transaction_id = event_data.id or event_data.transaction_id or correlation_id

    async def record_error(message: str) -> tuple[WebhookResponse | None, bool]:
        """Persist a retryable failure, or acknowledge a terminal race winner."""
        await db.rollback()
        if lock_handle is None:
            return None, False
        try:
            audit = await log_webhook_transaction(
                db,
                transaction_id,
                event_data.type,
                event_data.app_user_id,
                event.model_dump(mode="json"),
                correlation_id,
                "error",
                message,
            )
            await db.commit()
            if audit.processing_result in {"success", "skipped"}:
                return WebhookResponse(success=True, message="Already processed"), False
            return None, False
        except WebhookAuditConflict:
            await db.rollback()
            return await terminal_audit_response(), True
        except SQLAlchemyError:
            await db.rollback()
            return None, False

    async def terminal_audit_response() -> WebhookResponse | None:
        winner = await db.scalar(select(WebhookTransaction).where(WebhookTransaction.transaction_id == transaction_id))
        if winner and winner.processing_result in {"success", "skipped"}:
            return WebhookResponse(success=True, message="Already processed")
        return None

    try:
        # Check idempotency
        is_duplicate, lock_handle = await check_idempotency(
            db=db,
            redis=redis,
            transaction_id=transaction_id,
            legacy_transaction_id=event_data.transaction_id if event_data.id else None,
        )

        if is_duplicate:
            return WebhookResponse(success=True, message="Already processed")

        audit_disposition, results = await process_webhook(db, event, redis)
        await log_webhook_transaction(
            db=db,
            transaction_id=transaction_id,
            event_type=event_data.type,
            user_id=event_data.app_user_id,
            payload=event.model_dump(mode="json"),
            correlation_id=correlation_id,
            processing_result=audit_disposition,
        )
        await db.commit()
        for result in results:
            await invalidate_premium_cache(str(result.user_id), redis)
        return WebhookResponse(success=True, message="Webhook processed successfully")

    except CancelledError:
        await db.rollback()
        raise
    except WebhookAuditConflict:
        await db.rollback()
        winner = await terminal_audit_response()
        if winner is not None:
            return winner
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Webhook audit is busy") from None
    except PurchaseError as e:
        winner, audit_busy = await record_error(e.message)
        if winner is not None:
            return winner
        if audit_busy:
            error_status = 503
        elif isinstance(e, RevenueCatAPIError):
            error_status = 503 if e.upstream_status == 429 or e.failure_kind != "provider" else 502
        elif isinstance(e, RevenueCatNotConfiguredError) or e.status_code >= 500:
            error_status = 503
        else:
            error_status = e.status_code
        raise HTTPException(status_code=error_status, detail=e.message) from None

    except RedisError as e:
        logger.error("Redis error during webhook processing", error=str(e), correlation_id=correlation_id)
        winner, _ = await record_error(f"Redis error: {e}")
        if winner is not None:
            return winner
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook storage is unavailable",
        ) from None

    except SQLAlchemyError as e:
        logger.error("Database error during webhook processing", error=str(e), correlation_id=correlation_id)
        winner, _ = await record_error(f"Database error: {e}")
        if winner is not None:
            return winner
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook storage is unavailable",
        ) from None

    finally:
        await release_idempotency_lock(redis, lock_handle)


@router.post(
    "/restore",
    response_model=SubscriptionStatus,
    responses={
        400: {"description": "Invalid receipt"},
        502: {
            "description": "RevenueCat provider error",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
        503: {
            "description": "Subscription reconciliation conflict",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
    },
)
async def restore_user_purchases(
    request_data: RestorePurchaseRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
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
        503: Reconciliation conflict.
    """
    # Verify the request is for the current user
    if request_data.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot restore purchases for another user",
        )

    try:
        result = await restore_purchases(
            db=db,
            user_id=current_user.id,
            receipt=request_data.receipt,
            platform=request_data.platform,
            redis=redis,
        )
        await db.commit()
        await invalidate_premium_cache(str(result.user_id), redis)
        return await get_subscription_status(db, current_user.id)
    except PurchaseError as e:
        await db.rollback()
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
