"""A rename into another namespace moves a table only where grant authority allows, and where it inherits from moves with its name.

A table's grants are held by its uuid and so go with it (ADR 0173 §8). Under managed access a grant needs grant
authority, so a table that arrives with grants needs it on the destination, and one that leaves a managed scope needs it
on the table ([[LH-568]]): the door asks ``can_accept_moved_table`` on the destination namespace and ``can_move`` on the
table, which ``model.fga.yaml`` pins rule by rule. Here the door is driven on the real model, on a store of this test's
own ([[LH-235]]):

- a writer cannot bring a table into a managed namespace, and an owner inside one cannot take a table out;
- a rename that keeps its namespace asks on it what a create asks there;
- however a rename into another namespace ends, the table inherits from the namespace it is named in, and is named a
  child of no other. Its parent edge moves while the rename holds the table's location claim, in one write and before
  the name, and its ``child`` edge follows once the name is registered: a rename that is undone, by its backend or by
  an OpenFGA that loses an answer or stops answering, puts the edge back; a table between its two names is ended by
  neither namespace's removal, and one whose tuples a removal took as it moved arrives linked to the namespace it is
  in; a rename that loses the claim moves nothing, whichever rename won it; a move that went no further is finished
  by asking again; an edge that is not where the table's name says refuses the rename before anything moves; and a
  table a rival rename moved on as this one arrived is left where the rival took it.

Every tuple is the one a door writes, except the drift two cases start from, which no door produces, and the tuples
one case takes away as a racing removal would.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any, NamedTuple, cast

import pyarrow as pa
import pytest
from fastapi import Request
from lance_namespace import LanceNamespace, ServiceUnavailableError
from lance_namespace.errors import ErrorCode
from openfga_sdk import OpenFgaClient

from lakehouse import location_claims
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64())}))


class _RivalBefore:
    """The real namespace, landing ``rival`` once, just before the first call of ``method`` reaches it."""

    def __init__(self, ns: LanceNamespace, method: str, rival: Callable[[], object]) -> None:
        self._ns, self._method, self._rival, self._pending = ns, method, rival, True

    def __getattr__(self, name: str) -> Any:
        real = getattr(self._ns, name)
        if name != self._method:
            return real

        def _call(*args: object, **kwargs: object) -> object:
            # Disarmed before the rival sends anything: its own request reaches the backend through this too.
            if self._pending:
                self._pending = False
                self._rival()
            return real(*args, **kwargs)

        return _call


class _Writing:
    """The real OpenFGA client, its ``write`` replaced by one a case supplies."""

    def __init__(self, client: OpenFgaClient, write: Callable[..., Any]) -> None:
        self._client, self._write = client, write

    def __getattr__(self, name: str) -> Any:
        return self._write if name == "write" else getattr(self._client, name)


def _post(catalog: Any, path: str, caller: str, **kwargs: Any) -> Any:
    return catalog.client.post(path, headers={**catalog.bearer(caller), **kwargs.pop("headers", {})}, **kwargs)


def _ok(catalog: Any, path: str, caller: str = "alice", **kwargs: Any) -> dict[str, Any]:
    answer = _post(catalog, path, caller, **kwargs)
    assert answer.status_code == 200, f"{path}: {answer.status_code} {answer.text}"
    return answer.json()


def _create_table(catalog: Any, table: str, caller: str = "alice") -> dict[str, Any]:
    return _ok(catalog, f"/v1/table/{table}/create", caller, content=_ROWS, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})


def _object(catalog: Any, kind: str, name: str) -> str:
    """The object ``name``'s grants are held by: ``<kind>:<its uuid>``."""
    return f"{kind}:{_ok(catalog, f'/v1/{kind}/{name}/describe', json={})['properties']['rask.uuid']}"


def _edges(child: str, parent: str) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    """Both directions of the link that makes ``child`` inherit from ``parent``: its ``parent`` edge, then its ``child`` edge."""
    return (parent, "parent", child), (child, "child", parent)


def _move(catalog: Any, table: str, namespace: str, caller: str = "alice", *, name: str | None = None) -> Any:
    """Rename ``table`` into ``namespace``, under its own name unless ``name`` gives another."""
    return _post(catalog, f"/v1/table/{table}/rename", caller, json={"new_namespace_id": [namespace], "new_table_name": name or table.rsplit("$", 1)[-1]})


