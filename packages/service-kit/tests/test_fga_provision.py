"""`fga.provision` must write the WHOLE model — the conditions block included.

The regression this pins: `model.json` gained a `conditions` block (time-boxed grants —
`non_expired_grant`) whose name several relations reference, and `provision` passed only
`schema_version` + `type_definitions` to `WriteAuthorizationModelRequest`. OpenFGA then rejects the
model ("condition non_expired_grant is undefined for relation reader"), the FGA client never builds,
and EVERY authorized route in every FGA-enabled service fail-closes 503 — the whole plane down, from
one silently dropped key.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, ClassVar

import pytest

from service_kit.governed import fga


class _Stores:
    stores: ClassVar[list[Any]] = []


class _Written:
    authorization_model_id = "model-1"


class _FakeClient:
    """Captures what `provision` writes. One instance per `async with` block."""

    requests: ClassVar[list[Any]] = []

    def __init__(self, configuration: Any) -> None:
        self._config = configuration

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_stores(self) -> _Stores:
        return _Stores()

    async def create_store(self, request: Any) -> Any:
        class _Created:
            id = "store-1"

        return _Created()

    async def write_authorization_model(self, request: Any) -> _Written:
        _FakeClient.requests.append(request)
        return _Written()


@pytest.mark.asyncio
async def test_provision_writes_the_conditions_block(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.requests = []
    monkeypatch.setattr(fga, "OpenFgaClient", _FakeClient)

    store_id, model_id = await fga.provision("http://fga:8080")

    assert (store_id, model_id) == ("store-1", "model-1")
    assert len(_FakeClient.requests) == 1
    request = _FakeClient.requests[0]
    model = fga.load_model()
    assert model.get("conditions"), "model.json no longer defines conditions — this test needs updating, not deleting"
    assert request.conditions == model["conditions"], (
        "provision dropped the conditions block — OpenFGA will 400 the model and every FGA-enabled service fail-closes 503"
    )
    assert request.type_definitions == model["type_definitions"]
    assert request.schema_version == model["schema_version"]


# ---------------------------------------------------------------------------------------------- #
# An unchanged model is not rewritten on every boot
# ---------------------------------------------------------------------------------------------- #
# OpenFGA has no "update" for a model: every `write_authorization_model` mints a new immutable version,
# and `provision`'s one non-test caller is the catalog's lifespan, so each boot that writes adds one.
# Measured against the live store 2026-09-13: 1,316 authorization model versions for a `model.json` that
# had changed a handful of times. An estate whose model id changes on every restart cannot pin one or tell
# a real model edit from a restart. OpenFGA does not store the model it was given: it fills in defaults
# (`metadata: null`, `relations: {}`, `module: ""`, `condition: ""`, `source_info: null`, `object: ""`,
# `[]`), so a naive equality check never fires. Dropping the fills while keeping the grammar's empty
# messages (`this`, `wildcard`) makes the two identical: measured on OpenFGA v1.18.3 2026-09-25, 26,247
# canonical characters each, over REST and over the SDK read. A model that genuinely differs still writes.


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


class _ExistingStores:
    class _Store:
        id = "store-1"
        name = "lance-catalog"
        created_at = "2026-01-01T00:00:00Z"

    stores: ClassVar[list[Any]] = [_Store()]


class _ModelStoreClient:
    requests: ClassVar[list[Any]] = []
    current: ClassVar[Any] = None
    reads: ClassVar[int] = 0
    empty_store: ClassVar[bool] = False

    def __init__(self, configuration: Any) -> None:
        self._config = configuration

    async def __aenter__(self) -> _ModelStoreClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_stores(self) -> Any:
        if _ModelStoreClient.empty_store:
            return type("_Empty", (), {"stores": []})()
        return _ExistingStores()

    async def create_store(self, _request: Any) -> Any:
        return type("_Created", (), {"id": "store-NEW"})()

    async def read_authorization_models(self, options: dict[str, Any] | None = None) -> Any:
        del options
        _ModelStoreClient.reads += 1
        held = [_ModelStoreClient.current] if _ModelStoreClient.current is not None else []
        return type("_Response", (), {"authorization_models": held, "continuation_token": ""})()

    async def write_authorization_model(self, request: Any) -> Any:
        _ModelStoreClient.requests.append(request)
        return type("_Written", (), {"authorization_model_id": "model-NEWLY-WRITTEN"})()


def _install(monkeypatch: pytest.MonkeyPatch, *, desired: dict[str, Any], stored: dict[str, Any] | None, empty_store: bool = False) -> None:
    _ModelStoreClient.requests = []
    _ModelStoreClient.reads = 0
    _ModelStoreClient.empty_store = empty_store
    _ModelStoreClient.current = _StoredModel(stored) if stored is not None else None
    monkeypatch.setattr(fga, "OpenFgaClient", _ModelStoreClient)
    monkeypatch.setattr(fga, "load_model", lambda: desired)


def _with_wildcard_assignee(model: dict[str, Any]) -> dict[str, Any]:
    """``model`` with ``role#assignee``'s ``[user]`` widened to ``[user:*]`` — only the ``wildcard: {}`` marker differs."""
    role = {**model["type_definitions"][1]}
    role["metadata"] = {"relations": {"assignee": {"directly_related_user_types": [{"type": "user", "wildcard": {}}, {"relation": "member", "type": "team"}]}}}
    return {**model, "type_definitions": [model["type_definitions"][0], role]}


