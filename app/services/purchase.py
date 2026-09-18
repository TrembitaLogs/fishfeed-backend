"""Purchase service for RevenueCat webhook processing and subscription management."""

import hmac
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import ceil
from typing import Literal
from urllib.parse import quote
from uuid import UUID

import httpx
import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


class WebhookRetryableError(PurchaseError):
    """Raised when a webhook can safely retry after local state recovers."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=503)


class WebhookAuditConflict(Exception):
    """A concurrent writer won the webhook transaction unique key."""


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

    def __init__(
        self,
        message: str = "RevenueCat API error",
        *,
        upstream_status: int | None = None,
        retry_after_seconds: int | None = None,
        failure_kind: Literal["provider", "storage", "configuration"] = "provider",
    ) -> None:
        super().__init__(message, status_code=502)
        self.upstream_status = upstream_status
        self.retry_after_seconds = retry_after_seconds
        self.failure_kind = failure_kind


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


@dataclass(frozen=True)
class SubscriptionSnapshot:
    """Validated RevenueCat customer access evidence."""

    status: Literal["free", "premium"]
    expires_at: datetime | None
    product_id: str | None
    will_renew: bool
    is_trial: bool
    remove_ads_product_id: str | None = None


@dataclass(frozen=True)
class Reconciliation:
    """An immutable read proposal for a later transactional apply."""

    user_id: UUID
    before_status: str
    before_expires_at: datetime | None
    before_verified_at: datetime | None
    before_subscription: dict[str, object]
    snapshot: SubscriptionSnapshot
    verified_at: datetime
    outcome: Literal["changed", "unchanged", "conflict"]
    before_remove_ads: bool = False


_RECONCILIATION_COOLDOWN_KEY = "revenuecat:reconcile:cooldown"
_COOLDOWN_SCRIPT = """
local requested = tonumber(ARGV[1])
local current = redis.call('TTL', KEYS[1])
if current < requested then
  redis.call('SET', KEYS[1], '1', 'EX', requested)
