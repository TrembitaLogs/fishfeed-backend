"""Tests for validated RevenueCat reconciliation reads."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.user import User
from app.schemas.purchase import FREE_USER_LIMITS, PREMIUM_USER_LIMITS
from app.services.purchase import (
    _COOLDOWN_SCRIPT,
    _RECONCILIATION_COOLDOWN_KEY,
    PurchaseError,
    Reconciliation,
    RevenueCatAPIError,
    RevenueCatNotConfiguredError,
    SubscriptionSnapshot,
    apply_reconciliation,
    parse_revenuecat_subscriber,
    read_reconciliation,
    reconcile_user,
)


def subscriber_payload(*, sandbox: bool = False) -> dict:
    """Build a complete active subscription response."""
    return {
        "subscriber": {
            "entitlements": {
                "premium": {
                    "product_identifier": "premium.monthly",
                    "purchase_date": "2026-01-01T00:00:00Z",
                    "expires_date": "2099-01-01T00:00:00Z",
                    "grace_period_expires_date": None,
                }
            },
            "subscriptions": {
                "premium.monthly": {
                    "is_sandbox": sandbox,
                    "store": "app_store",
                    "period_type": "normal",
                    "purchase_date": "2026-01-01T00:00:00Z",
                    "expires_date": "2099-01-01T00:00:00Z",
                    "grace_period_expires_date": None,
                    "refunded_at": None,
                    "unsubscribe_detected_at": None,
                }
            },
            "non_subscriptions": {},
        }
    }


@pytest.mark.parametrize(("sandbox", "expected"), [(False, "premium"), (True, "free")])
def test_production_filters_purchase_evidence(sandbox: bool, expected: str) -> None:
    """A sandbox source cannot grant premium in production."""
    result = parse_revenuecat_subscriber(
        subscriber_payload(sandbox=sandbox),
        production=True,
        now=datetime(2026, 9, 17, tzinfo=UTC),
    )

    assert result.status == expected


def _subscription(payload: dict) -> dict:
    return payload["subscriber"]["subscriptions"]["premium.monthly"]


def _entitlement(payload: dict) -> dict:
    return payload["subscriber"]["entitlements"]["premium"]


def add_remove_ads(
    payload: dict,
    *,
    product_id: str = "fishfeed_remove_ads",
    purchase_date: str = "2026-02-01T00:00:00Z",
    expires_date: str | None = None,
    sandbox: bool = False,
    refunded_at: str | None = None,
    revoked_at: str | None = None,
    store: str = "play_store",
    in_subscriptions: bool = False,
) -> None:
    payload["subscriber"]["entitlements"]["remove_ads"] = {
        "product_identifier": product_id,
        "purchase_date": purchase_date,
        "expires_date": expires_date,
        "grace_period_expires_date": None,
    }
    record = {
        "is_sandbox": sandbox,
        "store": store,
        "period_type": "promotional" if store == "promotional" else "normal",
        "purchase_date": purchase_date,
        "expires_date": expires_date,
        "grace_period_expires_date": None,
        "refunded_at": refunded_at,
        "revoked_at": revoked_at,
        "unsubscribe_detected_at": None,
    }
    if in_subscriptions:
        payload["subscriber"]["subscriptions"][product_id] = record
    else:
        payload["subscriber"]["non_subscriptions"].setdefault(product_id, []).append(record)


def test_ads_only_customer_stays_free_with_active_remove_ads() -> None:
    """Catches dropping an active Remove Ads entitlement from a free snapshot."""
    payload = subscriber_payload()
    payload["subscriber"]["entitlements"].clear()
    payload["subscriber"]["subscriptions"].clear()
    add_remove_ads(payload)

    result = parse_revenuecat_subscriber(
        payload,
        production=True,
        now=datetime(2026, 9, 18, tzinfo=UTC),
    )

    assert result.status == "free"
    assert result.remove_ads_product_id == "fishfeed_remove_ads"


def test_separate_premium_and_remove_ads_evidence_are_both_active() -> None:
    """Catches retaining Premium while discarding separate active Remove Ads evidence."""
    payload = subscriber_payload()
    add_remove_ads(payload)

    result = parse_revenuecat_subscriber(
        payload,
        production=True,
        now=datetime(2026, 9, 18, tzinfo=UTC),
    )

    assert result.status == "premium"
    assert result.remove_ads_product_id == "fishfeed_remove_ads"


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("promotional lifetime", {"store": "promotional", "in_subscriptions": True}),
        (
            "promotional finite",
            {
                "store": "promotional",
                "in_subscriptions": True,
                "expires_date": "2099-01-01T00:00:00Z",
            },
        ),
    ],
)
def test_remove_ads_promotional_grants_remain_active(name: str, kwargs: dict) -> None:
    """Catches treating valid promotional lifetime or finite grants as inactive."""
    del name
    payload = subscriber_payload()
    add_remove_ads(payload, **kwargs)

    result = parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 18, tzinfo=UTC))

    assert result.remove_ads_product_id == "fishfeed_remove_ads"


def test_production_remove_ads_promo_wins_over_stale_sandbox_entitlement() -> None:
    """A dashboard grant must survive RevenueCat v1 selecting older sandbox evidence."""
    payload = subscriber_payload()
    payload["subscriber"]["entitlements"].clear()
    payload["subscriber"]["subscriptions"].clear()
    add_remove_ads(payload, sandbox=True, purchase_date="2026-04-29T14:17:29Z")
    payload["subscriber"]["subscriptions"]["rc_promo_remove_ads_daily"] = {
        "is_sandbox": False,
        "store": "promotional",
        "period_type": "normal",
        "purchase_date": "2026-09-18T11:48:54Z",
        "expires_date": "2026-09-19T11:48:54Z",
        "grace_period_expires_date": None,
        "refunded_at": None,
        "revoked_at": None,
        "unsubscribe_detected_at": None,
    }

    result = parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 18, tzinfo=UTC))

    assert result.status == "free"
    assert result.remove_ads_product_id == "rc_promo_remove_ads_daily"


def test_production_remove_ads_promo_does_not_invalidate_active_premium() -> None:
    """Recognized Remove Ads promo evidence must not look unowned to Premium parsing."""
    payload = subscriber_payload()
    add_remove_ads(payload, sandbox=True, purchase_date="2026-04-29T14:17:29Z")
    payload["subscriber"]["subscriptions"]["rc_promo_remove_ads_daily"] = {
        "is_sandbox": False,
        "store": "promotional",
        "period_type": "normal",
        "purchase_date": "2026-09-18T11:48:54Z",
        "expires_date": "2026-09-19T11:48:54Z",
        "grace_period_expires_date": None,
        "refunded_at": None,
        "revoked_at": None,
        "unsubscribe_detected_at": None,
    }

    result = parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 18, tzinfo=UTC))

    assert result.status == "premium"
    assert result.remove_ads_product_id == "rc_promo_remove_ads_daily"


def test_production_remove_ads_promo_does_not_hide_malformed_sibling() -> None:
    """An accepted dashboard grant must not bypass validation of sibling evidence."""
    payload = subscriber_payload()
    payload["subscriber"]["entitlements"].clear()
    payload["subscriber"]["subscriptions"].clear()
    add_remove_ads(payload, sandbox=True, purchase_date="2026-04-29T14:17:29Z")
    payload["subscriber"]["subscriptions"]["rc_promo_remove_ads_daily"] = {
        "is_sandbox": False,
        "store": "promotional",
        "period_type": "normal",
        "purchase_date": "2026-09-18T11:48:54Z",
        "expires_date": "2026-09-19T11:48:54Z",
        "grace_period_expires_date": None,
        "refunded_at": None,
        "revoked_at": None,
        "unsubscribe_detected_at": None,
    }
    payload["subscriber"]["subscriptions"]["rc_promo_remove_ads_monthly"] = {
        "store": "promotional",
        "period_type": "normal",
        "purchase_date": "2026-09-18T12:00:00Z",
        "expires_date": "2026-10-18T12:00:00Z",
    }

    with pytest.raises(RevenueCatAPIError, match="invalid is_sandbox"):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 18, tzinfo=UTC))


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("expired finite grant", {"expires_date": "2026-09-17T00:00:00Z"}),
        ("refunded", {"refunded_at": "2026-09-17T00:00:00Z"}),
        ("revoked", {"revoked_at": "2026-09-17T00:00:00Z"}),
        ("sandbox in production", {"sandbox": True}),
    ],
)
def test_remove_ads_inactive_evidence_is_not_granted(name: str, kwargs: dict) -> None:
    """Catches granting expired, refunded, revoked, or sandbox Remove Ads evidence in production."""
    del name
    payload = subscriber_payload()
    add_remove_ads(payload, **kwargs)

    result = parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 18, tzinfo=UTC))

    assert result.remove_ads_product_id is None


def test_remove_ads_missing_evidence_is_rejected() -> None:
    """Catches accepting an entitlement whose matching provider evidence disappeared."""
    payload = subscriber_payload()
    add_remove_ads(payload)
    payload["subscriber"]["subscriptions"].pop("fishfeed_remove_ads", None)
    del payload["subscriber"]["non_subscriptions"]["fishfeed_remove_ads"]

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 18, tzinfo=UTC))


def test_shared_evidence_between_premium_and_remove_ads_is_rejected() -> None:
    """Catches treating one provider tuple as both Premium and Remove Ads."""
    payload = subscriber_payload()
    add_remove_ads(payload)
    payload["subscriber"]["entitlements"]["remove_ads"] = deepcopy(_entitlement(payload))

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 18, tzinfo=UTC))


def test_remove_ads_revoked_old_promo_does_not_hide_active_store_source() -> None:
    """Catches letting an older revoked promotion invalidate a mapped active store purchase."""
    payload = subscriber_payload()
    add_remove_ads(payload)
    payload["subscriber"]["non_subscriptions"]["fishfeed_remove_ads"].append(
        {
            **payload["subscriber"]["non_subscriptions"]["fishfeed_remove_ads"][0],
            "store": "promotional",
            "period_type": "promotional",
            "purchase_date": "2026-01-01T00:00:00Z",
            "revoked_at": "2026-01-15T00:00:00Z",
        }
    )

    result = parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 18, tzinfo=UTC))

    assert result.remove_ads_product_id == "fishfeed_remove_ads"


def test_remove_ads_two_active_sources_are_rejected() -> None:
    """Catches silently accepting an unowned active Remove Ads aggregate tuple."""
    payload = subscriber_payload()
    add_remove_ads(payload)
    payload["subscriber"]["non_subscriptions"]["fishfeed_remove_ads_legacy"] = [
        {
            **payload["subscriber"]["non_subscriptions"]["fishfeed_remove_ads"][0],
            "purchase_date": "2026-01-01T00:00:00Z",
        }
    ]

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 18, tzinfo=UTC))


def test_remove_ads_sandbox_aggregate_with_production_source_is_rejected() -> None:
    """Catches inferring a production grant from mixed-environment aggregates."""
    payload = subscriber_payload()
    add_remove_ads(payload, sandbox=True)
    payload["subscriber"]["non_subscriptions"]["fishfeed_remove_ads_production"] = [
        {
            **payload["subscriber"]["non_subscriptions"]["fishfeed_remove_ads"][0],
            "is_sandbox": False,
            "purchase_date": "2026-01-01T00:00:00Z",
        }
    ]

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 18, tzinfo=UTC))


@pytest.mark.parametrize(
    ("name", "mutate", "expected"),
    [
        (
            "expired",
            lambda body: _subscription(body).update(expires_date="2026-09-16T00:00:00Z")
            or _entitlement(body).update(expires_date="2026-09-16T00:00:00Z"),
            "free",
        ),
        (
            "boundary",
            lambda body: _subscription(body).update(expires_date="2026-09-17T00:00:00Z")
            or _entitlement(body).update(expires_date="2026-09-17T00:00:00Z"),
            "free",
        ),
        (
            "cancelled",
            lambda body: _subscription(body).update(unsubscribe_detected_at="2026-09-16T00:00:00Z"),
            "premium",
        ),
        ("trial", lambda body: _subscription(body).update(period_type="trial"), "premium"),
        (
            "grace",
            lambda body: _subscription(body).update(
                expires_date="2026-09-16T00:00:00Z", grace_period_expires_date="2026-09-18T00:00:00Z"
            )
            or _entitlement(body).update(
                expires_date="2026-09-16T00:00:00Z", grace_period_expires_date="2026-09-18T00:00:00Z"
            ),
            "premium",
        ),
        ("refunded", lambda body: _subscription(body).update(refunded_at="2026-09-16T00:00:00Z"), "free"),
        (
            "finite promo",
            lambda body: _subscription(body).update(store="promotional", period_type="promotional"),
            "premium",
        ),
    ],
)
def test_validated_subscription_states(name: str, mutate, expected: str) -> None:
    """Changing expiry, renewal, refund, trial, or grace must change the snapshot."""
    del name
    payload = subscriber_payload()
    mutate(payload)

    result = parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))

    assert result.status == expected
    if expected == "premium" and _subscription(payload)["unsubscribe_detected_at"] is not None:
        assert result.will_renew is False
    if expected == "premium" and _subscription(payload)["period_type"] == "trial":
        assert result.is_trial is True
    if expected == "premium" and _subscription(payload)["period_type"] == "promotional":
        assert result.will_renew is False


def test_validated_lifetime_promo_and_one_time_premium() -> None:
    """Only explicit promotional or one-time evidence may validate a lifetime entitlement."""
    promo = subscriber_payload()
    _entitlement(promo)["expires_date"] = None
    _subscription(promo).update(expires_date=None, store="promotional", period_type="promotional")
    one_time = subscriber_payload()
    _entitlement(one_time)["expires_date"] = None
    record = deepcopy(_subscription(one_time))
    record.pop("expires_date")
    record.pop("grace_period_expires_date")
    one_time["subscriber"]["subscriptions"] = {}
    one_time["subscriber"]["non_subscriptions"] = {"premium.monthly": [record]}

    promo_result = parse_revenuecat_subscriber(promo, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))
    one_time_result = parse_revenuecat_subscriber(one_time, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))

    assert promo_result.status == "premium"
    assert promo_result.expires_at is None
    assert promo_result.will_renew is False
    assert one_time_result.status == "premium"
    assert one_time_result.expires_at is None


def test_absent_premium_entitlement_is_free() -> None:
    """Catches treating the absence of Premium as a paid subscription."""
    payload = subscriber_payload()
    payload["subscriber"]["entitlements"].clear()

    result = parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))

    assert result.status == "free"


def test_malformed_remove_ads_entitlement_is_rejected() -> None:
    """Catches treating a present malformed Remove Ads entitlement as absent."""
    payload = subscriber_payload()
    payload["subscriber"]["entitlements"]["remove_ads"] = {}

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: _subscription(body).pop("expires_date"),
        lambda body: _subscription(body).update(expires_date="not-a-date"),
        lambda body: _subscription(body).pop("is_sandbox"),
        lambda body: _subscription(body).update(is_sandbox="false"),
        lambda body: _subscription(body).update(purchase_date="2026-01-02T00:00:00Z"),
    ],
)
def test_incomplete_or_ambiguous_premium_evidence_is_rejected(mutate) -> None:
    """A missing required guard must refuse rather than guess a premium tier."""
    payload = subscriber_payload()
    mutate(payload)

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


def test_refunded_mapped_remove_ads_evidence_does_not_revoke_premium() -> None:
    """A distinct mapped refund cannot revoke the premium entitlement's source."""
    payload = subscriber_payload()
    payload["subscriber"]["entitlements"]["remove_ads"] = {
        "product_identifier": "fishfeed_remove_ads",
        "purchase_date": "2026-02-01T00:00:00Z",
        "expires_date": None,
        "grace_period_expires_date": None,
    }
    payload["subscriber"]["non_subscriptions"]["fishfeed_remove_ads"] = [
        {
            "is_sandbox": False,
            "store": "app_store",
            "period_type": "normal",
            "purchase_date": "2026-02-01T00:00:00Z",
            "refunded_at": "2026-09-16T00:00:00Z",
            "unsubscribe_detected_at": None,
        }
    ]

    result = parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))

    assert result.status == "premium"