@pytest.mark.asyncio
async def test_a_WILDCARD_only_write_names_the_relation_it_changed(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The write log's ``changed`` is how an operator tells what a new model version is about; a write
    caused only by a type restriction must name that relation rather than report ``changed=[]``."""
    _install(monkeypatch, desired=_with_wildcard_assignee(MODEL), stored=MODEL)

    with caplog.at_level(logging.INFO, logger=fga.__name__):
        await fga.provision("http://fga:8080")

    [written] = [record for record in caplog.records if record.getMessage() == "openfga_model_written"]
    assert written.__dict__["changed"] == ["role#assignee"], "the write was logged without the relation whose restriction it changed"


@pytest.mark.parametrize("name", ["this"])
def test_a_relation_NAMED_like_an_empty_message_is_not_kept_as_one(name: str) -> None:
    """``this`` and ``wildcard`` are grammar fields only inside a message. As a relation name they are a
    key the author chose, and the store's empty metadata fill under it is a default like any other: kept,
    it makes the served model differ from the authored one on every boot."""
    authored: dict[str, Any] = {
        "schema_version": "1.1",
        "type_definitions": [
            {"type": "user"},
            {
                "type": "doc",
                "relations": {"owner": {"this": {}}, name: {"computedUserset": {"relation": "owner"}}},
                "metadata": {"relations": {"owner": {"directly_related_user_types": [{"type": "user"}]}}},
            },
        ],
    }
    served = _as_openfga_stores_it(authored)
    served["type_definitions"][1]["metadata"]["relations"][name] = {"directly_related_user_types": [], "module": "", "source_info": None}

    assert fga.canonical_model(served) == fga.canonical_model(authored), f"the empty metadata fill under a relation named {name!r} was kept"


@pytest.mark.asyncio
async def test_an_unchanged_model_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GATE. 1,316 versions on the live store came from this write firing on every boot."""
    _install(monkeypatch, desired=MODEL, stored=MODEL)

    store_id, model_id = await fga.provision("http://fga:8080")

    assert _ModelStoreClient.requests == [], "an unchanged model must not mint a new version on every pod start"
    assert (store_id, model_id) == ("store-1", "model-EXISTING"), "the estate keeps answering with the model it already has"


@pytest.mark.asyncio
async def test_a_brand_new_store_is_modelled_without_a_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store minted moments ago holds nothing to compare against, so it must not pay the read — the
    same reason the narrowing guard skips it."""
    _install(monkeypatch, desired=MODEL, stored=None, empty_store=True)

    store_id, model_id = await fga.provision("http://fga:8080")

    assert _ModelStoreClient.reads == 0, "a first boot must not depend on a model that cannot exist yet"
    assert len(_ModelStoreClient.requests) == 1
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


def _sdk_assignee_model(*, wildcard: bool) -> Any:
    """``role#assignee: [user]`` or ``[user:*]`` as `read_latest_authorization_model` yields it: real SDK
    objects, whose ``wildcard`` is ``{}`` or ``None`` (measured on OpenFGA v1.18.3, 2026-09-25)."""
    from openfga_sdk.models.metadata import Metadata
    from openfga_sdk.models.relation_metadata import RelationMetadata
    from openfga_sdk.models.relation_reference import RelationReference
    from openfga_sdk.models.type_definition import TypeDefinition
    from openfga_sdk.models.userset import Userset

    restriction = RelationReference(type="user", wildcard={} if wildcard else None, condition="")
    role = TypeDefinition(
        type="role",
        relations={"assignee": Userset(this={})},
        metadata=Metadata(relations={"assignee": RelationMetadata(directly_related_user_types=[restriction], module="")}, module=""),
    )

    class _Stored:
        id = "model-EXISTING"
        schema_version = "1.1"
        conditions: ClassVar[dict[str, Any]] = {}
        type_definitions: ClassVar[list[Any]] = [TypeDefinition(type="user"), role]

    return _Stored()


def _authored_assignee_model(*, wildcard: bool) -> dict[str, Any]:
    restriction: dict[str, Any] = {"type": "user", "wildcard": {}} if wildcard else {"type": "user"}
    return {
        "schema_version": "1.1",
        "type_definitions": [
            {"type": "user"},
            {"type": "role", "relations": {"assignee": {"this": {}}}, "metadata": {"relations": {"assignee": {"directly_related_user_types": [restriction]}}}},
        ],
        "conditions": {},
    }


@pytest.mark.parametrize(("stored", "desired"), [(True, False), (False, True)], ids=["sdk-[user:*]-repo-[user]", "sdk-[user]-repo-[user:*]"])
def test_a_WILDCARD_toggle_survives_the_canonical_form_of_a_REAL_sdk_model(stored: bool, desired: bool) -> None:
    """The shape `provision` compares: an SDK ``wildcard={}`` against an authored plain restriction, and
    the reverse. Stripping every empty dict makes both pairs equal, and the boot keeps the store's rule."""
    assert fga.canonical_model(_sdk_assignee_model(wildcard=stored)) != fga.canonical_model(_authored_assignee_model(wildcard=desired))


def _schema_fields(matches: Callable[[str], bool]) -> set[str]:
    """Wire names of every field in the SDK's authorization-model schema whose declared type ``matches``."""
    import re

    from openfga_sdk import models

    found: set[str] = set()
    seen: set[str] = set()
    pending = ["AuthorizationModel"]
    while pending:
        klass = getattr(models, pending.pop())
        for attr, kind in (getattr(klass, "openapi_types", None) or {}).items():
            if matches(kind):
                found.add(klass.attribute_map[attr])
            inner = re.sub(r"^(?:list\[|dict\[str, )(.*)\]$", r"\1", kind)
            if hasattr(models, inner) and inner not in seen:
                seen.add(inner)
                pending.append(inner)
    return found


def _empty_message_fields() -> set[str]:
    """Every field the schema types as an empty message (``object``)."""
    return _schema_fields(lambda kind: kind == "object")


def test_every_EMPTY_MESSAGE_in_the_model_schema_survives_the_canonical_form() -> None:
    """An empty message is a value by its presence — `this` is a direct assignment, `wildcard` is `[x:*]` —
    so the canonical form must keep it while dropping the store's empty fills. Walked off the SDK's own
    schema, so a field a later SDK adds is covered without anyone remembering to list it."""
    fields = _empty_message_fields()
    assert {"this", "wildcard"} <= fields, f"the schema walk lost a known empty message: {sorted(fields)}"

    bare = fga.canonical_model({"schema_version": "1.1", "type_definitions": [{"type": "t"}]})
    dropped = [f for f in sorted(fields) if fga.canonical_model({"schema_version": "1.1", "type_definitions": [{"type": "t", f: {}}]}) == bare]
    assert not dropped, f"the canonical form strips these empty messages, so a model differing only by them reads as unchanged: {dropped}"


@pytest.mark.parametrize("wildcard", [True, False], ids=["[user:*]", "[user]"])
def test_an_unchanged_restriction_in_a_REAL_sdk_model_is_still_unchanged(wildcard: bool) -> None:
    """The no-op beside it: the SDK renders an absent wildcard as ``None`` and fills ``condition``/``module``
    with ``""``, and none of those may read as a change."""
    assert fga.canonical_model(_sdk_assignee_model(wildcard=wildcard)) == fga.canonical_model(_authored_assignee_model(wildcard=wildcard))
