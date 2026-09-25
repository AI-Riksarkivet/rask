"""The model write is idempotent by COMPARISON, and the comparison is the part worth testing.

[[LH-174]]. An OpenFGA model is immutable and versioned: every write mints a new id. So a hook that
writes unconditionally grows the store on every upgrade — measured 2026-09-18, it already held 50
models — while a hook that compares too loosely never writes and leaves the store behind. Behind by a
relation NAME is the drift this hook was built for (nine relations, 2026-09-18); behind by a rule BODY
is the drift a name-keyed comparison cannot see at all, because a tightened `can_*` keeps its name.
Every one of those failures is silent.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from service_kit.governed.auth.write_model import model_document, needs_write, shape


_READER: dict[str, Any] = {"computedUserset": {"relation": "reader"}}
_PASS_GRANTS: dict[str, Any] = {"computedUserset": {"relation": "pass_grants"}}
_READER_OR_PASS_GRANTS: dict[str, Any] = {"union": {"child": [_READER, _PASS_GRANTS]}}


def _model(**relations: list[str]) -> dict[str, Any]:
    """Types whose relations are all `define <name>: [user]`, in the form `fga model transform` emits.

    PRODUCTION-SHAPED ON PURPOSE. A direct relation compiles to a ``this`` body plus its type
    restriction in ``metadata``; a bare ``{}`` body is not a relation OpenFGA accepts, and a fixture
    built from them carries no rule to differ on, so it cannot tell a name comparison from a body one.
    """
    return {
        "schema_version": "1.1",
        "type_definitions": [
            {
                "type": t,
                "relations": {name: {"this": {}} for name in names},
                "metadata": {"relations": {name: {"directly_related_user_types": [{"type": "user"}]} for name in names}},
            }
            for t, names in relations.items()
        ],
    }


def _warehouse(can_read_data: dict[str, Any], *, reader_types: list[dict[str, str]] | None = None, condition: str = "") -> dict[str, Any]:
    """`warehouse` with two direct rungs and one derived permission, every part of it a parameter."""
    model = _model(warehouse=["reader", "pass_grants"])
    warehouse = model["type_definitions"][0]
    warehouse["relations"]["can_read_data"] = can_read_data
    if reader_types is not None:
        warehouse["metadata"]["relations"]["reader"]["directly_related_user_types"] = reader_types
    if condition:
        model["conditions"] = {"non_expired_grant": {"name": "non_expired_grant", "expression": condition}}
    return model


def _as_openfga_serves_it(model: dict[str, Any]) -> dict[str, Any]:
    """``model`` as `GET /stores/{id}/authorization-models` returns it.

    A server-assigned ``id``, plus the default fills the store materialises on write — ``metadata:
    null``, ``relations: {}``, ``module: ""``, ``condition: ""``, ``source_info: null`` — measured
    against the live store 2026-09-13 and recorded on `service_kit.governed.fga._without_defaults`.
    """
    served = copy.deepcopy(model)
    for definition in served["type_definitions"]:
        definition.setdefault("relations", {})
        metadata = definition.setdefault("metadata", None)
        if metadata is None:
            continue
        metadata.update(module="", source_info=None)
        for relation in metadata.get("relations", {}).values():
            relation.update(module="", source_info=None)
            for restriction in relation.get("directly_related_user_types", []):
                restriction.setdefault("condition", "")
    return {"id": "01STORED0000000000000000", **served}


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
    """The drift this hook was built for: all nine missing relations were on types that already
    existed, so a comparison keyed on type NAMES alone would have reported no change forever."""
    stored = _model(warehouse=["owner", "reader"])
    assert needs_write(stored, _model(warehouse=["owner", "reader", "maintainer"]))


@pytest.mark.parametrize(
    ("stored", "desired"),
    [
        pytest.param(_warehouse(_READER_OR_PASS_GRANTS), _warehouse(_READER), id="a-rewrite-TIGHTENED"),
        pytest.param(_warehouse(_READER), _warehouse(_READER_OR_PASS_GRANTS), id="a-rewrite-WIDENED"),
        pytest.param(
            _warehouse(_READER),
            _warehouse(_READER, reader_types=[{"type": "user"}, {"type": "team", "relation": "member"}]),
            id="a-direct-TYPE-RESTRICTION-changed",
        ),
        pytest.param(
            _warehouse(_READER, condition="current_time < grant_time + grant_duration"),
            _warehouse(_READER, condition="current_time <= grant_time + grant_duration"),
            id="a-CONDITION-expression-changed",
        ),
    ],
)
def test_a_changed_RULE_under_an_unchanged_name_is_a_difference(stored: dict[str, Any], desired: dict[str, Any]) -> None:
    """Same types, same relation names, one rule different — the edit a name comparison reports as
    "already current". A tightened `can_read_data` left unwritten keeps granting through `pass_grants`
    a path the repo has removed, and nothing downstream can tell: every check still answers."""
    assert shape(stored) == shape(desired), "the premise: no type or relation NAME differs, only a rule"
    assert needs_write(stored, desired), "a rule edit under an unchanged relation name was reported as already written"


def test_the_shipped_model_AS_THE_STORE_SERVES_IT_is_not_rewritten() -> None:
    """THE NO-OP A BODY COMPARISON MUST KEEP. It is only sound if the store's default fills and its
    `id` compare as nothing — otherwise every upgrade writes and the store grows a model per release."""
    assert not needs_write(_as_openfga_serves_it(model_document()), model_document())


def test_the_shipped_model_with_one_rule_TIGHTENED_is_a_write() -> None:
    """The body drift over the real model and the served shape together, not only a fixture: keep the
    first branch of the shipped model's first union and compare it against the store's copy of the
    original."""
    shipped = model_document()
    tightened = copy.deepcopy(shipped)
    type_name, relation, union = next(
        (definition["type"], name, body["union"])
        for definition in tightened["type_definitions"]
        for name, body in (definition.get("relations") or {}).items()
        if "union" in body
    )
    definition = next(d for d in tightened["type_definitions"] if d["type"] == type_name)
    definition["relations"][relation] = union["child"][0]

    assert shape(tightened) == shape(shipped), "the premise: no type or relation NAME differs, only a rule"
    assert needs_write(_as_openfga_serves_it(shipped), tightened), f"tightening {type_name}#{relation} was reported as already written"


def test_the_shipped_model_is_readable_and_has_the_types_the_estate_reasons_about() -> None:
    """Without this the tests above could pass over a document that never loads."""
    shipped = shape(model_document())

    assert {"warehouse", "namespace", "table", "project"} <= set(shipped), f"the shipped model lost a core type: {sorted(shipped)}"
    assert "maintainer" in shipped["warehouse"], "the shipped model has no warehouse#maintainer — the relation whose absence wedged an upgrade"