def test_duplicate_non_subscription_evidence_is_ambiguous() -> None:
    """A refunded and active copy of one tuple cannot prove separate lifetime purchases."""
    payload = subscriber_payload()
    _entitlement(payload)["expires_date"] = None
    record = deepcopy(_subscription(payload))
    record.pop("expires_date")
    record.pop("grace_period_expires_date")
    payload["subscriber"]["subscriptions"] = {}
    payload["subscriber"]["non_subscriptions"]["premium.monthly"] = [
        {**record, "refunded_at": "2026-09-16T00:00:00Z"},
        record,
    ]

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


def test_cross_container_duplicate_evidence_is_ambiguous() -> None:
    """Subscription and one-time aggregates cannot independently claim one tuple."""
    payload = subscriber_payload()
    _subscription(payload)["refunded_at"] = "2026-09-16T00:00:00Z"
    record = deepcopy(_subscription(payload))
    record["refunded_at"] = None
    record.pop("expires_date")
    record.pop("grace_period_expires_date")
    payload["subscriber"]["non_subscriptions"]["premium.monthly"] = [record]

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


def test_unmatched_production_source_is_ambiguous_when_current_entitlement_is_sandbox() -> None:
    """A v1 aggregate cannot prove which mixed-environment source granted premium."""
    payload = subscriber_payload(sandbox=True)
    payload["subscriber"]["subscriptions"]["premium.yearly"] = {
        **deepcopy(_subscription(payload)),
        "product_identifier": "premium.yearly",
        "is_sandbox": False,
        "purchase_date": "2026-02-01T00:00:00Z",
        "expires_date": "2099-02-01T00:00:00Z",
    }

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