@pytest.fixture
def estate(governed_catalog: Any) -> Any:
    """alice's namespaces ``open`` and ``managed``, the second under managed access; bob writes in both, and erin reads
    ``open`` and dave ``managed``."""
    for namespace in ("open", "managed"):
        _ok(governed_catalog, f"/v1/namespace/{namespace}/create", json={})
        _ok(governed_catalog, f"/management/v1/namespace/{namespace}/access/grant", json={"user": "dex~bob", "relation": "writer"})
    _ok(governed_catalog, "/management/v1/namespace/open/access/grant", json={"user": "dex~erin", "relation": "reader"})
    _ok(governed_catalog, "/management/v1/namespace/managed/access/grant", json={"user": "dex~dave", "relation": "reader"})
    _ok(governed_catalog, "/management/v1/namespace/managed/managed-access/set", json={"enabled": True})
    return governed_catalog


@pytest.mark.parametrize(
    ("source", "destination", "refusal"),
    [
        pytest.param("open", "managed", "can_accept_moved_table required on namespace:managed", id="a-writer-brings-no-table-into-a-managed-namespace"),
        pytest.param("managed", "open", "can_move required on table:managed$t", id="an-owner-inside-a-managed-namespace-takes-no-table-out"),
    ],
)
def test_a_move_across_a_managed_boundary_is_refused_to_a_mover_without_grant_authority(estate: Any, source: str, destination: str, refusal: str) -> None:
    _create_table(estate, f"{source}$t", caller="bob")
    held = estate.store.tuples()

    refused = _move(estate, f"{source}$t", destination, caller="bob")

    assert (refused.status_code, refused.json()["code"], refused.json()["detail"]) == (403, ErrorCode.PERMISSION_DENIED.value, refusal), refused.text
    assert estate.store.tuples() == held, "the refused move wrote or removed a tuple"
    _ok(estate, f"/v1/table/{source}$t/describe", "bob", json={})


def test_a_rename_within_its_namespace_asks_what_a_create_asks_there(estate: Any) -> None:
    _create_table(estate, "open$t", caller="bob")
    _ok(estate, "/management/v1/namespace/open/access/revoke", json={"user": "dex~bob", "relation": "writer"})

    refused = _post(estate, "/v1/table/open$t/rename", "bob", json={"new_table_name": "u"})

    assert (refused.status_code, refused.json()["code"], refused.json()["detail"]) == (
        403,
        ErrorCode.PERMISSION_DENIED.value,
        "can_create_table required on namespace:open",
    ), refused.text
    _ok(estate, "/v1/table/open$t/describe", "bob", json={})


class _Scene(NamedTuple):
    """alice's table ``open$t`` as a rename into ``managed`` finds it: the estate and the table's object."""

    estate: Any
    monkeypatch: pytest.MonkeyPatch
    table: str

    def space(self, name: str) -> str:
        """The object namespace ``name``'s grants are held by."""
        return _object(self.estate, "namespace", name)


def _land_before(scene: _Scene, method: str, rival: Callable[[], object]) -> None:
    """Land ``rival`` once, just before the rename's first call of the backend's ``method``."""
    # The app module builds its settings at import, so it is imported once the fixture has pinned the environment.
    from catalog.api.dependencies import get_namespace
    from catalog.core.config import get_settings
    from catalog.main import app

    racing: list[_RivalBefore] = []

    async def _racing(request: Request) -> LanceNamespace:
        # One wrapper for the test: the rival's own request resolves its namespace through here too.
        if not racing:
            racing.append(_RivalBefore(await get_namespace(request, get_settings()), method, rival))
        return cast("LanceNamespace", racing[0])

    app.dependency_overrides[get_namespace] = _racing


def _the_destination_is_dropped_as_the_name_is_registered(scene: _Scene) -> None:
    _land_before(scene, "register_table", lambda: _ok(scene.estate, "/v1/namespace/managed/drop", json={}))


def _the_backend_does_not_detach_the_source(scene: _Scene) -> None:
    def _unanswered() -> None:
        # Whatever the backend raises here is compensated alike; a typed error is one the door answers.
        raise ServiceUnavailableError("the store did not answer")

    _land_before(scene, "deregister_table", _unanswered)


