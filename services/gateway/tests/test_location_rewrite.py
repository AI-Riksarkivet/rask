"""gateway Location-header rewrite — unit tests (no network)."""

from gateway import _rewrite_location


def test_absolute_internal_location_made_relative() -> None:
    assert _rewrite_location("http://127.0.0.1:8801/api/batches/") == "/api/batches/"


def test_absolute_with_query_preserved() -> None:
    assert _rewrite_location("http://rask-core-api:8801/api/batches?x=1") == "/api/batches?x=1"


def test_relative_location_unchanged() -> None:
    assert _rewrite_location("/api/batches/") == "/api/batches/"
