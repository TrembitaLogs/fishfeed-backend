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
- Review-loop RED: four receipt tests showed that restore bypassed an existing shared cooldown and dropped receipt `429` retry metadata.
- Review-loop GREEN: restore checks the shared cooldown before its POST, parses numeric/HTTP-date/fallback `Retry-After`, and uses the existing atomic no-shortening Lua TTL write. The retained-TTL test observes a pre-existing longer `2000` second value.
- The restore route tests dirty the real request projection. A fresh session in the invalidation callback observes the committed projection, and failure tests observe both the route rollback and dependency cleanup rollback while preserving the original projection for `400`, provider `502`, and reconciliation-conflict `503`.
- SQLAdmin create now has the corresponding crafted-field regression, alongside valid create and edit controls.

## Verification

- Focused Task 5 suite: `160 passed`.
- Additional verified-premium API gate: `2 passed`.
- Backup-service environment classification with `DATABASE_URL` set to the isolated database: `10 passed`.
- `ruff check`, `ruff format --check`, and `mypy app`: passed.
- Controlled full coverage run with both `DATABASE_URL` and `TEST_DATABASE_URL` set to `fishfeed_task5_test`: `1860 passed, 6 skipped, 80 deselected`, `85.89%` coverage. Its sole failure is the preclassified Task 7 OpenAPI-baseline drift for the documented Task 5 responses; no backup-service environment failure remains.