def _a_rival_rename_takes_the_claim_first(scene: _Scene, *, into: str) -> None:
    from catalog.services import table_claims

    hand_over, pending = table_claims.hand_over, [True]

    def _after_the_rival(*args: Any, **kwargs: Any) -> object:
        # Disarmed before the rival sends anything: its own rename hands the claim over through this too.
        if pending:
            pending.clear()
            moved = _move(scene.estate, "open$t", into, name="u")
            assert moved.status_code == 200, moved.text
        return hand_over(*args, **kwargs)

    scene.monkeypatch.setattr(table_claims, "hand_over", _after_the_rival)


def _answering(scene: _Scene, write: Callable[[Callable[..., Any], Any], Any]) -> None:
    """Have the catalog reach OpenFGA through ``write``, which is handed the client's own write and the request's body."""
    from catalog.api.dependencies import get_fga_client
    from catalog.main import app

    real = app.state.fga

    async def _write(body: Any, *args: object, **kwargs: object) -> object:
        return await write(lambda: real.write(body, *args, **kwargs), body)

    client = _Writing(real, _write)
    app.dependency_overrides[get_fga_client] = lambda: client


def _the_answer_to_the_move_is_lost(scene: _Scene) -> None:
    pending = [True]

    async def _lost_once(send: Callable[[], Any], body: Any) -> object:
        answer = await send()
        # The Write that moves the parent edge is the one that both deletes and writes: it lands, and nobody is told.
        if pending and body.writes and body.deletes:
            pending.clear()
            raise TimeoutError
        return answer

    _answering(scene, _lost_once)


def _openfga_gives_no_answer_as_the_child_edge_is_taken_away(scene: _Scene) -> None:
    async def _deletes_unanswered(send: Callable[[], Any], body: Any) -> object:
        # Taking the child edge away is the Write that only deletes: it never reaches the store.
        if body.deletes and not body.writes:
            raise TimeoutError
        return await send()

    _answering(scene, _deletes_unanswered)


def _its_tuples_are_revoked_as_its_name_moves(scene: _Scene) -> None:
    def _revoke() -> None:
        # What a removal that listed the table under the namespace it left does to it: every tuple on it goes.
        scene.estate.store.delete(*(held for held in scene.estate.store.tuples() if held[2] == scene.table))

    _land_before(scene, "register_table", _revoke)


def _a_rival_rename_moves_it_on_as_it_arrives(scene: _Scene) -> None:
    _ok(scene.estate, "/v1/namespace/third/create", json={})
    arriving, pending = (scene.table, "child", scene.space("managed")), [True]

    def _moved_on() -> None:
        moved = _move(scene.estate, "managed$t", "third")
        assert moved.status_code == 200, moved.text

    async def _rival_before_the_arrival(send: Callable[[], Any], body: Any) -> object:
        # The arrival's first Write names the table a child of the namespace it entered. The rival lands before it,
        # disarmed first: its own rename writes through this too.
        if pending and any((held.user, held.relation, held.object) == arriving for held in body.writes or ()):
            pending.clear()
            await asyncio.to_thread(_moved_on)
        return await send()

    _answering(scene, _rival_before_the_arrival)


def _an_earlier_rename_moved_the_parent_edge_and_went_no_further(scene: _Scene) -> None:
    scene.estate.store.delete(*_edges(scene.table, scene.space("open")))
    scene.estate.store.write(_edges(scene.table, scene.space("managed"))[0])


def _it_inherits_from_both_namespaces(scene: _Scene) -> None:
    scene.estate.store.write(_edges(scene.table, scene.space("managed"))[0])


