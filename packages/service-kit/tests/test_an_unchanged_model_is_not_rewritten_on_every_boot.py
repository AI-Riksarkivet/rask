"""`provision` writes the authorization model on EVERY boot, even when nothing about it changed.

OpenFGA has no "update" for a model: every `write_authorization_model` mints a new immutable version.
`provision` has exactly one non-test caller — the catalog's lifespan — so each catalog boot adds one.
(A second publisher is not merely rare: `tests/unit/test_only_one_service_may_publish_the_authorization_
model.py` fails the suite on any service other than `catalog`.) Measured against the live store 2026-09-13: **1,316 authorization model versions**, paged out of
`GET /stores/{id}/authorization-models` — for a `model.json` that has changed a handful of times.

WHY THAT IS NOT MERELY UNTIDY. The store's "latest" model is whichever pod booted last, and that is the
value [[LH-139]]'s narrowing guard reads to decide whether a boot is a rollback. Churn is the substrate
that defect lived on: an estate whose model id changes on every restart cannot pin one, cannot tell a
real model edit from a restart, and gives an operator 1,316 candidates to diff when asking what the
authorization model actually says.

THE COMPARISON HAD TO BE PROVEN REACHABLE BEFORE IT WAS WORTH WRITING. OpenFGA does not store the model
it was given: it fills in defaults, so the stored form is ~48% larger (36,482 chars vs 24,579 for this
estate's model) and a naive equality check would never fire — a control that cannot fire. The fills are
`metadata: null`, `relations: {}`, `module: ""`, `condition: ""` and `source_info: null`. Dropping
null/empty values makes the two byte-identical: verified against the live store's newest model and this
repo's `model.json`, 20,310 characters each.

So the skip is conditioned on that canonical form, and a model that genuinely differs still writes —
which is what keeps `provision`'s whole reason for existing ("a `model.json` edit takes effect on boot")
intact.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from service_kit.governed import fga


MODEL: dict[str, Any] = {
    "schema_version": "1.1",
    "type_definitions": [
        {"type": "user"},
        {
            "type": "role",
            "relations": {"assignee": {"this": {}}},
            "metadata": {"relations": {"assignee": {"directly_related_user_types": [{"type": "user"}, {"relation": "member", "type": "team"}]}}},
        },
    ],
    "conditions": {},
}


def _as_openfga_stores_it(model: dict[str, Any]) -> dict[str, Any]:
    """``model`` in the shape OpenFGA hands back — the default fills, measured against the live store."""
    types: list[dict[str, Any]] = []
    for definition in model["type_definitions"]:
        stored: dict[str, Any] = {"type": definition["type"], "relations": definition.get("relations") or {}, "metadata": None}
        metadata = definition.get("metadata")
        if metadata:
            relations = {
                name: {
                    "directly_related_user_types": [{"condition": "", **entry} for entry in body.get("directly_related_user_types", [])],
                    "module": "",
                    "source_info": None,
                }
                for name, body in metadata["relations"].items()
            }
            stored["metadata"] = {"module": "", "relations": relations, "source_info": None}
        types.append(stored)
    return {"schema_version": model["schema_version"], "type_definitions": types, "conditions": model.get("conditions") or {}}


class _StoredModel:
    """What `read_latest_authorization_model` yields: an SDK object exposing `to_dict()`."""

    id = "model-EXISTING"

    def __init__(self, model: dict[str, Any]) -> None:
        self._model = _as_openfga_stores_it(model)
        self.schema_version = self._model["schema_version"]
        self.type_definitions = self._model["type_definitions"]
        self.conditions = self._model["conditions"]

    def to_dict(self) -> dict[str, Any]:
        return dict(self._model)


class _Stores:
    class _Store:
        id = "store-1"
        name = "lance-catalog"
        created_at = "2026-01-01T00:00:00Z"

    stores: ClassVar[list[Any]] = [_Store()]


class _FakeClient:
    requests: ClassVar[list[Any]] = []
    current: ClassVar[Any] = None
    reads: ClassVar[int] = 0
    empty_store: ClassVar[bool] = False

    def __init__(self, configuration: Any) -> None:
        self._config = configuration

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_stores(self) -> Any:
        if _FakeClient.empty_store:
            return type("_Empty", (), {"stores": []})()
        return _Stores()

    async def create_store(self, _request: Any) -> Any:
        return type("_Created", (), {"id": "store-NEW"})()

    async def read_latest_authorization_model(self) -> Any:
        _FakeClient.reads += 1
        return type("_Response", (), {"authorization_model": _FakeClient.current})()

    async def write_authorization_model(self, request: Any) -> Any:
        _FakeClient.requests.append(request)
        return type("_Written", (), {"authorization_model_id": "model-NEWLY-WRITTEN"})()


def _install(monkeypatch: pytest.MonkeyPatch, *, desired: dict[str, Any], stored: dict[str, Any] | None, empty_store: bool = False) -> None:
    _FakeClient.requests = []
    _FakeClient.reads = 0
    _FakeClient.empty_store = empty_store
    _FakeClient.current = _StoredModel(stored) if stored is not None else None
    monkeypatch.setattr(fga, "OpenFgaClient", _FakeClient)
    monkeypatch.setattr(fga, "load_model", lambda: desired)


def test_the_canonical_form_ignores_the_defaults_openfga_fills_in() -> None:
    """The reachability proof, as a unit: the stored shape and the authored one must canonicalise equal,
    or the skip below is a branch that never runs."""
    assert fga.canonical_model(_as_openfga_stores_it(MODEL)) == fga.canonical_model(MODEL)


def test_a_genuinely_different_model_does_not_canonicalise_equal() -> None:
    """The other half — otherwise the comparison would be 'always equal', which skips real edits."""
    widened = {**MODEL, "type_definitions": [*MODEL["type_definitions"], {"type": "team", "relations": {"member": {"this": {}}}}]}

    assert fga.canonical_model(_as_openfga_stores_it(MODEL)) != fga.canonical_model(widened)


@pytest.mark.asyncio
async def test_an_unchanged_model_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GATE. 1,316 versions on the live store came from this write firing on every boot."""
    _install(monkeypatch, desired=MODEL, stored=MODEL)

    store_id, model_id = await fga.provision("http://fga:8080")

    assert _FakeClient.requests == [], "an unchanged model must not mint a new version on every pod start"
    assert (store_id, model_id) == ("store-1", "model-EXISTING"), "the estate keeps answering with the model it already has"


