"""The checked-in OpenAPI document is exactly what the code serves."""

from pathlib import Path

from gremlin_control.openapi import document

PUBLISHED = Path(__file__).resolve().parents[2] / "docs/openapi.json"


def test_published_openapi_matches_the_code():
    # Regenerate: cd control && uv run python -m gremlin_control.openapi ../docs/openapi.json
    assert PUBLISHED.read_text() == document()