@pytest.mark.parametrize(
    ("found", "answer", "named", "parents", "children", "readers"),
    [
        pytest.param(
            _the_destination_is_dropped_as_the_name_is_registered,
            (404, ErrorCode.NAMESPACE_NOT_FOUND.value),
            "open$t",
            {"open"},
            {"open"},
            {"erin"},
            id="a-table-between-its-names-outlives-the-namespace-it-was-entering-and-its-edge-is-put-back",
        ),
        pytest.param(
            _the_backend_does_not_detach_the_source,
            (503, ErrorCode.SERVICE_UNAVAILABLE.value),
            "open$t",
            {"open"},
            {"open"},
            {"erin"},
            id="a-rename-whose-source-is-not-detached-puts-the-edge-back",
        ),
        pytest.param(
            partial(_a_rival_rename_takes_the_claim_first, into="managed"),
            (409, ErrorCode.CONCURRENT_MODIFICATION.value),
            "managed$u",
            {"managed"},
            {"managed"},
            {"dave"},
            id="the-rename-that-wins-the-claim-moves-the-edge-and-the-one-that-loses-it-leaves-it-there",
        ),
        pytest.param(
            partial(_a_rival_rename_takes_the_claim_first, into="open"),
            (409, ErrorCode.CONCURRENT_MODIFICATION.value),
            "open$u",
            {"open"},
            {"open"},
            {"erin"},
            id="a-rename-that-loses-the-claim-has-moved-no-edge",
        ),
        pytest.param(
            _the_answer_to_the_move_is_lost,
            (503, ErrorCode.SERVICE_UNAVAILABLE.value),
            "open$t",
            {"open"},
            {"open"},
            {"erin"},
            id="a-move-whose-answer-is-lost-is-put-back",
        ),
        pytest.param(
            _openfga_gives_no_answer_as_the_child_edge_is_taken_away,
            (503, ErrorCode.SERVICE_UNAVAILABLE.value),
            "open$t",
            {"open"},
            {"open"},
            {"erin"},
            id="a-move-that-openfga-stops-answering-part-way-is-put-back",
        ),
        pytest.param(
            _its_tuples_are_revoked_as_its_name_moves,
            (200, None),
            "managed$t",
            {"managed"},
            {"managed"},
            {"dave"},
            id="a-table-whose-tuples-a-removal-took-as-it-moved-arrives-linked-to-the-namespace-it-is-in",
        ),
        pytest.param(
            _an_earlier_rename_moved_the_parent_edge_and_went_no_further,
            (200, None),
            "managed$t",
            {"managed"},
            {"managed"},
            {"dave"},
            id="a-move-that-went-no-further-is-finished-by-asking-again",
        ),
        pytest.param(
            _it_inherits_from_both_namespaces,
            (409, ErrorCode.CONCURRENT_MODIFICATION.value),
            "open$t",
            {"open", "managed"},
            {"open"},
            {"dave", "erin"},
            id="an-edge-that-is-not-where-the-name-says-refuses-the-rename",
        ),
        pytest.param(
            _a_rival_rename_moves_it_on_as_it_arrives,
            (200, None),
            "third$t",
            {"third"},
            {"third"},
            set(),
            id="a-table-a-rival-moved-on-as-it-arrived-is-left-where-the-rival-took-it",
        ),
    ],
)
def test_however_a_rename_into_another_namespace_ends_the_table_inherits_from_the_namespace_it_is_named_in(
    estate: Any,
    monkeypatch: pytest.MonkeyPatch,
    found: Callable[[_Scene], None],
    answer: tuple[int, int | None],
    named: str,
    parents: set[str],
    children: set[str],
    readers: set[str],
) -> None:
    from catalog.core.config import get_settings
    from catalog.services import table_claims

    location = str(_create_table(estate, "open$t")["location"])
    scene = _Scene(estate, monkeypatch, _object(estate, "table", "open$t"))
    found(scene)

    answered = _move(estate, "open$t", "managed")

    assert (answered.status_code, answered.json().get("code")) == answer, answered.text
    assert _object(estate, "table", named) == scene.table, f"the table is not the one named {named}"
    holder = location_claims.holder(table_claims.claim_store(get_settings()), location)
    assert holder is not None and holder.table == named, f"the table's location is claimed for {holder and holder.table}, not for the name it has"
    held = estate.store.tuples()
    assert {parent for parent, relation, child in held if relation == "parent" and child == scene.table} == {scene.space(name) for name in parents}
    assert {parent for child, relation, parent in held if relation == "child" and child == scene.table} == {scene.space(name) for name in children}
    assert {reader for reader in ("dave", "erin") if _post(estate, f"/v1/table/{named}/describe", reader, json={}).status_code == 200} == readers