end
return redis.call('TTL', KEYS[1])
"""
_RECONCILIATION_ENVIRONMENTS = {"production": True, "development": False}


def _as_dict(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RevenueCatAPIError(f"RevenueCat response has invalid {field}")
    return value


def _required_string(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise RevenueCatAPIError(f"RevenueCat response has invalid {field}")
    return value


def _datetime_value(record: dict[str, object], field: str, *, required: bool = False) -> datetime | None:
    if field not in record:
        if required:
            raise RevenueCatAPIError(f"RevenueCat response is missing {field}")
        return None
    value = record[field]
    if value is None:
        return None
    if not isinstance(value, str):
        raise RevenueCatAPIError(f"RevenueCat response has invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RevenueCatAPIError(f"RevenueCat response has invalid {field}") from error
    if parsed.tzinfo is None:
        raise RevenueCatAPIError(f"RevenueCat response has timezone-naive {field}")
    return parsed.astimezone(UTC)


def _effective_expiry(expires_at: datetime | None, grace_at: datetime | None) -> datetime | None:
    if expires_at is None:
        return None
    return max(expires_at, grace_at) if grace_at is not None else expires_at


def _free_snapshot(remove_ads_product_id: str | None = None) -> SubscriptionSnapshot:
    return SubscriptionSnapshot(
        status="free",
        expires_at=None,
        product_id=None,
        will_renew=False,
        is_trial=False,
        remove_ads_product_id=remove_ads_product_id,
    )


def _parse_candidate(
    source: dict[str, object],
    *,
    product_id: str,
    purchase_at: datetime,
    entitlement_expires_at: datetime | None,
    entitlement_grace_at: datetime | None,
    is_subscription: bool,
) -> tuple[bool, datetime | None, bool, bool, bool] | None:
    if "is_sandbox" not in source or not isinstance(source["is_sandbox"], bool):
        raise RevenueCatAPIError("RevenueCat response has invalid is_sandbox")
    _required_string(source, "store")
    period_type = _required_string(source, "period_type") if is_subscription else source.get("period_type", "normal")
    if not isinstance(period_type, str):
        raise RevenueCatAPIError("RevenueCat response has invalid period_type")
    if "product_identifier" in source and source["product_identifier"] != product_id:
        raise RevenueCatAPIError("RevenueCat response has inconsistent product identity")
    if _datetime_value(source, "purchase_date", required=True) != purchase_at:
        return None

    source_expires_at = _datetime_value(source, "expires_date", required=is_subscription)
    source_grace_at = _datetime_value(source, "grace_period_expires_date")
    if "expires_date" in source and source_expires_at != entitlement_expires_at:
        raise RevenueCatAPIError("RevenueCat response has inconsistent expiry")
    if source_grace_at != entitlement_grace_at and (
        "grace_period_expires_date" in source or entitlement_grace_at is not None
    ):
        raise RevenueCatAPIError("RevenueCat response has inconsistent grace expiry")

    store = _required_string(source, "store").lower()
    promotional = store == "promotional" or period_type == "promotional"
    expires_at = source_expires_at if source_expires_at is not None else entitlement_expires_at
    if expires_at is None and is_subscription and not promotional:
        raise RevenueCatAPIError("RevenueCat subscription is missing an expiration timestamp")

    refunded_at = _datetime_value(source, "refunded_at")
    revoked_at = _datetime_value(source, "revoked_at")
    if refunded_at is not None or revoked_at is not None:
        return bool(source["is_sandbox"]), None, False, False, True
    will_renew = is_subscription and not promotional and _datetime_value(source, "unsubscribe_detected_at") is None
    return (
        bool(source["is_sandbox"]),
        _effective_expiry(expires_at, source_grace_at),
        will_renew,
        period_type == "trial",
        False,
    )


def _has_unmatched_active_evidence(
    records_by_product: dict[str, object],
    matched_evidence: tuple[str, datetime],
    entitlement_owners: dict[tuple[str, datetime], set[str]],
    now: datetime,
    *,
    entitlement_id: str,
    non_subscription: bool,
) -> bool:
    has_unmatched_active_evidence = False
    for product_id, value in records_by_product.items():
        records = _non_subscription_records(value) if non_subscription else [value]
        for record in records:
            source = _as_dict(record, "unmatched subscription evidence")
            if "is_sandbox" not in source or not isinstance(source["is_sandbox"], bool):
                raise RevenueCatAPIError("RevenueCat response has invalid is_sandbox")
            if "product_identifier" in source and source["product_identifier"] != product_id:
                raise RevenueCatAPIError("RevenueCat response has inconsistent product identity")
            _required_string(source, "store")
            purchase_at = _datetime_value(source, "purchase_date", required=True)
            assert purchase_at is not None
            evidence = (product_id, purchase_at)
            if evidence == matched_evidence:
                continue
            owners = entitlement_owners.get(evidence, set())
            if len(owners) == 1:
                continue
            expires_at = _datetime_value(source, "expires_date")
            grace_at = _datetime_value(source, "grace_period_expires_date")
            if _datetime_value(source, "refunded_at") is not None or _datetime_value(source, "revoked_at") is not None:
                continue
            effective_expiry = _effective_expiry(expires_at, grace_at)
            if effective_expiry is None or effective_expiry > now:
                has_unmatched_active_evidence = True
    return has_unmatched_active_evidence


def _non_subscription_records(value: object) -> list[object]:
    if not isinstance(value, list) or not value:
        raise RevenueCatAPIError("RevenueCat response has invalid non-subscription evidence")
    for record in value:
        _as_dict(record, "non-subscription evidence")
    return value


def _ensure_unique_evidence_identities(subscriptions: dict[str, object], non_subscriptions: dict[str, object]) -> None:
    seen: set[tuple[str, datetime]] = set()
    for records_by_product, non_subscription in ((subscriptions, False), (non_subscriptions, True)):
        for product_id, value in records_by_product.items():
            records = _non_subscription_records(value) if non_subscription else [value]
            for record in records:
                source = _as_dict(record, "subscription evidence")
                if "product_identifier" in source and source["product_identifier"] != product_id:
                    raise RevenueCatAPIError("RevenueCat response has inconsistent product identity")
                purchase_at = _datetime_value(source, "purchase_date", required=True)
                assert purchase_at is not None
                evidence = (product_id, purchase_at)
                if evidence in seen:
                    raise RevenueCatAPIError("RevenueCat response has duplicate evidence identity")
                seen.add(evidence)


def _entitlement_owners(entitlements: dict[str, object]) -> dict[tuple[str, datetime], set[str]]:
    owners: dict[tuple[str, datetime], set[str]] = {}
    for entitlement_id, value in entitlements.items():
        entitlement = _as_dict(value, "entitlement")
        product_id = _required_string(entitlement, "product_identifier")
        purchase_at = _datetime_value(entitlement, "purchase_date", required=True)
        assert purchase_at is not None
        owners.setdefault((product_id, purchase_at), set()).add(entitlement_id)
    return owners


def _parse_remove_ads_product_id(
    entitlements: dict[str, object],
    subscriptions: dict[str, object],
    non_subscriptions: dict[str, object],
    entitlement_owners: dict[tuple[str, datetime], set[str]],
    *,
    production: bool,
    now: datetime,
) -> str | None:
    value = entitlements.get("remove_ads")
    if value is None:
        return None
    entitlement = _as_dict(value, "remove_ads entitlement")
    product_id = _required_string(entitlement, "product_identifier")
    purchase_at = _datetime_value(entitlement, "purchase_date", required=True)
    assert purchase_at is not None
    matched_evidence = (product_id, purchase_at)
    if entitlement_owners.get(matched_evidence) != {"remove_ads"}:
        raise RevenueCatAPIError("RevenueCat response has ambiguous entitlement evidence")
    active_promotional_products: list[tuple[datetime | None, str]] = []
    for promotional_product_id, value in subscriptions.items():
        if not promotional_product_id.startswith("rc_promo_remove_ads_"):
            continue
        source = _as_dict(value, "remove_ads promotional evidence")
        if _required_string(source, "store").lower() != "promotional":
            continue
        promotional_purchase_at = _datetime_value(source, "purchase_date", required=True)
        assert promotional_purchase_at is not None
        promotional_expires_at = _datetime_value(source, "expires_date", required=True)
        promotional_grace_at = _datetime_value(source, "grace_period_expires_date")
        candidate = _parse_candidate(
            source,
            product_id=promotional_product_id,
            purchase_at=promotional_purchase_at,
            entitlement_expires_at=promotional_expires_at,
            entitlement_grace_at=promotional_grace_at,
            is_subscription=True,
        )
        if (
            candidate is not None
            and not candidate[4]
            and (not production or not candidate[0])
            and (candidate[1] is None or candidate[1] > now)
        ):
            promotional_evidence = (promotional_product_id, promotional_purchase_at)
            owners = entitlement_owners.get(promotional_evidence, set())
            if owners and owners != {"remove_ads"}:
                raise RevenueCatAPIError("RevenueCat response has ambiguous entitlement evidence")
            entitlement_owners[promotional_evidence] = {"remove_ads"}
            active_promotional_products.append((candidate[1], promotional_product_id))
    expires_at = _datetime_value(entitlement, "expires_date")
    grace_at = _datetime_value(entitlement, "grace_period_expires_date")
    candidates: list[tuple[bool, datetime | None, bool, bool, bool]] = []
    if product_id in subscriptions:
        candidate = _parse_candidate(
            _as_dict(subscriptions[product_id], "remove_ads subscription evidence"),
            product_id=product_id,
            purchase_at=purchase_at,
            entitlement_expires_at=expires_at,
            entitlement_grace_at=grace_at,
            is_subscription=True,
        )
        if candidate is not None:
            candidates.append(candidate)
    if product_id in non_subscriptions:
        for record in _non_subscription_records(non_subscriptions[product_id]):
            candidate = _parse_candidate(
                _as_dict(record, "remove_ads non-subscription evidence"),
                product_id=product_id,
                purchase_at=purchase_at,
                entitlement_expires_at=expires_at,
                entitlement_grace_at=grace_at,
                is_subscription=False,
            )
            if candidate is not None:
                candidates.append(candidate)
    if not candidates:
        raise RevenueCatAPIError("RevenueCat remove_ads entitlement has no matching evidence")
    if _has_unmatched_active_evidence(
        subscriptions,
        matched_evidence,
        entitlement_owners,
        now,
        entitlement_id="remove_ads",
        non_subscription=False,
    ) or _has_unmatched_active_evidence(
        non_subscriptions,
        matched_evidence,
        entitlement_owners,
        now,
        entitlement_id="remove_ads",
        non_subscription=True,
    ):
        raise RevenueCatAPIError("RevenueCat response has ambiguous unmatched remove_ads evidence")
    if active_promotional_products:
        return max(
            active_promotional_products,
            key=lambda item: item[0] or datetime.max.replace(tzinfo=UTC),
        )[1]
    active = [
        candidate
        for candidate in candidates
        if not candidate[4]
        and (not production or not candidate[0])
        and (candidate[1] is None or candidate[1] > now)
    ]
    return product_id if active else None


def parse_revenuecat_subscriber(
    payload: object,
    *,
    production: bool,
    now: datetime,
) -> SubscriptionSnapshot:
    """Validate a v1 subscriber response without trusting inferred product names."""
    if now.tzinfo is None:
        raise RevenueCatAPIError("Reconciliation time must be timezone-aware")
    root = _as_dict(payload, "payload")
    subscriber = _as_dict(root.get("subscriber"), "subscriber")
    entitlements = _as_dict(subscriber.get("entitlements"), "entitlements")
    subscriptions = _as_dict(subscriber.get("subscriptions"), "subscriptions")
    non_subscriptions = _as_dict(subscriber.get("non_subscriptions"), "non_subscriptions")
    for value in non_subscriptions.values():
        _non_subscription_records(value)
    _ensure_unique_evidence_identities(subscriptions, non_subscriptions)
    entitlement_owners = _entitlement_owners(entitlements)
    remove_ads_product_id = _parse_remove_ads_product_id(
        entitlements,
        subscriptions,
        non_subscriptions,
        entitlement_owners,
        production=production,
        now=now,
    )
    if not entitlements:
        return _free_snapshot(remove_ads_product_id)
    if "premium" not in entitlements:
        return _free_snapshot(remove_ads_product_id)
    premium = entitlements["premium"]
    entitlement = _as_dict(premium, "premium entitlement")
    product_id = _required_string(entitlement, "product_identifier")
    purchase_at = _datetime_value(entitlement, "purchase_date", required=True)
    assert purchase_at is not None
    entitlement_expires_at = _datetime_value(entitlement, "expires_date", required=True)
    entitlement_grace_at = _datetime_value(entitlement, "grace_period_expires_date")

    candidates: list[tuple[bool, datetime | None, bool, bool, bool]] = []
    if product_id in subscriptions:
        subscription = subscriptions[product_id]
        candidate = _parse_candidate(
            _as_dict(subscription, "subscription evidence"),
            product_id=product_id,
            purchase_at=purchase_at,
            entitlement_expires_at=entitlement_expires_at,
            entitlement_grace_at=entitlement_grace_at,
            is_subscription=True,
        )
        if candidate is not None:
            candidates.append(candidate)
    if product_id in non_subscriptions:
        non_subscription = non_subscriptions[product_id]
        records = _non_subscription_records(non_subscription)
        for record in records:
            candidate = _parse_candidate(
                _as_dict(record, "non-subscription evidence"),
                product_id=product_id,
                purchase_at=purchase_at,
                entitlement_expires_at=entitlement_expires_at,
                entitlement_grace_at=entitlement_grace_at,
                is_subscription=False,
            )
            if candidate is not None:
                candidates.append(candidate)
    if not candidates:
        raise RevenueCatAPIError("RevenueCat premium entitlement has no matching evidence")
    matched_evidence = (product_id, purchase_at)
    if entitlement_owners.get(matched_evidence) != {"premium"}:
        raise RevenueCatAPIError("RevenueCat response has ambiguous entitlement evidence")
    if _has_unmatched_active_evidence(
        subscriptions,
        matched_evidence,
        entitlement_owners,
        now,
        entitlement_id="premium",
        non_subscription=False,
    ) or _has_unmatched_active_evidence(
        non_subscriptions,
        matched_evidence,
        entitlement_owners,
        now,
        entitlement_id="premium",
        non_subscription=True,
    ):
        raise RevenueCatAPIError("RevenueCat response has ambiguous unmatched premium evidence")

    allowed = [candidate for candidate in candidates if not candidate[4] and (not production or not candidate[0])]
    if not allowed:
        return _free_snapshot(remove_ads_product_id)
    active = [candidate for candidate in allowed if candidate[1] is None or candidate[1] > now]
    if not active:
        return _free_snapshot(remove_ads_product_id)
    sandbox, expires_at, will_renew, is_trial, blocked = max(
        active,
        key=lambda candidate: candidate[1] or datetime.max.replace(tzinfo=UTC),
    )
    del sandbox, blocked
    return SubscriptionSnapshot(
        status="premium",
        expires_at=expires_at,
        product_id=product_id,
        will_renew=will_renew,
        is_trial=is_trial,
        remove_ads_product_id=remove_ads_product_id,
    )


def _retry_after_seconds(value: str | None) -> int:
    if value is not None:
        try:
            return max(1, int(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is not None:
                    return max(1, ceil((retry_at - datetime.now(UTC)).total_seconds()))
            except (TypeError, ValueError):
                pass
    return 60


async def _check_reconciliation_cooldown(redis: Redis) -> None:
    try:
        cooldown = await redis.ttl(_RECONCILIATION_COOLDOWN_KEY)
    except RedisError as error:
        raise RevenueCatAPIError("RevenueCat cooldown is unavailable", failure_kind="storage") from error
    if cooldown > 0:
        raise RevenueCatAPIError(
            "RevenueCat reconciliation is cooling down",
            upstream_status=429,
            retry_after_seconds=cooldown,
        )


async def _set_reconciliation_cooldown(redis: Redis, retry_after: int) -> int:
    try:
        retained_ttl = await redis.execute_command(
            "EVAL",
            _COOLDOWN_SCRIPT,
            1,
            _RECONCILIATION_COOLDOWN_KEY,
            retry_after,
        )
    except RedisError as error:
        raise RevenueCatAPIError(
            "RevenueCat cooldown is unavailable",
            upstream_status=429,
            retry_after_seconds=retry_after,
            failure_kind="storage",
        ) from error
    if not isinstance(retained_ttl, int) or isinstance(retained_ttl, bool) or retained_ttl <= 0:
        raise RevenueCatAPIError(
            "RevenueCat cooldown returned an invalid TTL",
            upstream_status=429,
            retry_after_seconds=retry_after,
        )
    return retained_ttl


async def read_reconciliation(
    db: AsyncSession,
    redis: Redis,
    user_id: UUID,
    *,
    dry_run: bool = False,
) -> Reconciliation:
    """Fetch and validate a snapshot without changing a user projection."""
    settings = get_settings()
    if not settings.REVENUECAT_API_KEY:
        raise RevenueCatNotConfiguredError()
    try:
        production = _RECONCILIATION_ENVIRONMENTS[settings.ENVIRONMENT]
    except KeyError:
        raise RevenueCatAPIError(
            "RevenueCat reconciliation has unsupported environment",
            failure_kind="configuration",
        ) from None
    user = await _get_user_by_id(db, user_id)
    before_status = user.subscription_status
    before_expires_at = user.subscription_expires_at
    before_verified_at = user.subscription_verified_at
    before_subscription = _get_subscription_settings(user)
    before_remove_ads, before_products = _remove_ads_state(user.settings)
    await _check_reconciliation_cooldown(redis)

    url = f"https://api.revenuecat.com/v1/subscribers/{quote(str(user_id), safe='')}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {settings.REVENUECAT_API_KEY}"})
    except httpx.TimeoutException:
        raise RevenueCatAPIError("RevenueCat API request timed out") from None
    except httpx.RequestError as error:
        raise RevenueCatAPIError(f"RevenueCat API request failed: {error}") from error

    if response.status_code == 429:
        retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
        if not dry_run:
            retry_after = await _set_reconciliation_cooldown(redis, retry_after)
        raise RevenueCatAPIError(
            "RevenueCat API rate limit exceeded",
            upstream_status=429,
            retry_after_seconds=retry_after,
        )
    if response.status_code not in {200, 201}:
        raise RevenueCatAPIError(
            f"RevenueCat API returned status {response.status_code}",
            upstream_status=response.status_code,
        )
    if response.status_code == 201 and (dry_run or before_status != "free" or before_remove_ads):
        raise RevenueCatAPIError("RevenueCat unexpectedly created a customer", upstream_status=201)
    try:
        payload = response.json()
    except ValueError as error:
        raise RevenueCatAPIError("RevenueCat API returned invalid JSON") from error
    verified_at = datetime.now(UTC)
    snapshot = parse_revenuecat_subscriber(
        payload,
        production=production,
        now=verified_at,
    )
    remove_ads_matches = before_remove_ads == (snapshot.remove_ads_product_id is not None)
    if snapshot.remove_ads_product_id is not None:
        remove_ads_matches = remove_ads_matches and snapshot.remove_ads_product_id in before_products
    unchanged = (
        before_status == snapshot.status
        and before_expires_at == snapshot.expires_at
        and before_subscription.get("product_id") == snapshot.product_id
        and before_subscription.get("will_renew", False) == snapshot.will_renew
        and before_subscription.get("is_trial", False) == snapshot.is_trial
        and remove_ads_matches
    )
    return Reconciliation(
        user_id=user.id,
        before_status=before_status,
        before_expires_at=before_expires_at,
        before_verified_at=before_verified_at,
        before_subscription=before_subscription,
        snapshot=snapshot,
        verified_at=verified_at,
        outcome="unchanged" if unchanged else "changed",
        before_remove_ads=before_remove_ads,
    )


async def apply_reconciliation(db: AsyncSession, proposal: Reconciliation) -> Reconciliation:
    """Apply a previously-read RevenueCat snapshot if its baseline is still current."""
    stmt = (
        select(User)
        .where(User.id == proposal.user_id, User.deleted_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        return replace(proposal, outcome="conflict")

    if (
        user.subscription_status != proposal.before_status
        or user.subscription_expires_at != proposal.before_expires_at
        or user.subscription_verified_at != proposal.before_verified_at
        or _get_subscription_settings(user) != proposal.before_subscription
        or _remove_ads_state(user.settings)[0] != proposal.before_remove_ads
    ):
        return replace(proposal, outcome="conflict")

    if proposal.outcome == "changed":
        user.subscription_status = proposal.snapshot.status
        user.subscription_expires_at = proposal.snapshot.expires_at
        subscription = _get_subscription_settings(user)
        if proposal.snapshot.status == "premium":
            subscription.update(
                product_id=proposal.snapshot.product_id,
                will_renew=proposal.snapshot.will_renew,
                is_trial=proposal.snapshot.is_trial,
            )
        else:
            subscription.pop("product_id", None)
            subscription.pop("is_trial", None)
            subscription.pop("billing_issue", None)
            subscription["will_renew"] = False
        _set_subscription_settings(user, subscription)
        _set_remove_ads_projection(user, proposal.snapshot.remove_ads_product_id, proposal.verified_at)

    user.subscription_verified_at = proposal.verified_at
    await db.flush()

    if proposal.outcome == "changed" and proposal.before_status == "premium" and proposal.snapshot.status == "free":
        from app.jobs.subscription_jobs import apply_free_tier_limits

        await apply_free_tier_limits(db, user.id)
    elif (
        proposal.outcome == "changed" and proposal.before_status != "premium" and proposal.snapshot.status == "premium"
    ):
        await _clear_downgrade_info(db, user.id)

    return proposal


async def reconcile_user(
    db: AsyncSession,
    redis: Redis,
    user_id: UUID,
    *,
    dry_run: bool = False,
) -> Reconciliation:
    """Read and atomically apply a single user's RevenueCat reconciliation."""
    proposal = await read_reconciliation(db, redis, user_id, dry_run=dry_run)
    if dry_run:
        return proposal
    result = await apply_reconciliation(db, proposal)
    if result.outcome == "conflict":
        raise PurchaseError("Subscription changed during reconciliation; retry", status_code=503)
    return result