@pytest.mark.asyncio
async def test_a_widened_model_is_still_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """`provision` exists so a `model.json` edit takes effect on boot; the skip must not cost that."""
    widened = {**MODEL, "type_definitions": [*MODEL["type_definitions"], {"type": "team", "relations": {"member": {"this": {}}}}]}
    _install(monkeypatch, desired=widened, stored=MODEL)

    _store_id, model_id = await fga.provision("http://fga:8080")

    assert len(_FakeClient.requests) == 1, "a real model change must still be written"
    assert model_id == "model-NEWLY-WRITTEN"


@pytest.mark.asyncio
async def test_a_narrowing_model_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The skip sits beside the narrowing guard and must not swallow it: a model that REMOVES a relation
    is a rollback, and it is neither unchanged nor writable."""
    narrowed = {"schema_version": "1.1", "type_definitions": [{"type": "user"}], "conditions": {}}
    _install(monkeypatch, desired=narrowed, stored=MODEL)

    _store_id, model_id = await fga.provision("http://fga:8080")

    assert _FakeClient.requests == []
    assert model_id == "model-EXISTING"


@pytest.mark.asyncio
async def test_a_brand_new_store_is_modelled_without_a_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store minted moments ago holds nothing to compare against, so it must not pay the read — the
    same reason the narrowing guard skips it."""
    _install(monkeypatch, desired=MODEL, stored=None, empty_store=True)

    store_id, model_id = await fga.provision("http://fga:8080")

    assert _FakeClient.reads == 0, "a first boot must not depend on a model that cannot exist yet"
    assert len(_FakeClient.requests) == 1
    assert (store_id, model_id) == ("store-NEW", "model-NEWLY-WRITTEN")


# ---------------------------------------------------------------------------------------------- #
# The shape the store really hands back
# ---------------------------------------------------------------------------------------------- #


def test_the_canonical_form_matches_a_REAL_sdk_model_not_just_a_double() -> None:
    """THE DOUBLE ABOVE CANNOT CATCH THE ONE THING THAT MATTERS: key spelling.

    `_StoredModel.to_dict()` returns the authored dict verbatim, so it agrees with `model.json` by
    construction. The store does not. `openfga_sdk`'s generated models keep Python attribute names and
    their `to_dict()` renders those unless asked to serialize:

        attr = self.attribute_map.get(attr, attr) if serialize else attr   # openfga_sdk/models/userset.py

    so `to_dict()` yields `computed_userset` while `model.json` — and the wire — say `computedUserset`.
    A canonicaliser reading the unserialized form can never equal the authored model, which makes the
    skip it feeds a branch that cannot fire.

    This test builds REAL SDK objects for exactly that reason. It is the check the double was standing
    in for and could not perform.
    """
    from openfga_sdk.models.object_relation import ObjectRelation
    from openfga_sdk.models.type_definition import TypeDefinition
    from openfga_sdk.models.userset import Userset

    authored = {
        "schema_version": "1.1",
        "type_definitions": [
            {"type": "user"},
            {"type": "doc", "relations": {"can_read": {"computedUserset": {"relation": "owner"}}}},
        ],
        "conditions": {},
    }

    class _Stored:
        id = "model-EXISTING"
        schema_version = "1.1"
        conditions: ClassVar[dict[str, Any]] = {}
        type_definitions: ClassVar[list[Any]] = [
            TypeDefinition(type="user"),
            TypeDefinition(type="doc", relations={"can_read": Userset(computed_userset=ObjectRelation(relation="owner"))}),
        ]

    assert fga.canonical_model(_Stored()) == fga.canonical_model(authored), (
        "the stored model and the authored one must canonicalise equal, or the unchanged-model skip is a "
        "branch that never runs and every boot mints another version"
    )