def test_unmatched_active_premium_source_is_ambiguous() -> None:
    """A second potentially premium source must not be silently ignored."""
    payload = subscriber_payload()
    payload["subscriber"]["subscriptions"]["premium.yearly"] = {
        **deepcopy(_subscription(payload)),
        "product_identifier": "premium.yearly",
        "purchase_date": "2026-02-01T00:00:00Z",
        "expires_date": "2099-02-01T00:00:00Z",
    }

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


def test_premium_allows_exactly_mapped_remove_ads_evidence() -> None:
    """A separate entitlement mapping proves that an active Remove Ads purchase is not premium evidence."""
    payload = subscriber_payload()
    payload["subscriber"]["entitlements"]["remove_ads"] = {
        "product_identifier": "fishfeed_remove_ads",
        "purchase_date": "2026-02-01T00:00:00Z",
        "expires_date": None,
        "grace_period_expires_date": None,
    }
    payload["subscriber"]["non_subscriptions"]["fishfeed_remove_ads"] = [
        {
            "is_sandbox": False,
            "store": "app_store",
            "period_type": "normal",
            "purchase_date": "2026-02-01T00:00:00Z",
            "refunded_at": None,
            "unsubscribe_detected_at": None,
        }
    ]

    result = parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))

    assert result.status == "premium"


@pytest.mark.parametrize("sandbox", [False, True])
def test_same_product_different_purchase_stays_ambiguous(sandbox: bool) -> None:
    """Only the entitlement-selected purchase may be skipped from aggregate ambiguity checks."""
    payload = subscriber_payload(sandbox=sandbox)
    payload["subscriber"]["non_subscriptions"]["premium.monthly"] = [
        {
            "is_sandbox": False,
            "store": "app_store",
            "period_type": "normal",
            "purchase_date": "2026-02-01T00:00:00Z",
            "refunded_at": None,
            "unsubscribe_detected_at": None,
        }
    ]

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


@pytest.mark.parametrize("extra_entitlement", ["remove_ads_duplicate", "bonus"])
def test_multiple_entitlement_owners_of_evidence_are_ambiguous(extra_entitlement: str) -> None:
    """An evidence tuple may only be exempt when owned by one non-premium entitlement."""
    payload = subscriber_payload()
    if extra_entitlement == "remove_ads_duplicate":
        payload["subscriber"]["entitlements"]["remove_ads"] = {
            "product_identifier": "fishfeed_remove_ads",
            "purchase_date": "2026-02-01T00:00:00Z",
            "expires_date": None,
            "grace_period_expires_date": None,
        }
        payload["subscriber"]["entitlements"][extra_entitlement] = deepcopy(
            payload["subscriber"]["entitlements"]["remove_ads"]
        )
        payload["subscriber"]["non_subscriptions"]["fishfeed_remove_ads"] = [
            {
                "is_sandbox": False,
                "store": "app_store",
                "period_type": "normal",
                "purchase_date": "2026-02-01T00:00:00Z",
                "refunded_at": None,
                "unsubscribe_detected_at": None,
            }
        ]
    else:
        payload["subscriber"]["entitlements"][extra_entitlement] = deepcopy(_entitlement(payload))

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


def test_matching_non_subscription_requires_official_list_shape() -> None:
    """A singleton object is not a valid v1 non-subscriptions aggregate."""
    payload = subscriber_payload()
    _entitlement(payload)["expires_date"] = None
    record = deepcopy(_subscription(payload))
    record.pop("expires_date")
    record.pop("grace_period_expires_date")
    payload["subscriber"]["subscriptions"] = {}
    payload["subscriber"]["non_subscriptions"]["premium.monthly"] = record

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


def test_present_invalid_matching_non_subscription_is_rejected() -> None:
    """A valid subscription cannot hide a malformed sibling aggregate value."""
    payload = subscriber_payload()
    payload["subscriber"]["non_subscriptions"]["premium.monthly"] = None

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


