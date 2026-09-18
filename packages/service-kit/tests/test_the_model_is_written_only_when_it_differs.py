"""The model write is idempotent by COMPARISON, and the comparison is the part worth testing.

[[LH-174]]. An OpenFGA model is immutable and versioned: every write mints a new id. So a hook that
writes unconditionally grows the store on every upgrade — measured 2026-09-18, it already held 50
models — while a hook that compares too loosely never writes and leaves the store nine relations
behind, which is the defect it exists to fix. Both failures are silent.
"""

from __future__ import annotations

from typing import Any

from service_kit.governed.auth.write_model import model_document, needs_write, shape


def _model(**relations: list[str]) -> dict[str, Any]:
    return {"type_definitions": [{"type": t, "relations": {name: {} for name in r}} for t, r in relations.items()]}


def test_an_empty_store_is_always_a_write() -> None:
    assert needs_write(None, _model(warehouse=["owner"]))


def test_an_identical_model_is_not_rewritten() -> None:
    """The property that keeps the store from growing a model per upgrade forever."""
    stored = _model(warehouse=["owner", "reader"])
    assert not needs_write(stored, _model(warehouse=["reader", "owner"])), "relation ORDER made two identical models look different"


def test_a_SERVER_ASSIGNED_ID_is_not_a_difference() -> None:
    """Every stored model carries an `id` the document being written does not. Comparing whole
    documents would therefore differ every single time — always writing, never converging."""
    stored = {"id": "01ABCDEF", **_model(warehouse=["owner"])}
    assert not needs_write(stored, _model(warehouse=["owner"]))


def test_a_MISSING_RELATION_on_an_existing_type_is_a_difference() -> None:
    """The exact drift this closes: all nine missing relations were on types that already existed, so
    a comparison keyed on type NAMES alone would have reported no change forever."""
    stored = _model(warehouse=["owner", "reader"])
    assert needs_write(stored, _model(warehouse=["owner", "reader", "maintainer"]))


def test_the_shipped_model_is_readable_and_has_the_types_the_estate_reasons_about() -> None:
    """Without this the three above could pass over a document that never loads."""
    shipped = shape(model_document())

    assert {"warehouse", "namespace", "table", "project"} <= set(shipped), f"the shipped model lost a core type: {sorted(shipped)}"
    assert "maintainer" in shipped["warehouse"], "the shipped model has no warehouse#maintainer — the relation whose absence wedged an upgrade"
