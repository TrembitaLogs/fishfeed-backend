# Task 5: close existing writer bypasses

## Delivered

- `GET /api/v1/purchases/subscription` remains a local formatter: no RevenueCat or Redis call and no projection mutation.
- Receipt restore still validates the authenticated user's receipt, then uses the shared reconciliation path. The route commits before cache invalidation and returns the local formatted projection; both premium and free reconciliation results are covered.
- Receipt validation errors remain `400`; provider errors retain upstream metadata and return provider errors; reconciliation conflicts remain `503`; cross-user restores remain `403`.
- Direct admin grant and subscription update writers look up the user first, then return `409 Manage premium grants and revocations in RevenueCat`. Both routes document `409`; unrelated admin authorization, 404, and quota operations remain covered.
- SQLAdmin user create/edit forms allow only `email` and `nickname`. A server-side raw-form allowlist rejects crafted premium fields; password hashes, subscription fields, verification timestamp, settings, and other non-profile fields are not exposed.
- Verified lifetime and verified past-expiry premium users retain the AI API gate during provider outage.

## TDD evidence

- RED: direct admin writer tests failed because both writers mutated state.
- GREEN: service/API guards passed after the shared `409` rejection.
- RED: restore route attempted to serialize a `Reconciliation` as `SubscriptionStatus`.
- GREEN: restore commits, invalidates cache, and returns the local formatter. SQLAdmin allowlist/crafted-input and profile-edit tests also pass.

## Verification

- Focused Task 5 suite: `150 passed`.
- Additional verified-premium API gate: `2 passed`.
- Backup-service environment classification with `DATABASE_URL` set to the isolated database: `10 passed`.
- `ruff check`, `ruff format --check`, and `mypy app`: passed.
- Controlled full coverage run: `1842 passed, 6 skipped, 80 deselected`, `84.78%` coverage. It reported nine failures: eight backup-service tests used the default `DATABASE_URL` (`localhost:5432`) rather than the supplied `TEST_DATABASE_URL` and pass with the explicit isolated `DATABASE_URL`; the remaining OpenAPI-baseline drift is expected from the new documented responses and belongs to Task 7's baseline update.