def test_present_invalid_matching_subscription_is_rejected() -> None:
    """A valid one-time record cannot hide a malformed subscription aggregate value."""
    payload = subscriber_payload()
    _entitlement(payload)["expires_date"] = None
    record = deepcopy(_subscription(payload))
    record.pop("expires_date")
    record.pop("grace_period_expires_date")
    payload["subscriber"]["subscriptions"]["premium.monthly"] = None
    payload["subscriber"]["non_subscriptions"]["premium.monthly"] = [record]

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: _subscription(body).pop("grace_period_expires_date"),
        lambda body: _subscription(body).update(grace_period_expires_date="2026-09-19T00:00:00Z"),
    ],
)
def test_missing_or_inconsistent_source_grace_is_rejected(mutate) -> None:
    """Entitlement grace cannot be trusted without matching source evidence."""
    payload = subscriber_payload()
    _entitlement(payload).update(
        expires_date="2026-09-16T00:00:00Z",
        grace_period_expires_date="2026-09-18T00:00:00Z",
    )
    _subscription(payload)["expires_date"] = "2026-09-16T00:00:00Z"
    _subscription(payload)["grace_period_expires_date"] = "2026-09-18T00:00:00Z"
    mutate(payload)

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


def test_explicit_malformed_premium_entitlement_is_rejected() -> None:
    """A present but invalid premium key cannot be mistaken for an absent entitlement."""
    payload = subscriber_payload()
    payload["subscriber"]["entitlements"]["premium"] = None

    with pytest.raises(RevenueCatAPIError):
        parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))


class FakeRedis:
    """Small Redis boundary double for cooldown behavior."""

    def __init__(self, ttl: int = -2, eval_result: int | None = None) -> None:
        self.ttl_value = ttl
        self.eval_result = eval_result
        self.eval_calls: list[tuple[object, ...]] = []

    async def ttl(self, key: str) -> int:
        assert key == "revenuecat:reconcile:cooldown"
        return self.ttl_value

    async def execute_command(self, *args: object) -> int:
        self.eval_calls.append(args)
        if self.eval_result is not None:
            return self.eval_result
        assert isinstance(args[-1], int)
        return args[-1]


class BrokenRedis(FakeRedis):
    """Simulate an unavailable cooldown store."""

    async def ttl(self, key: str) -> int:
        raise __import__("redis").exceptions.ConnectionError("unavailable")


class BrokenEvalRedis(FakeRedis):
    """Simulate failure while installing a provider cooldown."""

    async def execute_command(self, *args: object) -> int:
        raise __import__("redis").exceptions.ConnectionError("unavailable")


def _user(status: str = "free") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        subscription_status=status,
        subscription_expires_at=None,
        subscription_verified_at=None,
        settings={"subscription": {}},
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_reconciliation_merges_premium_snapshot_without_losing_other_settings(
    async_session: AsyncSession,
) -> None:
    """Removing the fresh guarded write must leave this proposal unapplied."""
    await async_session.execute(text("DELETE FROM users"))
    await async_session.commit()
    user = User(
        email="apply-reconciliation@example.com",
        password_hash="test_hash",
        subscription_status="free",
        settings={"non_subscriptions": {"products": ["remove_ads"]}, "theme": "dark"},
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)
    verified_at = datetime(2026, 9, 17, tzinfo=UTC)
    proposal = Reconciliation(
        user_id=user.id,
        before_status="free",
        before_expires_at=None,
        before_verified_at=None,
        before_subscription={},
        snapshot=SubscriptionSnapshot(
            status="premium",
            expires_at=datetime(2026, 10, 17, tzinfo=UTC),
            product_id="premium.monthly",
            will_renew=True,
            is_trial=False,
        ),
        verified_at=verified_at,
        outcome="changed",
    )

    result = await apply_reconciliation(async_session, proposal)

    assert result.outcome == "changed"
    await async_session.refresh(user)
    assert user.subscription_status == "premium"
    assert user.subscription_verified_at == verified_at
    assert user.settings == {
        "non_subscriptions": {"products": ["remove_ads"]},
        "theme": "dark",
        "subscription": {"product_id": "premium.monthly", "will_renew": True, "is_trial": False},
    }


def _proposal(
    user: User,
    *,
    status: str,
    verified_at: datetime,
    expires_at: datetime | None = None,
    product_id: str | None = None,
    remove_ads_product_id: str | None = None,
    outcome: str = "changed",
) -> Reconciliation:
    non_subscriptions = user.settings.get("non_subscriptions", {})
    entitlements = non_subscriptions.get("entitlements", []) if isinstance(non_subscriptions, dict) else []
    return Reconciliation(
        user_id=user.id,
        before_status=user.subscription_status,
        before_expires_at=user.subscription_expires_at,
        before_verified_at=user.subscription_verified_at,
        before_subscription=dict(user.settings.get("subscription", {})),
        snapshot=SubscriptionSnapshot(
            status=status,
            expires_at=expires_at,
            product_id=product_id,
            will_renew=False,
            is_trial=False,
            remove_ads_product_id=remove_ads_product_id,
        ),
        verified_at=verified_at,
        outcome=outcome,  # type: ignore[arg-type]
        before_remove_ads=isinstance(entitlements, list) and "remove_ads" in entitlements,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_reconciliation_grants_remove_ads_without_premium(async_session: AsyncSession) -> None:
    """Removing the Remove Ads projection leaves a paid free-tier account without access."""
    await async_session.execute(text("DELETE FROM users"))
    record = User(email=f"{uuid4()}@example.com", password_hash="unused", settings={"theme": "dark"})
    async_session.add(record)
    await async_session.commit()
    proposal = _proposal(
        record,
        status="free",
        remove_ads_product_id="fishfeed_remove_ads",
        verified_at=datetime(2026, 9, 18, tzinfo=UTC),
    )

    await apply_reconciliation(async_session, proposal)
    await async_session.refresh(record)

    assert record.subscription_status == "free"
    assert record.settings["theme"] == "dark"
    assert record.settings["non_subscriptions"]["products"] == ["fishfeed_remove_ads"]
    assert record.settings["non_subscriptions"]["entitlements"] == ["remove_ads"]


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_reconciliation_removes_only_remove_ads_access(async_session: AsyncSession) -> None:
    """Removing only the entitlement must retain purchase history and unrelated Premium state."""
    await async_session.execute(text("DELETE FROM users"))
    expires_at = datetime(2026, 10, 1, tzinfo=UTC)
    record = User(
        email=f"{uuid4()}@example.com",
        password_hash="unused",
        subscription_status="premium",
        subscription_expires_at=expires_at,
        settings={
            "theme": "dark",
            "subscription": {"product_id": "premium.monthly", "will_renew": True},
            "non_subscriptions": {
                "products": ["fishfeed_remove_ads"],
                "entitlements": ["remove_ads", "bonus"],
            },
        },
    )
    async_session.add(record)
    await async_session.commit()

    await apply_reconciliation(
        async_session,
        _proposal(
            record,
            status="premium",
            expires_at=expires_at,
            product_id="premium.monthly",
            verified_at=datetime(2026, 9, 18, tzinfo=UTC),
        ),
    )
    await async_session.refresh(record)

    assert record.subscription_status == "premium"
    assert record.subscription_expires_at == expires_at
    assert record.settings["theme"] == "dark"
    assert record.settings["subscription"] == {"product_id": "premium.monthly", "will_renew": False, "is_trial": False}
    assert record.settings["non_subscriptions"]["products"] == ["fishfeed_remove_ads"]
    assert record.settings["non_subscriptions"]["entitlements"] == ["bonus"]


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("remove_ads_product_id", "initial"),
    [("fishfeed_remove_ads", {}), (None, {"products": ["fishfeed_remove_ads"], "entitlements": ["remove_ads"]})],
)
async def test_repeated_fresh_remove_ads_reconciliations_keep_non_subscription_updated_at_stable(
    async_session: AsyncSession,
    remove_ads_product_id: str | None,
    initial: dict[str, object],
) -> None:
    """A matching fresh proposal cannot duplicate identifiers or rewrite its projection timestamp."""
    await async_session.execute(text("DELETE FROM users"))
    record = User(
        email=f"{uuid4()}@example.com",
        password_hash="unused",
        settings={"non_subscriptions": initial, "theme": "dark"},
    )
    async_session.add(record)
    await async_session.commit()
    payload = subscriber_payload()
    payload["subscriber"]["entitlements"].clear()
    payload["subscriber"]["subscriptions"].clear()
    if remove_ads_product_id is not None:
        add_remove_ads(payload, product_id=remove_ads_product_id)
    real_client = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    with (
        patch(
            "app.services.purchase.get_settings",
            return_value=SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production"),
        ),
        patch(
            "app.services.purchase.httpx.AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
        ),
    ):
        assert (await reconcile_user(async_session, FakeRedis(), record.id)).outcome == "changed"
        await async_session.commit()
        await async_session.refresh(record)
        before_updated_at = record.settings["non_subscriptions"].get("updated_at")

        assert (await reconcile_user(async_session, FakeRedis(), record.id)).outcome == "unchanged"
        await async_session.commit()
        await async_session.refresh(record)

    assert record.settings["non_subscriptions"].get("updated_at") == before_updated_at
    assert record.settings["non_subscriptions"].get("products", []).count("fishfeed_remove_ads") == 1
    assert record.settings["non_subscriptions"].get("entitlements", []).count("remove_ads") <= 1
    assert len(requests) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_reconciliation_conflicts_when_remove_ads_changes_after_read(async_engine) -> None:
    """A delayed Remove Ads grant cannot overwrite a newer local access projection."""
    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as setup:
        await setup.execute(text("DELETE FROM users"))
        user = User(email=f"{uuid4()}@example.com", password_hash="unused", settings={})
        setup.add(user)
        await setup.commit()
        user_id = user.id

    async with sessions() as stale, sessions() as writer:
        before = await stale.get(User, user_id)
        current = await writer.get(User, user_id)
        assert before is not None and current is not None
        proposal = _proposal(before, status="free", verified_at=datetime(2026, 9, 18, tzinfo=UTC))
        current.settings = {"non_subscriptions": {"products": ["fishfeed_remove_ads"], "entitlements": ["remove_ads"]}}
        await writer.commit()

        assert (await apply_reconciliation(stale, proposal)).outcome == "conflict"
        await stale.rollback()


