"""Unit tests for the read-only maintenance middleware helpers."""

from __future__ import annotations

from catalog.api.maintenance_mode import is_mutating


def test_is_mutating_classifies_methods() -> None:
    assert not is_mutating("GET")
    assert not is_mutating("head")
    assert not is_mutating("OPTIONS")
    assert is_mutating("POST")
    assert is_mutating("delete")
    assert is_mutating("PUT")
    assert is_mutating("PATCH")
