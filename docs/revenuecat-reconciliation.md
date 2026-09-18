# RevenueCat reconciliation rollout

This runbook is an approval-gated operational path. It does not authorize a
production configuration change, migration, dry-run, grant, deploy, rollback,
or data reset. The current 23 accounts are test data, but they must not be
reset as part of this work.

## 1. Preflight evidence

Record the candidate source revision or image, the current Alembic revision,
and evidence that the candidate passed isolated PostgreSQL and Redis tests.
Use mocked provider calls for local tests. Confirm that no production database,
Redis, credential, webhook secret, or Sentry configuration is present in the
test process.

## 2. Separately approved RevenueCat configuration

With separate approval, configure and verify the project public API key in
`REVENUECAT_API_KEY` and the webhook Authorization secret. V1 subscriber reads
support the project public key; this does not require a new v2 secret. Verify
the configured project's identity response, webhook authentication rejection,
UUID alias mapping, and actual promotional, lifetime, and environment fixtures
without printing keys, secrets, payloads, or customer data.

Missing credentials block live verification; they do not block fixture-driven
development. A v1 customer-info read is a **Get or Create Customer** request:
an unknown ID can create an empty RevenueCat customer. Use only
operator-confirmed known customer UUIDs for a live dry-run. An unexpected 201
must stop the dry-run and be recorded; do not delete the created customer.

## 3. Separately approved additive schema preparation

Apply only the backward-compatible nullable `subscription_verified_at` migration
while the old API and worker images remain active. Do not change user statuses.
Run the candidate image only as a one-shot dry-run against that schema; do not
start its scheduler or replace the API/worker at this stage. The required
synthetic syntax is illustrative only:

```bash
uv run python -m app.workers.feeding_worker --run-once --job=check_subscriptions --dry-run --user-id 11111111-1111-4111-8111-111111111111
```

## 4. Review the dry-run report

The report contains IDs, prior and proposed status/expiry, reason, and
changed/unchanged/skipped/error totals. It must not contain credentials, full
provider payloads, or emails. Include the three legacy premium rows with no
expiry in the ordinary report. Record unresolved identities rather than
inventing historical intent.

Fresh RevenueCat evidence is authoritative. The first normal apply pass repairs
resolvable rows. Do not create an account-repair script, migration, or
unconditional SQL updates from this report. A manual RevenueCat grant requires
its own explicit approval.

## 5. Separately approved deploy and apply

Deploy the API and worker together so no old projection writer remains active.
Then perform an apply pass from fresh reads and verify webhook authentication
rejection, committed audits, retry behavior, changed/unchanged/error totals,
cache refresh, and the scheduled job.

The existing Customer Info read now projects Premium and Remove Ads together.
Ads-only `NON_RENEWING_PURCHASE`, `CANCELLATION`, `EXPIRATION`, and both sides
of `TRANSFER` reconcile current provider state. The existing daily
reconciliation of free users repairs missed Remove Ads events; it needs no
additional API call or scheduler. Product history is retained after access
ends.

Promotional evidence is accepted from the v1 `subscriptions` container, while
ordinary non-consumables remain under `non_subscriptions`. If v1 cannot map
multiple active or mixed-environment sources, preserve the local access rather
than downgrading it. Admin `Yes`/`No` is the backend projection; Flutter
independently uses RevenueCat SDK active entitlements.

Retrying an already terminal event ID is deduplicated. Recovery therefore uses
a new provider event or scheduled reconciliation. Production promotional grant
and revoke actions still require action-time approval.

At the present distribution of three premium and 20 free test accounts, the
estimated scheduled volume is 23–92 reads per day: daily for all users, and
hourly only for premium users with a finite expiry near or past expiry. Scheduler
delay and webhook/restore traffic add to this estimate; outages and cooldowns
are excluded. A missed far-future refund can therefore remain active for up to
a day. Failed or ambiguous provider reads retain the last confirmed access; the
worker does not independently expire it during an outage.

No mobile deployment is required for this backend rollout. Sandbox SDK premium
can disagree with the production backend because production rejects sandbox
evidence. Do not add a production sandbox UUID exception without a separate
decision.

## 6. Rollback limits

An old image is not automatically projection-compatible: it restores direct
expiry, admin, restore, and webhook writers. A safe rollback requires a tested
compatibility build that retains the nullable model mapping, the shared access
helper, and non-mutating subscription GET; it must disable the legacy
subscription job and legacy restore/admin/webhook projection writers. Disabled
purchase mutations return retryable 503 after normal authentication and never
write a successful audit. Other worker jobs may remain enabled.

Keep the current subscription data and nullable column; do not restore or drop
the database. Recovery is paused in compatibility rollback mode and must be
reported as such. A direct old-image rollback is a separately approved return
to old subscription behavior, not a safe default. No tested compatibility build
exists yet; this runbook does not create one.

## OPEN gates before public launch

- **RevenueCat live verification:** credentials, project identity, aliases, and
  promotional/lifetime/environment fixtures require separately approved live
  verification. Local mocks do not close this gate.
- **Purchase-to-unlock:** on an approved isolated non-production backend that
  accepts sandbox, record real TestFlight and Play internal-testing purchases
  for fresh test users. Prove SDK UUID to authenticated webhook to verified
  backend premium to AI/family access, then expiry/refund to free limits. Record
  build versions, environment, event IDs, and observed transitions without
  secrets; also prove production rejects the same sandbox event. A dashboard
  grant is not a substitute.
- **Sandbox routing and review builds:** isolated sandbox infrastructure and
  review-build routing need separate provisioning/configuration approval. If
  absent, this gate remains OPEN; do not change production configuration or add
  a production allowlist to close it.