@pytest.mark.asyncio
async def test_read_reconciliation_marks_remove_ads_only_change_with_one_provider_request() -> None:
    """Ignoring Remove Ads in equality would falsely report this one-request snapshot as unchanged."""
    user = _user()
    requests: list[httpx.Request] = []
    payload = subscriber_payload()
    payload["subscriber"]["entitlements"].clear()
    payload["subscriber"]["subscriptions"].clear()
    add_remove_ads(payload)
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    with (
        patch(
            "app.services.purchase.get_settings",
            return_value=SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production"),
        ),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch(
            "app.services.purchase.httpx.AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
        ),
    ):
        result = await read_reconciliation(AsyncMock(), FakeRedis(), user.id)

    assert result.outcome == "changed"
    assert result.snapshot.remove_ads_product_id == "fishfeed_remove_ads"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_remove_ads_provider_exception_preserves_local_projection() -> None:
    """Provider failure must not remove local Remove Ads access before a verified response arrives."""
    user = _user()
    user.settings = {"non_subscriptions": {"products": ["fishfeed_remove_ads"], "entitlements": ["remove_ads"]}}
    before = deepcopy(user.settings)
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch(
            "app.services.purchase.httpx.AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
        ),
        pytest.raises(RevenueCatAPIError),
    ):
        await read_reconciliation(AsyncMock(), FakeRedis(), user.id)

    assert user.settings == before


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_reconciliation_conflicts_after_another_session_commits(
    async_engine,
) -> None:
    """A delayed provider result cannot overwrite a newer committed snapshot."""
    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as setup:
        await setup.execute(text("DELETE FROM users"))
        user = User(email="conflict@example.com", password_hash="test_hash", settings={"theme": "dark"})
        setup.add(user)
        await setup.commit()
        user_id = user.id

    async with sessions() as session_a, sessions() as session_b:
        user_a = await session_a.get(User, user_id)
        user_b = await session_b.get(User, user_id)
        assert user_a is not None and user_b is not None
        proposal_a = _proposal(user_a, status="premium", verified_at=datetime(2026, 9, 17, tzinfo=UTC))
        proposal_b = _proposal(user_b, status="free", verified_at=datetime(2026, 9, 18, tzinfo=UTC))

        assert (await apply_reconciliation(session_b, proposal_b)).outcome == "changed"
        await session_b.commit()
        assert (await apply_reconciliation(session_a, proposal_a)).outcome == "conflict"
        await session_a.rollback()

    async with sessions() as check:
        current = await check.get(User, user_id)
        assert current is not None
        assert current.subscription_verified_at == datetime(2026, 9, 18, tzinfo=UTC)
        assert current.settings == {"theme": "dark", "subscription": {"will_renew": False}}


@pytest.mark.asyncio(loop_scope="session")
async def test_simultaneous_apply_waits_for_locked_winner_then_conflicts(async_engine) -> None:
    """The second identical baseline apply blocks behind the row lock."""
    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as setup:
        await setup.execute(text("DELETE FROM users"))
        user = User(email="locked-conflict@example.com", password_hash="test_hash")
        setup.add(user)
        await setup.commit()
        user_id = user.id
    async with sessions() as first, sessions() as second:
        a = await first.get(User, user_id)
        b = await second.get(User, user_id)
        assert a is not None and b is not None
        proposal_a = _proposal(a, status="premium", verified_at=datetime(2026, 9, 17, tzinfo=UTC))
        proposal_b = _proposal(b, status="free", verified_at=datetime(2026, 9, 18, tzinfo=UTC))
        locked = asyncio.Event()
        release = asyncio.Event()

        async def winner() -> None:
            assert (await apply_reconciliation(first, proposal_a)).outcome == "changed"
            locked.set()
            await release.wait()
            await first.commit()

        task_a = asyncio.create_task(winner())
        await asyncio.wait_for(locked.wait(), timeout=1)
        task_b = asyncio.create_task(apply_reconciliation(second, proposal_b))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task_b), timeout=0.1)
        release.set()
        await asyncio.wait_for(task_a, timeout=1)
        assert (await asyncio.wait_for(task_b, timeout=1)).outcome == "conflict"
        await second.rollback()


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("changed_field", ["status", "expiry", "verified", "metadata"])
async def test_apply_reconciliation_conflicts_for_each_changed_baseline_field(async_engine, changed_field: str) -> None:
    """Each captured baseline field independently prevents a stale overwrite."""
    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    initial_status = "free" if changed_field == "status" else "premium"
    async with sessions() as setup:
        await setup.execute(text("DELETE FROM users"))
        user = User(email=f"baseline-{changed_field}@example.com", password_hash="test_hash", subscription_status=initial_status)
        setup.add(user)
        await setup.commit()
        user_id = user.id
    async with sessions() as stale, sessions() as writer:
        original = await stale.get(User, user_id)
        current = await writer.get(User, user_id)
        assert original is not None and current is not None
        proposal = _proposal(original, status=initial_status, verified_at=datetime(2026, 9, 17, tzinfo=UTC))
        if changed_field == "status":
            current.subscription_status = "premium"
        elif changed_field == "expiry":
            current.subscription_expires_at = datetime(2026, 10, 1, tzinfo=UTC)
        elif changed_field == "verified":
            current.subscription_verified_at = datetime(2026, 9, 1, tzinfo=UTC)
        else:
            current.settings = {"subscription": {"product_id": "current"}}
        await writer.commit()
        assert (await apply_reconciliation(stale, proposal)).outcome == "conflict"
        await stale.rollback()
    async with sessions() as check:
        current = await check.get(User, user_id)
        assert current is not None
        if changed_field == "status":
            assert current.subscription_status == "premium"
        elif changed_field == "expiry":
            assert current.subscription_expires_at == datetime(2026, 10, 1, tzinfo=UTC)
        elif changed_field == "verified":
            assert current.subscription_verified_at == datetime(2026, 9, 1, tzinfo=UTC)
        else:
            assert current.settings == {"subscription": {"product_id": "current"}}


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("deleted", [False, True])
async def test_apply_reconciliation_conflicts_for_missing_or_deleted_user(async_engine, deleted: bool) -> None:
    """A disappeared user is a conflict and never recreated or overwritten."""
    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    user_id = uuid4()
    async with sessions() as setup:
        await setup.execute(text("DELETE FROM users"))
        if deleted:
            user = User(id=user_id, email="deleted-proposal@example.com", password_hash="test_hash", deleted_at=datetime.now(UTC))
            setup.add(user)
            await setup.commit()
    proposal_user = User(id=user_id, email="proposal@example.com", password_hash="test_hash", settings={})
    proposal = _proposal(proposal_user, status="premium", verified_at=datetime(2026, 9, 17, tzinfo=UTC))
    async with sessions() as session:
        assert (await apply_reconciliation(session, proposal)).outcome == "conflict"
        await session.rollback()
    async with sessions() as check:
        current = await check.get(User, user_id)
        assert (current is None) if not deleted else (current is not None and current.deleted_at is not None)


