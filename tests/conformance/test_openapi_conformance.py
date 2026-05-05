"""OpenAPI conformance fuzz tests powered by Schemathesis.

For every operation declared in the OpenAPI schema, Schemathesis generates
a handful of synthesised requests and verifies that the response matches
the response schema declared for that operation (status code, content type,
body shape, error_code contract, etc.).

This catches the silent class of bug where a handler quietly returns a
shape the schema doesn't promise — the kind of drift that breaks the
mobile client without surfacing in unit tests.

These tests are slow (they do real HTTP-style I/O against the ASGI app
and need Postgres + Redis services up). They are excluded from the default
pytest run via the `schemathesis` marker; opt-in explicitly:

    uv run pytest -m schemathesis

CI will gate them in a separate job (Plan task T0.3).

When a conformance failure is found, fix it by either (a) updating the
handler to return the declared shape, or (b) updating the schema to match
reality and regenerating the OpenAPI baseline (`tests/contract/`). Do
**not** silence the test — that defeats the purpose.
"""

from __future__ import annotations

import pytest
import schemathesis
import schemathesis.openapi as schemathesis_openapi

from app.main import app

# Build the schema directly from `app.openapi()` rather than `from_asgi`.
# `from_asgi` issues an actual ASGI request to fetch /openapi.json at module
# import time, which routes through the Prometheus instrumentator and adds a
# `handler="/openapi.json"` sample to the process-wide default registry. That
# pollutes unrelated metrics tests that read the same global registry. Going
# through `app.openapi()` keeps schema construction in-process with no
# middleware involvement.
_schema = schemathesis_openapi.from_dict(app.openapi())
# Wire the ASGI app back so `case.call_and_validate()` dispatches through it
# (the same transport `from_asgi` would have set).
_schema.app = app
_schema.location = "/openapi.json"

# Cap the example budget. Default is 100 examples per operation, which
# multiplied across ~67 endpoints is several minutes of CI time. Five
# examples is enough to catch handler/schema drift; bump locally if
# investigating a specific failure.
_schema.config.generation.max_examples = 5


@pytest.mark.schemathesis
@_schema.parametrize()
def test_openapi_response_conforms_to_schema(case: schemathesis.Case) -> None:
    """Every response from a fuzzed request must match its declared schema."""
    case.call_and_validate()
