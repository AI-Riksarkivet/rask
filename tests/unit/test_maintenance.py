"""Unit tests for the read-only maintenance middleware helpers."""

from __future__ import annotations

from catalog.api.maintenance import RETRY_AFTER_SECONDS, is_mutating, maintenance_response


def test_is_mutating_classifies_methods() -> None:
    assert not is_mutating("GET")
    assert not is_mutating("head")
    assert not is_mutating("OPTIONS")
    assert is_mutating("POST")
    assert is_mutating("delete")
    assert is_mutating("PUT")
    assert is_mutating("PATCH")


def test_maintenance_response_shape() -> None:
    resp = maintenance_response()
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)
    assert resp.media_type == "application/problem+json"