@pytest.mark.asyncio(loop_scope="session")
async def test_reconcile_user_does_not_lock_during_provider_wait_and_returns_503_on_conflict(async_engine) -> None:
    """A concurrent apply completes while HTTP waits, then the stale wrapper reports retryable conflict."""
    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as setup:
        await setup.execute(text("DELETE FROM users"))
        user = User(email="wait-conflict@example.com", password_hash="test_hash")
        setup.add(user)
        await setup.commit()
        user_id = user.id

    started = asyncio.Event()
    release = asyncio.Event()
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    real_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        started.set()
        await release.wait()
        return httpx.Response(200, json=subscriber_payload())

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    async with sessions() as session_a, sessions() as session_b:
        with (
            patch("app.services.purchase.get_settings", return_value=settings),
            patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
        ):
            task = asyncio.create_task(reconcile_user(session_a, FakeRedis(), user_id))
            await started.wait()
            user_b = await session_b.get(User, user_id)
            assert user_b is not None
            proposal_b = _proposal(user_b, status="free", verified_at=datetime(2026, 9, 18, tzinfo=UTC))
            await asyncio.wait_for(apply_reconciliation(session_b, proposal_b), timeout=1)
            await session_b.commit()
            release.set()
            with pytest.raises(PurchaseError, match="Subscription changed during reconciliation") as error:
                await task
            assert error.value.status_code == 503
            await session_a.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_reconciliation_rollback_restores_user_quota_and_settings(async_engine) -> None:
    """The guarded downgrade remains one transaction until its caller commits."""
    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as setup:
        await setup.execute(text("DELETE FROM users"))
        user = User(
            email="rollback@example.com",
            password_hash="test_hash",
            subscription_status="premium",
            subscription_expires_at=datetime(2026, 10, 1, tzinfo=UTC),
            free_ai_scans_remaining=100,
            settings={"subscription": {"product_id": "premium.monthly", "will_renew": True}, "theme": "dark"},
        )
        setup.add(user)
        await setup.commit()
        user_id = user.id

    async with sessions() as applying:
        before = await applying.get(User, user_id)
        assert before is not None
        result = await apply_reconciliation(
            applying,
            _proposal(before, status="free", verified_at=datetime(2026, 9, 17, tzinfo=UTC)),
        )
        assert result.outcome == "changed"
        await applying.rollback()

    async with sessions() as check:
        restored = await check.get(User, user_id)
        assert restored is not None
        assert restored.subscription_status == "premium"
        assert restored.free_ai_scans_remaining == 100
        assert restored.settings == {
            "subscription": {"product_id": "premium.monthly", "will_renew": True},
            "theme": "dark",
        }


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_reconciliation_handles_premium_free_and_lifetime_transitions(async_engine) -> None:
    """Removing any transition branch must leave the projection or premium metadata wrong."""
    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as setup:
        await setup.execute(text("DELETE FROM users"))
        user = User(
            email="transitions@example.com",
            password_hash="test_hash",
            free_ai_scans_remaining=100,
            settings={"theme": "dark", "non_subscriptions": {"products": ["remove_ads"]}},
        )
        setup.add(user)
        await setup.commit()
        user_id = user.id

    transitions = (
        ("premium", datetime(2026, 10, 1, tzinfo=UTC), "premium.monthly"),
        ("free", None, None),
        ("premium", None, "premium.lifetime"),
    )
    for index, (status, expires_at, product_id) in enumerate(transitions):
        async with sessions() as applying:
            current = await applying.get(User, user_id)
            assert current is not None
            result = await apply_reconciliation(
                applying,
                _proposal(
                    current,
                    status=status,
                    expires_at=expires_at,
                    product_id=product_id,
                    verified_at=datetime(2026, 9, 17 + index, tzinfo=UTC),
                ),
            )
            assert result.outcome == "changed"
            await applying.commit()

        async with sessions() as check:
            projected = await check.get(User, user_id)
            assert projected is not None
            assert projected.settings["theme"] == "dark"
            assert projected.settings["non_subscriptions"] == {"products": ["remove_ads"]}
            if status == "premium" and product_id == "premium.monthly":
                assert projected.subscription_status == "premium"
                assert projected.subscription_expires_at == expires_at
                assert projected.settings["subscription"] == {
                    "product_id": "premium.monthly",
                    "will_renew": False,
                    "is_trial": False,
                }
                assert projected.free_ai_scans_remaining == 100
            elif status == "free":
                assert projected.subscription_status == "free"
                assert projected.subscription_expires_at is None
                assert projected.settings["subscription"] == {"will_renew": False}
                assert projected.free_ai_scans_remaining == FREE_USER_LIMITS.ai_scans_per_month
            else:
                assert projected.subscription_status == "premium"
                assert projected.subscription_expires_at is None
                assert projected.settings["subscription"] == {
                    "product_id": "premium.lifetime",
                    "will_renew": False,
                    "is_trial": False,
                }

    async with sessions() as check:
        current = await check.get(User, user_id)
        assert current is not None
        assert current.subscription_status == "premium"
        assert current.subscription_expires_at is None
        assert current.settings == {
            "theme": "dark",
            "non_subscriptions": {"products": ["remove_ads"]},
            "subscription": {"product_id": "premium.lifetime", "will_renew": False, "is_trial": False},
        }


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("legacy_status", ["expired", "cancelled"])
async def test_legacy_nonpremium_upgrade_clears_downgrade_info(async_engine, legacy_status: str) -> None:
    """Legacy expired/cancelled rows regain premium without carrying downgrade state forward."""
    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as setup:
        await setup.execute(text("DELETE FROM users"))
        user = User(
            email=f"{legacy_status}@example.com",
            password_hash="test_hash",
            subscription_status=legacy_status,
            free_ai_scans_remaining=1,
            settings={"limits_exceeded": {"fish": {}}, "downgraded_at": "old", "theme": "dark"},
        )
        setup.add(user)
        await setup.commit()
        user_id = user.id

    async with sessions() as applying:
        current = await applying.get(User, user_id)
        assert current is not None
        await apply_reconciliation(
            applying,
            _proposal(
                current,
                status="premium",
                product_id="premium.monthly",
                verified_at=datetime(2026, 9, 17, tzinfo=UTC),
            ),
        )
        await applying.commit()

    async with sessions() as check:
        upgraded = await check.get(User, user_id)
        assert upgraded is not None
        assert upgraded.free_ai_scans_remaining == PREMIUM_USER_LIMITS.ai_scans_per_month
        assert upgraded.settings == {
            "theme": "dark",
            "subscription": {"product_id": "premium.monthly", "will_renew": False, "is_trial": False},
        }


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(("status", "quota"), [("free", 3), ("premium", 17)])
async def test_unchanged_reconciliation_refreshes_verification_without_resetting_quota(
    async_engine,
    status: str,
    quota: int,
) -> None:
    """Same-state verification refreshes only its timestamp, never quota consequences."""
    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as setup:
        await setup.execute(text("DELETE FROM users"))
        user = User(
            email=f"unchanged-{status}@example.com",
            password_hash="test_hash",
            subscription_status=status,
            free_ai_scans_remaining=quota,
            settings={"subscription": {"will_renew": False}, "theme": "dark"},
        )
        setup.add(user)
        await setup.commit()
        user_id = user.id

    async with sessions() as applying:
        current = await applying.get(User, user_id)
        assert current is not None
        await apply_reconciliation(
            applying,
            _proposal(
                current,
                status=status,
                verified_at=datetime(2026, 9, 17, tzinfo=UTC),
                outcome="unchanged",
            ),
        )
        await applying.commit()

    async with sessions() as check:
        unchanged = await check.get(User, user_id)
        assert unchanged is not None
        assert unchanged.free_ai_scans_remaining == quota
        assert unchanged.settings == {"subscription": {"will_renew": False}, "theme": "dark"}


