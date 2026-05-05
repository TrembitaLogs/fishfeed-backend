"""Detect unintentional changes to the public OpenAPI schema.

The mobile client and any third-party consumers reason about request/response
shapes from the schema FastAPI publishes. A silent change (renamed field,
shifted enum, removed endpoint) breaks the contract without ever surfacing
in unit tests.

This test snapshots the schema once and fails on any drift. When a change is
intentional, regenerate the baseline:

    uv run python -c "from app.main import app; import json; print(json.dumps(app.openapi(), indent=2, sort_keys=True))" > tests/contract/openapi_baseline.json

Then commit the new baseline alongside the change so the diff is part of the
PR review.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import app

BASELINE_PATH = Path(__file__).parent / "openapi_baseline.json"

REGEN_HINT = (
    'uv run python -c "from app.main import app; import json; '
    'print(json.dumps(app.openapi(), indent=2, sort_keys=True))" '
    "> tests/contract/openapi_baseline.json"
)


def _load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text())


def _current_schema() -> dict:
    # Round-trip through json.dumps with sort_keys to match how the baseline
    # was written, so dict ordering can never produce false positives.
    return json.loads(json.dumps(app.openapi(), sort_keys=True))


def test_openapi_baseline_file_exists() -> None:
    assert BASELINE_PATH.exists(), (
        f"Missing OpenAPI baseline at {BASELINE_PATH}. Generate it with:\n"
        f"  {REGEN_HINT}"
    )


def test_openapi_schema_matches_baseline() -> None:
    expected = _load_baseline()
    actual = _current_schema()
    assert actual == expected, (
        "OpenAPI schema drifted from baseline. If the change is intentional, "
        f"regenerate the baseline and commit it alongside the change:\n  {REGEN_HINT}"
    )
