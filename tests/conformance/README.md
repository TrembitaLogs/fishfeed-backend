# OpenAPI Conformance Tests

Schemathesis-powered fuzz tests that verify every declared endpoint returns
responses matching its OpenAPI schema. Detects backend↔mobile contract
drift before it reaches production.

## Why a separate marker

Conformance tests are slow (real ASGI calls against ~67 endpoints with
generated payloads) and noisy when first introduced. They are excluded
from the default `uv run pytest` to keep the inner loop fast.

## How to run

Locally, with Postgres + Redis available (use the `ssh fishfeed-pve` env
or local docker-compose):

```bash
uv run pytest -m schemathesis
```

To investigate a single failing operation, raise the example budget and
filter by path:

```bash
uv run pytest -m schemathesis -k 'GET_/api/v1/aquariums' -v
```

Bump `_schema.config.generation.max_examples` in
`test_openapi_conformance.py` when triaging.

## When a test fails

A failure means either the handler returns a shape the schema doesn't
declare, or the schema declares a shape the handler doesn't honour.

Two ways to fix:

1. **Handler is wrong** — change the handler to match the contract. This
   is the usual case for newly-discovered drift.
2. **Schema is wrong** — update the Pydantic response model so the
   schema reflects reality, then regenerate the contract baseline:

   ```bash
   uv run python -c "from app.main import app; import json; print(json.dumps(app.openapi(), indent=2, sort_keys=True))" > tests/contract/openapi_baseline.json
   ```

Do **not** silence the test by adding the failing operation to a skip
list. The whole point of this suite is to catch unannounced contract
changes.

## CI integration

A dedicated CI job runs `uv run pytest -m schemathesis` separately from
the main test job (Plan task T0.3). Conformance failures are
non-blocking initially; once the suite stabilises they will gate merges.