@pytest.mark.asyncio
async def test_read_requires_configured_revenuecat_key() -> None:
    """A missing credential must use the established configuration error."""
    with (
        patch("app.services.purchase.get_settings", return_value=SimpleNamespace(REVENUECAT_API_KEY=None)),
        pytest.raises(RevenueCatNotConfiguredError),
    ):
        await read_reconciliation(AsyncMock(), FakeRedis(), uuid4())


@pytest.mark.asyncio
async def test_read_uses_one_encoded_authorized_ten_second_request() -> None:
    """A mutation that adds a retry, platform header, or wrong timeout must fail here."""
    user = _user()
    redis = FakeRedis()
    requests: list[httpx.Request] = []
    client_options: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=subscriber_payload())

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client_options.append(kwargs)
        return real_client(transport=transport, **kwargs)

    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
    ):
        result = await read_reconciliation(AsyncMock(), redis, user.id)

    assert result.snapshot.status == "premium"
    assert requests[0].url.path == f"/v1/subscribers/{user.id}"
    assert requests[0].headers["Authorization"] == "Bearer test-key"
    assert "X-Platform" not in requests[0].headers
    assert len(requests) == 1
    assert client_options == [{"timeout": 10.0}]


@pytest.mark.asyncio
@pytest.mark.parametrize(("environment", "expected"), [("production", "free"), ("development", "premium")])
async def test_read_uses_explicit_environment_policy(environment: str, expected: str) -> None:
    """Only the repository's production environment filters sandbox evidence."""
    user = _user()
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT=environment)
    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=subscriber_payload(sandbox=True))),
            **kwargs,
        )

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
    ):
        result = await read_reconciliation(AsyncMock(), FakeRedis(), user.id)

    assert result.snapshot.status == expected


@pytest.mark.asyncio
async def test_read_rejects_unknown_environment_before_provider_call() -> None:
    """A typo such as prod must not silently authorize sandbox premium."""
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="prod")
    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock()),
        patch("app.services.purchase.httpx.AsyncClient") as client,
        pytest.raises(RevenueCatAPIError),
    ):
        await read_reconciliation(AsyncMock(), FakeRedis(), uuid4())

    client.assert_not_called()


@pytest.mark.asyncio
async def test_read_captures_baseline_before_provider_response() -> None:
    """A delayed response cannot replace the baseline required by the later apply conflict check."""
    user = _user()
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        user.subscription_status = "premium"
        user.settings = {"subscription": {"product_id": "other"}}
        return httpx.Response(200, json=subscriber_payload())

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
    ):
        result = await read_reconciliation(AsyncMock(), FakeRedis(), user.id)

    assert result.before_status == "free"
    assert result.before_subscription == {}


@pytest.mark.asyncio
async def test_existing_cooldown_skips_http_request() -> None:
    """A 2,000-second cooldown must stop the next fifteen-minute reader tick."""
    user = _user()
    redis = FakeRedis(ttl=2000)
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient") as client,
        pytest.raises(RevenueCatAPIError) as error,
    ):
        await read_reconciliation(AsyncMock(), redis, user.id)

    assert error.value.upstream_status == 429
    assert error.value.retry_after_seconds == 2000
    client.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_sets_apply_cooldown_but_not_dry_run() -> None:
    """Only an apply-mode 429 may write the shared cooldown key."""
    user = _user()
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "120"})), **kwargs
        )

    for dry_run, expected_writes in ((False, 1), (True, 0)):
        redis = FakeRedis()
        with (
            patch("app.services.purchase.get_settings", return_value=settings),
            patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
            patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
            pytest.raises(RevenueCatAPIError) as error,
        ):
            await read_reconciliation(AsyncMock(), redis, user.id, dry_run=dry_run)
        assert error.value.retry_after_seconds == 120
        assert len(redis.eval_calls) == expected_writes


@pytest.mark.asyncio
async def test_rate_limit_reports_atomically_retained_cooldown_ttl() -> None:
    """The 429 metadata must report a concurrently retained longer cooldown."""
    user = _user()
    redis = FakeRedis(eval_result=2000)
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    requests = 0
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
        pytest.raises(RevenueCatAPIError) as error,
    ):
        await read_reconciliation(AsyncMock(), redis, user.id)

    assert requests == 1
    assert error.value.upstream_status == 429
    assert error.value.retry_after_seconds == 2000


@pytest.mark.asyncio
async def test_rate_limit_rejects_an_invalid_retained_cooldown_ttl() -> None:
    """A non-positive Lua result cannot become a retry delay."""
    user = _user()
    redis = FakeRedis(eval_result=0)
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "120"}))
        return real_client(transport=transport, **kwargs)

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
        pytest.raises(RevenueCatAPIError) as error,
    ):
        await read_reconciliation(AsyncMock(), redis, user.id)

    assert str(error.value) == "RevenueCat cooldown returned an invalid TTL"
    assert error.value.upstream_status == 429
    assert error.value.retry_after_seconds == 120