async def _get_user_by_id(db: AsyncSession, user_id: UUID) -> User:
    """Get user by ID or raise UserNotFoundError."""
    stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise UserNotFoundError(user_id)

    return user


def _get_subscription_settings(user: User) -> dict:
    """Get subscription-related settings from user settings JSON.

    Returns a copy to allow safe modification without affecting the original.
    """
    subscription = user.settings.get("subscription", {})
    # Return a copy to avoid in-place mutations that SQLAlchemy won't detect
    return dict(subscription)


def _remove_ads_state(settings: object) -> tuple[bool, set[str]]:
    """Return current Remove Ads access and immutable product purchase history."""
    if not isinstance(settings, dict):
        return False, set()
    value = settings.get("non_subscriptions")
    if not isinstance(value, dict):
        return False, set()
    entitlements = value.get("entitlements")
    products = value.get("products")
    active = isinstance(entitlements, list) and "remove_ads" in entitlements
    product_ids = {item for item in products if isinstance(item, str)} if isinstance(products, list) else set()
    return active, product_ids


def _set_remove_ads_projection(user: User, product_id: str | None, updated_at: datetime) -> None:
    """Merge verified Remove Ads access while retaining unrelated settings and history."""
    settings = dict(user.settings)
    raw = settings.get("non_subscriptions")
    non_subscriptions = dict(raw) if isinstance(raw, dict) else {}
    raw_products = non_subscriptions.get("products")
    raw_entitlements = non_subscriptions.get("entitlements")
    products = [item for item in raw_products if isinstance(item, str)] if isinstance(raw_products, list) else []
    entitlements = (
        [item for item in raw_entitlements if isinstance(item, str)] if isinstance(raw_entitlements, list) else []
    )
    before = (list(products), list(entitlements))
    if product_id is None:
        entitlements = [item for item in entitlements if item != "remove_ads"]
    else:
        if product_id not in products:
            products.append(product_id)
        if "remove_ads" not in entitlements:
            entitlements.append("remove_ads")
    if before == (products, entitlements):
        return
    non_subscriptions["products"] = products
    non_subscriptions["entitlements"] = entitlements
    non_subscriptions["updated_at"] = updated_at.isoformat()
    settings["non_subscriptions"] = non_subscriptions
    user.settings = settings


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
    legacy_transaction_id: str | None = None,
) -> tuple[bool, tuple[str, str] | None]:
    """Check if webhook transaction has already been processed.

    Uses Redis lock for race condition protection and database for persistence.

    Args:
        db: Database session.
        redis: Redis client.
        transaction_id: Event ID (or legacy transaction ID) used as the deduplication key.
        lock_timeout: Lock expiry in seconds.
        legacy_transaction_id: Store transaction key used by older webhook handlers.

    Returns:
        Tuple of (is_duplicate, lock_handle). True only acknowledges committed
        success or skipped audits; a failed audit is retried under a new handle.
    """
    lock_key = f"webhook_lock:{transaction_id}"

    ownership_token = secrets.token_urlsafe(24)
    lock_handle = (lock_key, ownership_token)
    lock_acquired = await redis.set(lock_key, ownership_token, nx=True, ex=lock_timeout)

    if not lock_acquired:
        logger.info("Webhook is being processed by another worker", transaction_id=transaction_id)
        raise PurchaseError("Webhook is already being processed; retry", status_code=503)

    try:
        existing = await db.scalar(
            select(WebhookTransaction).where(WebhookTransaction.transaction_id == transaction_id)
        )
        if existing is None and legacy_transaction_id and legacy_transaction_id != transaction_id:
            legacy = await db.scalar(
                select(WebhookTransaction).where(WebhookTransaction.transaction_id == legacy_transaction_id)
            )
            if legacy and legacy.payload.get("event", {}).get("id") == transaction_id:
                existing = legacy
        if existing and existing.processing_result in {"success", "skipped"}:
            await release_idempotency_lock(redis, lock_handle)
            logger.info("Webhook already processed", transaction_id=transaction_id, processed_at=existing.processed_at)
            return True, None
        return False, lock_handle
    except BaseException:
        await release_idempotency_lock(redis, lock_handle)
        raise


