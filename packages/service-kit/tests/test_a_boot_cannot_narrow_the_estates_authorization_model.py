"""A booting pod may ADD to the estate's authorization model. It may never take a relation away.

MEASURED ON THE LIVE ESTATE 2026-09-11, and it cost two sessions before anything named the cause. A
routine `make k3s-up` layered a stale `values-live-pins.yaml` over the live values and rolled the
catalog from `main-6fc3748c` back to `main-8c229296`. The older image booted, `provision` wrote ITS
bundled `model.json`, and the store's newest model lost `warehouse#event_stager` and
`warehouse#can_stage_events`. The visible symptom was `rask-bootstrap-admin` crash-looping on
`Invalid tuple … relation 'warehouse#event_stager' not found` — a job crash naming neither the model
nor the catalog — and it blocked `helm upgrade --wait-for-jobs` so the estate could not converge: every
re-apply booted the same stale catalog, which reverted the model again.

WHY THIS IS A CONTROL AND NOT A PREFERENCE. A missing relation does not DENY, it ERRORS: one `Check`
against the narrowed model answered `{"code":"validation_error","message":"object relation does not
exist"}` where the previous model answered `{"allowed":true}`. The fail-closed wrapper turns that into
"authorization service unavailable" for every caller of that door, so a narrowed model reads as an
outage of the authorization service rather than as a permissions change.

THE DIRECTION IS THE WHOLE RULE. `provision` exists so a `model.json` edit takes effect on boot, and
that stays true — adding a type, a relation or a condition still writes. What it must not do is let the
OLDEST pod win: a model that removes something the store already defines is a ROLLBACK, and image order
must not be load-bearing for who may do what. Refusing keeps the existing model id, so the estate goes
on answering with the model it already had.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from service_kit.governed import fga


def _typedef(type_name: str, relations: list[str]) -> Any:
    """The SDK's `TypeDefinition` shape: a `.type` and a `.relations` mapping. Only the KEYS matter to
    the comparison under test, so the values are placeholders."""
    return SimpleNamespace(type=type_name, relations=dict.fromkeys(relations, object()))


class _CurrentModel:
    id = "model-EXISTING"
    schema_version = "1.1"
    conditions: ClassVar[dict[str, Any]] = {}
    type_definitions: ClassVar[list[Any]] = [
        _typedef("warehouse", ["owner", "event_stager", "can_stage_events"]),
        _typedef("table", ["owner", "reader"]),
    ]


class _Stores:
    class _Store:
        id = "store-1"
        name = "lance-catalog"
        created_at = "2026-01-01T00:00:00Z"

    stores: ClassVar[list[Any]] = [_Store()]


class _FakeClient:
    """A store that ALREADY holds `_CurrentModel`, capturing any write `provision` attempts."""

    requests: ClassVar[list[Any]] = []
    current: ClassVar[Any] = _CurrentModel()

    def __init__(self, configuration: Any) -> None:
        self._config = configuration

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_stores(self) -> _Stores:
        return _Stores()

    async def read_latest_authorization_model(self) -> Any:
        class _Response:
            authorization_model = _FakeClient.current

        return _Response()

    async def write_authorization_model(self, request: Any) -> Any:
        _FakeClient.requests.append(request)

        class _Written:
            authorization_model_id = "model-NEWLY-WRITTEN"

        return _Written()


def _install(monkeypatch: pytest.MonkeyPatch, model: dict[str, Any]) -> None:
    _FakeClient.requests = []
    _FakeClient.current = _CurrentModel()
    monkeypatch.setattr(fga, "OpenFgaClient", _FakeClient)
    monkeypatch.setattr(fga, "load_model", lambda: model)


@pytest.mark.asyncio
async def test_a_boot_that_would_REMOVE_a_relation_keeps_the_model_the_store_already_has(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GATE. This is the live failure: an older image's model lacking `warehouse#event_stager`."""
    _install(
        monkeypatch,
        {
            "schema_version": "1.1",
            "type_definitions": [
                {"type": "warehouse", "relations": {"owner": {}}},
                {"type": "table", "relations": {"owner": {}, "reader": {}}},
            ],
            "conditions": {},
        },
    )

    store_id, model_id = await fga.provision("http://fga:8080")

    assert _FakeClient.requests == [], "a narrower model is a ROLLBACK — writing it lets the oldest pod decide who may do what"
    assert (store_id, model_id) == ("store-1", "model-EXISTING"), "the estate must go on answering with the model it already had"


@pytest.mark.asyncio
async def test_a_boot_that_would_REMOVE_a_whole_type_is_refused_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Losing a type takes every relation on it, so the coarser loss cannot be the one that slips through."""
    _install(
        monkeypatch,
        {
            "schema_version": "1.1",
            "type_definitions": [{"type": "warehouse", "relations": {"owner": {}, "event_stager": {}, "can_stage_events": {}}}],
            "conditions": {},
        },
    )

    _store_id, model_id = await fga.provision("http://fga:8080")

    assert _FakeClient.requests == []
    assert model_id == "model-EXISTING"


@pytest.mark.asyncio
async def test_a_model_that_only_ADDS_is_still_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control, and the reason this is not a guard that refuses everything.

    `provision` exists so a `model.json` edit takes effect on boot. A guard that also blocked additions
    would freeze the estate's authorization at whatever the first pod happened to ship.
    """
    _install(
        monkeypatch,
        {
            "schema_version": "1.1",
            "type_definitions": [
                {"type": "warehouse", "relations": {"owner": {}, "event_stager": {}, "can_stage_events": {}, "auditor": {}}},
                {"type": "table", "relations": {"owner": {}, "reader": {}}},
                {"type": "branch", "relations": {"owner": {}}},
            ],
            "conditions": {"non_expired_grant": {}},
        },
    )

    _store_id, model_id = await fga.provision("http://fga:8080")

    assert len(_FakeClient.requests) == 1, "adding a type or relation is an EDIT and must still take effect"
    assert model_id == "model-NEWLY-WRITTEN"


@pytest.mark.asyncio
async def test_a_store_with_no_model_yet_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """First boot of a fresh store. Nothing to narrow, so the comparison must not stand in the way."""
    _install(monkeypatch, {"schema_version": "1.1", "type_definitions": [{"type": "warehouse", "relations": {"owner": {}}}], "conditions": {}})
    _FakeClient.current = None

    _store_id, model_id = await fga.provision("http://fga:8080")

    assert len(_FakeClient.requests) == 1
    assert model_id == "model-NEWLY-WRITTEN"


@pytest.mark.asyncio
async def test_a_store_whose_model_cannot_be_READ_is_not_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """The teeth. A guard that answered "nothing to narrow" on a failed read could be waved through by
    a flaky OpenFGA — which is exactly when a boot storm is most likely.

    `_current_model` runs under the module's own posture (retry, then fail closed), so an unreadable
    store raises out of `provision` rather than writing. `auth_lifespan` turns that into no client and
    503 on the governed routes: an estate that cannot verify its own model does not get to overwrite it.
    """

    class _Unreadable(_FakeClient):
        async def read_latest_authorization_model(self) -> Any:
            raise ConnectionError("openfga unreachable")

    _install(monkeypatch, {"schema_version": "1.1", "type_definitions": [{"type": "warehouse", "relations": {"owner": {}}}], "conditions": {}})
    monkeypatch.setattr(fga, "OpenFgaClient", _Unreadable)

    with pytest.raises(Exception, match="authorization service unavailable"):
        await fga.provision("http://fga:8080", retry_attempts=1)

    assert _FakeClient.requests == [], "an unverifiable store must keep the model it has"