@pytest.mark.asyncio
async def test_rate_limit_redis_failure_preserves_provider_metadata() -> None:
    """A failed cooldown write must still stop the current pass as a provider 429."""
    user = _user()
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    calls = 0
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
        pytest.raises(RevenueCatAPIError) as error,
    ):
        await read_reconciliation(AsyncMock(), BrokenEvalRedis(), user.id)

    assert calls == 1
    assert error.value.upstream_status == 429
    assert error.value.retry_after_seconds == 120
    assert isinstance(error.value.__cause__, __import__("redis").exceptions.ConnectionError)


@pytest.mark.asyncio
@pytest.mark.parametrize(("header", "expected"), [("0", 1), ("not-a-date", 60)])
async def test_rate_limit_normalizes_retry_after(header: str, expected: int) -> None:
    """Bad or zero Retry-After values must not create a zero-second retry storm."""
    user = _user()
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": header}))
        return real_client(transport=transport, **kwargs)

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
        pytest.raises(RevenueCatAPIError) as error,
    ):
        await read_reconciliation(AsyncMock(), FakeRedis(), user.id)

    assert error.value.retry_after_seconds == expected


@pytest.mark.asyncio
async def test_rate_limit_parses_http_date_retry_after() -> None:
    """An HTTP-date backoff must not degrade to the 60-second fallback."""
    user = _user()
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    header = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": header}))
        return real_client(transport=transport, **kwargs)

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
        pytest.raises(RevenueCatAPIError) as error,
    ):
        await read_reconciliation(AsyncMock(), FakeRedis(), user.id, dry_run=True)

    assert 61 <= error.value.retry_after_seconds <= 120


@pytest.mark.asyncio(loop_scope="session")
async def test_cooldown_lua_retains_a_longer_existing_ttl(redis_client) -> None:
    """The actual Redis Lua script must never shorten a cross-trigger cooldown."""
    await redis_client.set(_RECONCILIATION_COOLDOWN_KEY, "1", ex=2000)
    try:
        retained_ttl = await redis_client.execute_command(
            "EVAL", _COOLDOWN_SCRIPT, 1, _RECONCILIATION_COOLDOWN_KEY, 120
        )
        assert retained_ttl > 1900
        assert await redis_client.ttl(_RECONCILIATION_COOLDOWN_KEY) > 1900
    finally:
        await redis_client.delete(_RECONCILIATION_COOLDOWN_KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "dry_run", "local_status", "raises"),
    [(201, False, "free", False), (201, True, "free", True), (201, False, "premium", True)],
)
async def test_customer_creation_is_limited_to_routine_free_apply(
    status: int,
    dry_run: bool,
    local_status: str,
    raises: bool,
) -> None:
    """An unexpected v1 get-or-create response cannot downgrade a premium or dry-run user."""
    user = _user(local_status)
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json=subscriber_payload())), **kwargs
        )

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
    ):
        if raises:
            with pytest.raises(RevenueCatAPIError) as error:
                await read_reconciliation(AsyncMock(), FakeRedis(), user.id, dry_run=dry_run)
            assert error.value.upstream_status == 201
        else:
            result = await read_reconciliation(AsyncMock(), FakeRedis(), user.id, dry_run=dry_run)
            assert result.snapshot.status == "premium"


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("provider_status", [200, 201])
async def test_created_customer_cannot_revoke_existing_remove_ads(
    async_session: AsyncSession,
    provider_status: int,
) -> None:
    """A newly created provider profile cannot disprove existing Remove Ads access."""
    record = User(
        email=f"{uuid4()}@example.com",
        password_hash="unused",
        subscription_status="free",
        settings={
            "non_subscriptions": {
                "products": ["fishfeed_remove_ads"],
                "entitlements": ["remove_ads"],
            }
        },
    )
    async_session.add(record)
    await async_session.commit()
    payload = {"subscriber": {"entitlements": {}, "subscriptions": {}, "non_subscriptions": {}}}
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        transport = httpx.MockTransport(lambda request: httpx.Response(provider_status, json=payload))
        return real_client(transport=transport, **kwargs)

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
    ):
        if provider_status == 201:
            with pytest.raises(RevenueCatAPIError) as error:
                await reconcile_user(async_session, FakeRedis(), record.id)
            assert error.value.upstream_status == 201
            await async_session.rollback()
        else:
            await reconcile_user(async_session, FakeRedis(), record.id)
            await async_session.commit()

    await async_session.refresh(record)
    active = "remove_ads" in record.settings["non_subscriptions"]["entitlements"]
    assert active is (provider_status == 201)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before_status", "mutated_status", "raises"),
    [("premium", "free", True), ("free", "premium", False)],
)
async def test_customer_creation_uses_pre_request_status(
    before_status: str,
    mutated_status: str,
    raises: bool,
) -> None:
    """A delayed 201 decision must use the state captured before provider I/O."""
    user = _user(before_status)
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        user.subscription_status = mutated_status
        return httpx.Response(201, json=subscriber_payload())

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
    ):
        if raises:
            with pytest.raises(RevenueCatAPIError) as error:
                await read_reconciliation(AsyncMock(), FakeRedis(), user.id)
            assert error.value.upstream_status == 201
        else:
            result = await read_reconciliation(AsyncMock(), FakeRedis(), user.id)
            assert result.before_status == "free"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed", "timeout", "redis", "ambiguous"])
async def test_read_failures_preserve_the_local_user(failure: str) -> None:
    """Failed or ambiguous reads preserve every stored subscription projection field."""
    user = _user("premium")
    user.subscription_expires_at = datetime(2099, 1, 1, tzinfo=UTC)
    user.subscription_verified_at = datetime(2026, 9, 1, tzinfo=UTC)
    user.settings = {
        "subscription": {"product_id": "premium.monthly", "will_renew": True, "is_trial": False},
        "non_subscriptions": {"products": ["remove_ads"]},
    }
    before = (
        user.subscription_status,
        user.subscription_expires_at,
        user.subscription_verified_at,
        deepcopy(user.settings),
    )
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "malformed":
            return httpx.Response(200, content=b"not json")
        if failure == "ambiguous":
            payload = subscriber_payload()
            _entitlement(payload)["product_identifier"] = "different.product"
            return httpx.Response(200, json=payload)
        raise httpx.ReadTimeout("timed out", request=request)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
        pytest.raises(RevenueCatAPIError),
    ):
        await read_reconciliation(AsyncMock(), BrokenRedis() if failure == "redis" else FakeRedis(), user.id)

    assert (user.subscription_status, user.subscription_expires_at, user.subscription_verified_at, user.settings) == before


@pytest.mark.asyncio
async def test_timeout_makes_exactly_one_provider_request() -> None:
    """Timeout recovery is delegated to a later trigger, never an in-request retry loop."""
    user = _user()
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    calls = 0
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
        pytest.raises(RevenueCatAPIError),
    ):
        await read_reconciliation(AsyncMock(), FakeRedis(), user.id)

    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_read_preserves_response_status_for_provider_errors(status: int) -> None:
    """A response-derived error must carry its upstream status for callers."""
    user = _user()
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(status)), **kwargs)

    with (
        patch("app.services.purchase.get_settings", return_value=settings),
        patch("app.services.purchase._get_user_by_id", new=AsyncMock(return_value=user)),
        patch("app.services.purchase.httpx.AsyncClient", side_effect=client_factory),
        pytest.raises(RevenueCatAPIError) as error,
    ):
        await read_reconciliation(AsyncMock(), FakeRedis(), user.id)

    assert error.value.upstream_status == status