async def has_terminal_webhook_audit(db: AsyncSession, transaction_id: str) -> bool:
    """Return whether an event has a committed terminal webhook audit."""
    processing_result = await db.scalar(
        select(WebhookTransaction.processing_result).where(WebhookTransaction.transaction_id == transaction_id)
    )
    return processing_result in {"success", "skipped"}


async def release_idempotency_lock(redis: Redis, lock_handle: tuple[str, str] | None) -> None:
    """Release the idempotency lock after processing.

    Args:
        redis: Redis client.
        lock_handle: Lock key and ownership token (or None if not acquired).
    """
    if lock_handle is None:
        return
    try:
        await redis.execute_command(
            "EVAL",
            "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0",
            1,
            lock_handle[0],
            lock_handle[1],
        )
    except RedisError as error:
        logger.warning("Webhook lock cleanup failed", lock_key=lock_handle[0], error=str(error))


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
    transaction = await db.scalar(
        select(WebhookTransaction)
        .where(WebhookTransaction.transaction_id == transaction_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if transaction is None:
        transaction = WebhookTransaction(
            transaction_id=transaction_id,
            event_type=event_type,
            user_id=user_id,
            payload=payload,
            correlation_id=correlation_id,
            processing_result=processing_result,
            error_message=error_message,
        )
        try:
            async with db.begin_nested():
                db.add(transaction)
                await db.flush()
        except IntegrityError as error:
            if _is_webhook_transaction_unique_conflict(error):
                raise WebhookAuditConflict() from error
            raise
    elif transaction.processing_result not in {"success", "skipped"}:
        transaction.event_type = event_type
        transaction.user_id = user_id
        transaction.payload = payload
        transaction.correlation_id = correlation_id
        transaction.processing_result = processing_result
        transaction.error_message = error_message
    if transaction not in db.new:
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


def _is_webhook_transaction_unique_conflict(error: IntegrityError) -> bool:
    """Return whether PostgreSQL rejected the webhook transaction id unique key."""
    current: object | None = error.orig
    while current is not None:
        diagnostic = getattr(current, "diag", None)
        constraint_name = getattr(current, "constraint_name", None) or getattr(diagnostic, "constraint_name", None)
        if getattr(current, "sqlstate", None) == "23505" and constraint_name in {
            "ix_webhook_transactions_transaction_id",
            "webhook_transactions_transaction_id_key",
        }:
            return True
        current = getattr(current, "__cause__", None)
    return False


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


def _webhook_user_ids(event: WebhookEvent) -> list[UUID]:
    event_data = event.event
    values = [
        event_data.app_user_id,
        event_data.original_app_user_id,
        *event_data.aliases,
        *event_data.transferred_from,
        *event_data.transferred_to,
    ]
    user_ids: set[UUID] = set()
    for value in values:
        if value:
            try:
                user_ids.add(UUID(value))
            except ValueError:
                continue
    return sorted(user_ids, key=str)


def _webhook_event_class(event: WebhookEvent) -> Literal["reconcile", "skipped"]:
    """Classify whether a webhook must refresh RevenueCat customer state."""
    event_data = event.event
    if event_data.type == "SUBSCRIBER_ALIAS":
        return "skipped"
    if event_data.type in {
        "INITIAL_PURCHASE",
        "RENEWAL",
        "CANCELLATION",
        "EXPIRATION",
        "BILLING_ISSUE",
        "PRODUCT_CHANGE",
        "UNCANCELLATION",
        "TRANSFER",
    }:
        return "reconcile"
    if event_data.type != "NON_RENEWING_PURCHASE":
        return "skipped"
    entitlement_ids = set(event_data.entitlement_ids or [])
    legacy_product_ids = {item.product_identifier for item in event_data.entitlements}
    if event_data.product_id:
        legacy_product_ids.add(event_data.product_id)
    if event_data.transaction and event_data.transaction.product_id:
        legacy_product_ids.add(event_data.transaction.product_id)
    supported_entitlement = bool(entitlement_ids & {"premium", "remove_ads"})
    supported_legacy_product = bool(legacy_product_ids & {"premium", "remove_ads", "fishfeed_remove_ads"})
    return "reconcile" if supported_entitlement or supported_legacy_product else "skipped"


async def process_webhook(
    db: AsyncSession,
    event: WebhookEvent,
    redis: Redis,
) -> tuple[Literal["success", "skipped"], list[Reconciliation]]:
    """Process RevenueCat webhook event.

    Webhooks are only authenticated triggers. RevenueCat's subscriber snapshot
    remains the source of both Premium and Remove Ads projections.
    """
    event_data = event.event
    event_type = event_data.type
    settings = get_settings()
    if settings.ENVIRONMENT == "production" and event_data.environment in {"SANDBOX", "TEST"}:
        logger.info("Skipping non-production webhook", event_type=event_type)
        return "skipped", []

    event_class = _webhook_event_class(event)
    if event_class == "skipped":
        logger.info("Skipping non-mutating webhook", event_type=event_type)
        return "skipped", []

    user_ids = _webhook_user_ids(event)
    if not user_ids:
        logger.info("Skipping webhook without a local UUID identity", event_type=event_type)
        return "skipped", []
    existing_ids = set(
        (await db.scalars(select(User.id).where(User.id.in_(user_ids), User.deleted_at.is_(None)))).all()
    )
    user_ids = [user_id for user_id in user_ids if user_id in existing_ids]
    if not user_ids:
        logger.info("Skipping webhook without a local user", event_type=event_type)
        return "skipped", []

    try:
        proposals = [await read_reconciliation(db, redis, user_id) for user_id in user_ids]
    except UserNotFoundError as error:
        raise WebhookRetryableError("Webhook participant disappeared; retry") from error
    results: list[Reconciliation] = []
    for proposal in proposals:
        result = await apply_reconciliation(db, proposal)
        if result.outcome == "conflict":
            raise PurchaseError("Subscription changed during reconciliation; retry", status_code=503)
        results.append(result)
    return "success", results


async def restore_purchases(
    db: AsyncSession,
    user_id: UUID,
    receipt: str,
    platform: str,
    redis: Redis,
) -> Reconciliation:
    """Restore purchases from app store receipt.

    Validates the receipt with RevenueCat, then applies an authoritative
    subscriber snapshot.

    Args:
        db: Database session.
        user_id: User UUID.
        receipt: Base64 encoded receipt data from app store.
        platform: Platform identifier (ios or android).

    Returns:
        Applied reconciliation result.

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
    await _check_reconciliation_cooldown(redis)

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

            if response.status_code == 400:
                raise InvalidReceiptError("Receipt validation failed")
            if response.status_code == 401:
                logger.error("RevenueCat API authentication failed")
                raise RevenueCatAPIError("API authentication failed", upstream_status=response.status_code)
            if response.status_code == 429:
                retry_after = await _set_reconciliation_cooldown(
                    redis,
                    _retry_after_seconds(response.headers.get("Retry-After")),
                )
                raise RevenueCatAPIError(
                    "RevenueCat API rate limit exceeded",
                    upstream_status=429,
                    retry_after_seconds=retry_after,
                )
            if response.status_code != 200:
                logger.error("RevenueCat API error", status_code=response.status_code, response_text=response.text)
                raise RevenueCatAPIError(
                    f"API returned status {response.status_code}", upstream_status=response.status_code
                )
            try:
                receipt_result = response.json()
            except ValueError as error:
                raise RevenueCatAPIError("Receipt validation returned invalid JSON") from error
            if not isinstance(receipt_result, dict) or not isinstance(receipt_result.get("subscriber"), dict):
                raise RevenueCatAPIError("Receipt validation returned an invalid response")

    except httpx.TimeoutException:
        logger.error("RevenueCat API timeout")
        raise RevenueCatAPIError("API request timed out") from None
    except httpx.RequestError as e:
        logger.error("RevenueCat API request error", error=str(e))
        raise RevenueCatAPIError(f"API request failed: {e}") from None

    return await reconcile_user(db, redis, user_id)


async def get_subscription_status(db: AsyncSession, user_id: UUID) -> SubscriptionStatus:
    """Get current subscription status for user.

    Returns the stored RevenueCat projection without provider calls or mutations.

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

    return SubscriptionStatus(
        status=status,  # type: ignore[arg-type]
        expires_at=expires_at,
        product_id=subscription_settings.get("product_id"),
        is_trial=subscription_settings.get("is_trial", False),
        will_renew=subscription_settings.get("will_renew", False),
        original_purchase_date=None,
    )
