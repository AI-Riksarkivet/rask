"""Every branch and tag MUTATION announces itself on the control lane; the reads stay silent.

[[LH-056]]. The ref plane is where `published` lives, and `table_published` already announced that one
tag moving. The other five ref mutations moved in silence: a console holding a branch or tag list had
no way to learn one appeared, moved or vanished, and a reader that polls discovers a deleted tag by a
failing read.

UNTARGETED, by the rule the estate codified in
`tests/unit/test_control_action_three_file_contract.py`: "a control event is targeted when it changes
what a specific person may do or must do, not when it changes an object". A branch or tag is an object,
so these five name nobody and stay out of notifications' `NAMED_ACTIONS` -- they reach a feed, not a
person.

DRIVEN THROUGH THE ROUTES against a real `dir` namespace and real pylance refs, because the subject is
whether the DOOR emits. A unit test on the emitter would pass with no route calling it, which is the
state this file was written to end.

THE OBJECT ID IS THE TABLE, not the ref. A consumer joins events by `object_id` -- the catalog's own
`evict_stale_bindings` does exactly that -- so a branch event has to carry the same id a publication for
that table carries, with the ref itself in `extra`. Naming the branch there instead would give every
branch its own object and join nothing.

`fga.canonical_object_id(segments, ...)` is used for consistency with every other table emitter here, not
because it corrects anything: it is `delimiter.join(segments)` against `parse_identifier`'s split, so it
round-trips the raw path parameter byte-for-byte. Checked, because the difference looked load-bearing and
is not.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import connect

from catalog.api.dependencies import ControlEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints import branches as branch_door
from catalog.api.v1.endpoints import tags as tag_door
from catalog.services.dataplane import create_table
from service_kit.lakehouse.ns_errors import install_problem_handlers


lance = pytest.importorskip("lance")

_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"
#: ROOT-LEVEL on purpose: a child namespace needs an existing `__manifest` dataset, and this
#: file's subject is the ref doors, not namespace bootstrap.
TABLE = ["alpha"]
TABLE_PATH = "alpha"


def _ipc() -> bytes:
    table = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace, a runtime-only type
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    create_table(namespace, {}, TABLE, _ipc(), mode="create")
    return namespace


@pytest.fixture
def emitted() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def app(ns, emitted: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:  # noqa: ANN001
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(branch_door.router)
    application.include_router(tag_door.router)

    # `registry_root` is not decoration: the delete doors read a protection record before they
    # destroy anything ([[LH-056]]), and a double without it fails inside the handler rather than
    # at the assertion — the shape "a double must carry the whole signature".
    settings = SimpleNamespace(delimiter="$", storage_options=lambda: {}, fga_enabled=False, registry_root=str(tmp_path / "control"))
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[NamespaceDep.__metadata__[0].dependency] = lambda: ns
    application.dependency_overrides[StorageOptionsDep.__metadata__[0].dependency] = lambda: {}
    application.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: SimpleNamespace(sub=_SUB)
    application.dependency_overrides[ControlEmitterDep.__metadata__[0].dependency] = lambda: None

    async def _record(_emitter: Any, **kwargs: Any) -> None:
        emitted.append(kwargs)

    # `raising=False` so a RED run reports the MISSING EMIT rather than an AttributeError about the
    # patch target — the assertion is the message worth reading.
    for module in (branch_door, tag_door):
        monkeypatch.setattr(module, "emit_control", _record, raising=False)
    yield application


def _actions(emitted: list[dict[str, Any]]) -> list[str]:
    return [str(call.get("action")) for call in emitted]


def test_the_harness_reaches_the_refs(app: FastAPI) -> None:
    """Without this, every assertion below could pass on a door that 500s."""
    with TestClient(app) as client:
        created = client.post(f"/v1/table/{TABLE_PATH}/branches/create", json={"id": TABLE, "name": "staging"})
        listed = client.post(f"/v1/table/{TABLE_PATH}/branches/list", json={"id": TABLE})
    assert created.status_code == 200, created.text
    assert listed.status_code == 200, listed.text


def test_a_branch_mutation_announces_itself(app: FastAPI, emitted: list[dict[str, Any]]) -> None:
    with TestClient(app) as client:
        assert client.post(f"/v1/table/{TABLE_PATH}/branches/create", json={"id": TABLE, "name": "staging"}).status_code == 200
        assert client.post(f"/v1/table/{TABLE_PATH}/branches/delete", json={"id": TABLE, "name": "staging"}).status_code == 200

    assert _actions(emitted) == ["table_branch_created", "table_branch_deleted"], (
        f"a branch appeared and vanished with no control event, so a console holding a branch list learns neither: {emitted}"
    )
    assert [c["extra"]["branch"] for c in emitted] == ["staging", "staging"], (
        f"the event does not name the branch it is about, which makes it unactionable: {emitted}"
    )


def test_a_tag_mutation_announces_itself_including_the_MOVE(app: FastAPI, emitted: list[dict[str, Any]]) -> None:
    """The move is the one a consumer cannot otherwise detect: the name survives, the version changes."""
    with TestClient(app) as client:
        assert client.post(f"/v1/table/{TABLE_PATH}/tags/create", json={"id": TABLE, "tag": "ready", "version": 1}).status_code == 200
        assert client.post(f"/v1/table/{TABLE_PATH}/tags/update", json={"id": TABLE, "tag": "ready", "version": 1}).status_code == 200
        assert client.post(f"/v1/table/{TABLE_PATH}/tags/delete", json={"id": TABLE, "tag": "ready"}).status_code == 200

    assert _actions(emitted) == ["table_tag_created", "table_tag_updated", "table_tag_deleted"], (
        f"a tag moved with no control event; `table_published` is the only ref mutation anyone hears: {emitted}"
    )


def test_a_READ_of_the_ref_plane_stays_SILENT(app: FastAPI, emitted: list[dict[str, Any]]) -> None:
    """A list or a resolve changes nothing, and an event per read is a feed nobody can use."""
    with TestClient(app) as client:
        client.post(f"/v1/table/{TABLE_PATH}/branches/create", json={"id": TABLE, "name": "staging"})
        emitted.clear()
        assert client.post(f"/v1/table/{TABLE_PATH}/branches/list", json={"id": TABLE}).status_code == 200
        assert client.post(f"/v1/table/{TABLE_PATH}/tags/list", json={"id": TABLE}).status_code == 200

    assert emitted == [], f"a READ emitted a control event: {emitted}"


def test_the_event_is_about_the_TABLE_with_the_ref_in_extra(app: FastAPI, emitted: list[dict[str, Any]]) -> None:
    """The join key is the table, so these land beside a publication for the same table.

    IF THIS IS RED: the door is naming the wrong object. A branch is not an object of its own -- the
    Lance Namespace spec defines three TABLE-scoped branch ops and no branch resource -- so the event
    is about the table and the ref rides in `extra`.
    """
    with TestClient(app) as client:
        client.post(f"/v1/table/{TABLE_PATH}/branches/create", json={"id": TABLE, "name": "staging"})

    assert [c["object_id"] for c in emitted] == [f"table:{TABLE_PATH}"], (
        f"the ref event does not name the table it happened to, so it joins no other event for it: {emitted}"
    )
    assert [c["object_type"] for c in emitted] == ["table"], f"the object type is not `table`: {emitted}"
    assert emitted[0]["extra"]["branch"] == "staging", "the ref itself must be in `extra`, not in the object id"
