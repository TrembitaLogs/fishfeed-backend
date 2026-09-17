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


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["subscriber"]["entitlements"].clear(),
        lambda body: body["subscriber"]["entitlements"].clear()
        or body["subscriber"]["entitlements"].update(remove_ads={}),
    ],
)
def test_absent_or_ads_only_entitlement_is_free(mutate) -> None:
    """A non-premium entitlement cannot be promoted by a product name."""
    payload = subscriber_payload()
    mutate(payload)

    result = parse_revenuecat_subscriber(payload, production=True, now=datetime(2026, 9, 17, tzinfo=UTC))

    assert result.status == "free"


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


def _proposal(user: User, *, status: str, verified_at: datetime) -> Reconciliation:
    return Reconciliation(
        user_id=user.id,
        before_status=user.subscription_status,
        before_expires_at=user.subscription_expires_at,
        before_verified_at=user.subscription_verified_at,
        before_subscription=dict(user.settings.get("subscription", {})),
        snapshot=SubscriptionSnapshot(
            status=status, expires_at=None, product_id=None, will_renew=False, is_trial=False
        ),
        verified_at=verified_at,
        outcome="changed",
    )


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
@pytest.mark.parametrize("failure", ["malformed", "timeout", "redis"])
async def test_read_failures_preserve_the_local_user(failure: str) -> None:
    """Any failed read must stop before mutating status, expiry, or verification time."""
    user = _user("premium")
    user.subscription_expires_at = datetime(2099, 1, 1, tzinfo=UTC)
    before = (user.subscription_status, user.subscription_expires_at, user.subscription_verified_at)
    settings = SimpleNamespace(REVENUECAT_API_KEY="test-key", ENVIRONMENT="production")
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "malformed":
            return httpx.Response(200, content=b"not json")
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

    assert (user.subscription_status, user.subscription_expires_at, user.subscription_verified_at) == before


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
