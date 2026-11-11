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
  neither namespace's removal; a rename that loses the claim moves nothing, whichever rename won it; a move that went
  no further, its rename dead as it reached the backend, is finished by asking again, and so is the move of a table
  left inheriting from both namespaces; and a rival rename landed as this one arrives moves nothing, since the
  rename holds the claim until the table's edges are whole ([[LH-586]]);
- a door that finds the table standing under the name it is asked for grants its caller nothing: a registration made
  again is refused and writes no tuple at all, and a create that keeps the table, or its owner's Overwrite, writes only
  the link of a table that inherits from nowhere, so none puts the readers of the namespace a rename left back on it,
  and a create that loses the race to the name keeps the winner's table and owns none of it;
- a rename landed inside a namespace cascade's removal of its table is refused, and the cascade removes what it
  listed, into the trash where the drop is recoverable; a table renamed out before its turn it leaves whole, and a
  table made since under a listed name it refuses.

Every tuple is the one a door writes, except the drift two arrangements start from, which no door produces: a table
inheriting from both namespaces, and one inheriting from none.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple, cast
from urllib.parse import urlsplit

import lance
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


def _a_rival_rename_lands_as_it_arrives(scene: _Scene) -> None:
    _ok(scene.estate, "/v1/namespace/third/create", json={})
    arriving, pending = (scene.table, "child", scene.space("managed")), [True]

    async def _rival_before_the_arrival(send: Callable[[], Any], body: Any) -> object:
        # The arrival's Write names the table a child of the namespace it entered. The rival lands before it, when the
        # name is registered and the rename still holds the claim; disarmed first, as its own rename writes through this.
        if pending and any((held.user, held.relation, held.object) == arriving for held in body.writes or ()):
            pending.clear()
            await asyncio.to_thread(_move, scene.estate, "managed$t", "third")
        return await send()

    _answering(scene, _rival_before_the_arrival)


class _Died(BaseException):
    """The process serving a rename is gone: nothing the rename had left to do runs, as after a kill."""


def _a_rename_into_managed_dies_as_it_reaches_the_backend(scene: _Scene) -> None:
    """Leave ``open$t`` as a rename into ``managed`` leaves it when its process dies between moving the parent edge
    and its first backend call: named in ``open``, inheriting from ``managed``, its claim handed to a name nothing holds."""
    from catalog.services import dataplane

    rename = dataplane.rename_table

    def _die() -> None:
        raise _Died

    def _until_it_dies(*args: Any, **kwargs: Any) -> object:
        # Nothing in the rename catches the death, so none of its undo runs; only the request has to end, which an
        # error the app answers does without stopping the test's client.
        try:
            return rename(*args, **kwargs)
        except _Died:
            raise ServiceUnavailableError("the catalog serving the rename died") from None

    scene.monkeypatch.setattr(dataplane, "rename_table", _until_it_dies)
    _land_before(scene, "deregister_table", _die)
    died = _move(scene.estate, "open$t", "managed")
    assert died.status_code == 503, died.text


def _an_earlier_rename_moved_the_parent_edge_and_went_no_further(scene: _Scene) -> None:
    _a_rename_into_managed_dies_as_it_reaches_the_backend(scene)
    # The dead rename's claim names a table that never came to be, and every door waits its lease out.
    scene.monkeypatch.setattr(location_claims, "LEASE", timedelta(0))


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
            (200, None),
            "managed$t",
            {"managed"},
            {"managed"},
            {"dave"},
            id="a-table-inheriting-from-both-namespaces-is-left-inheriting-from-the-one-it-enters",
        ),
        pytest.param(
            _a_rival_rename_lands_as_it_arrives,
            (200, None),
            "managed$t",
            {"managed"},
            {"managed"},
            {"dave"},
            id="a-rival-rename-landed-as-it-arrives-moves-nothing",
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


def _a_rename_left_it_half_moved(scene: _Scene) -> set[tuple[str, str, str]]:
    """The table is named in ``open`` and inherits from ``managed``, so it lacks no link: the empty set."""
    _a_rename_into_managed_dies_as_it_reaches_the_backend(scene)
    return set()


def _it_inherits_from_nowhere(scene: _Scene) -> set[tuple[str, str, str]]:
    """The table's link to the namespace it is named in is gone, as a create that died before its seed leaves it: the
    link it lacks."""
    link = set(_edges(scene.table, scene.space("open")))
    scene.estate.store.delete(*link)
    return link


def _a_create_lands_first(scene: _Scene) -> set[tuple[str, str, str]]:
    """alice creates ``open$r`` between bob's probe of that name and his declaration of it, as a racing create lands, so
    bob's create finds her table standing and keeps it. It lacks nothing."""
    from catalog.services import dataplane

    declare, pending = dataplane.declare_table, [True]

    def _declared_after_the_winner(*args: Any, **kwargs: Any) -> object:
        # Disarmed before the winner sends anything: its own create declares through this too.
        if pending:
            pending.clear()
            _create_table(scene.estate, "open$r")
        return declare(*args, **kwargs)

    scene.monkeypatch.setattr(dataplane, "declare_table", _declared_after_the_winner)
    return set()


def _nothing(_scene: _Scene, _lacking: set[tuple[str, str, str]]) -> set[tuple[str, str, str]]:
    return set()


def _the_link_it_lacks(_scene: _Scene, lacking: set[tuple[str, str, str]]) -> set[tuple[str, str, str]]:
    return lacking


def _the_winners_own(scene: _Scene, _lacking: set[tuple[str, str, str]]) -> set[tuple[str, str, str]]:
    """What the create that won the race wrote, read once the door answered: alice owns ``open$r``, linked to ``open``."""
    won = _object(scene.estate, "table", "open$r")
    return {*_edges(won, scene.space("open")), ("user:dex~alice", "owner", won)}


_ARROW = {"content": _ROWS, "headers": {"Content-Type": ARROW_STREAM_MEDIA_TYPE}}
_REGISTERED_AGAIN = ("/v1/table/open$t/register", {"json": {"location": "held"}}, (409, ErrorCode.TABLE_ALREADY_EXISTS.value))


#: Each door that finds a table standing under the name it is asked for, and so brings none into being. A registration
#: made again is refused, and a refused request writes no tuple, whatever the table lacks. A create that keeps the table
#: and its owner's Overwrite answer 200, and write the link of a table that inherits from nowhere and nothing else: no
#: second parent on a table a rename left half-moved, and no ownership for the caller, not even where the table it keeps
#: was made by a create that won the race to the name. bob only writes in `open`.
@pytest.mark.parametrize(
    ("caller", "path", "sent", "answer", "found", "adds"),
    [
        pytest.param("bob", *_REGISTERED_AGAIN, _a_rename_left_it_half_moved, _nothing, id="a-registration-made-again-of-a-table-a-rename-left-half-moved"),
        pytest.param("bob", *_REGISTERED_AGAIN, _it_inherits_from_nowhere, _nothing, id="a-registration-made-again-of-a-table-that-inherits-from-nowhere"),
        pytest.param(
            "bob",
            "/v1/table/open$t/create?mode=exist_ok",
            _ARROW,
            (200, None),
            _it_inherits_from_nowhere,
            _the_link_it_lacks,
            id="a-create-keeping-a-table-that-inherits-from-nowhere",
        ),
        pytest.param(
            "bob",
            "/v1/table/open$r/create?mode=exist_ok",
            _ARROW,
            (200, None),
            _a_create_lands_first,
            _the_winners_own,
            id="a-create-keeping-the-table-a-racing-create-made",
        ),
        pytest.param(
            "alice",
            "/v1/table/open$t/create?mode=overwrite",
            _ARROW,
            (200, None),
            _a_rename_left_it_half_moved,
            _nothing,
            id="an-overwrite-of-a-table-a-rename-left-half-moved",
        ),
    ],
)
def test_a_door_that_finds_a_table_standing_grants_nothing_and_a_refused_registration_writes_no_tuple(
    estate: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caller: str,
    path: str,
    sent: dict[str, Any],
    answer: tuple[int, int | None],
    found: Callable[[_Scene], set[tuple[str, str, str]]],
    adds: Callable[[_Scene, set[tuple[str, str, str]]], set[tuple[str, str, str]]],
) -> None:
    # A registered table, since only a registered table's location can be registered again.
    lance.write_dataset(pa.ipc.open_stream(_ROWS).read_all(), str(tmp_path / "held"), data_storage_version="2.2", enable_stable_row_ids=True)
    _ok(estate, "/v1/table/open$t/register", json={"location": "held"})
    scene = _Scene(estate, monkeypatch, _object(estate, "table", "open$t"))
    lacking = found(scene)
    held = estate.store.tuples()

    answered = _post(estate, path, caller, **sent)

    now = estate.store.tuples()
    # No door puts a reader of `open` back on a table its mover shut them out of, and none makes its caller an owner.
    assert ((answered.status_code, answered.json().get("code")), now - held, held - now) == (answer, adds(scene, lacking), set()), answered.text


def _naming(estate: Any, obj: str) -> set[tuple[str, str, str]]:
    """Every tuple the store holds that names ``obj``, as its user or as its object."""
    return {held for held in estate.store.tuples() if obj in (held[0], held[2])}


def _as_its_first_row_goes(scene: _Scene, rival: Callable[[], object]) -> None:
    _land_before(scene, "deregister_table", rival)


def _once_it_has_listed_its_tables(scene: _Scene, rival: Callable[[], object]) -> None:
    """Land ``rival`` once, after the cascade listed its subtree and before its first step: as it reads how long its
    warehouse keeps a drop."""
    from catalog.services import trash_doors

    grace, pending = trash_doors.drop_grace_days, [True]

    async def _after_the_rival(*args: Any, **kwargs: Any) -> float:
        if pending:
            pending.clear()
            await asyncio.to_thread(rival)
        return await grace(*args, **kwargs)

    scene.monkeypatch.setattr(trash_doors, "drop_grace_days", _after_the_rival)


class _Rival(NamedTuple):
    """What another request does to a table a cascade listed, while the cascade runs."""

    #: Act on ``open$<name>`` and answer what the door said: its refusal, else its last step's answer.
    act: Callable[[Any, str], tuple[int, int | None]]
    #: The namespace a table it acted on is named in afterwards.
    leaves_it_in: str
    #: Whether the table then standing there is the one the cascade listed, or one made since under its name.
    the_listed_table: bool


def _rename_into_managed(estate: Any, name: str) -> tuple[int, int | None]:
    answered = _move(estate, f"open${name}", "managed")
    return answered.status_code, answered.json().get("code")


def _drop_and_make_again(estate: Any, name: str) -> tuple[int, int | None]:
    dropped = _post(estate, f"/v1/table/open${name}/drop", "alice", json={})
    answered = _post(estate, f"/v1/table/open${name}/create", "alice", **_ARROW) if dropped.status_code == 200 else dropped
    return answered.status_code, answered.json().get("code")


_RENAMED_OUT = _Rival(_rename_into_managed, "managed", the_listed_table=True)
_MADE_AGAIN = _Rival(_drop_and_make_again, "open", the_listed_table=False)
_DONE = (200, None)
_REFUSED = (409, ErrorCode.CONCURRENT_MODIFICATION.value)


#: A cascade lists its tables once and removes them one at a time, and while it runs another request acts on both of
#: its tables. As its first row is about to go, the table being removed is held by the cascade and refuses, and the
#: other, whose turn has not come, does not. Renamed into ``managed``, it has left: a destroying cascade resolves each
#: name as its turn comes, so it finds the name gone and finishes, and a recoverable one resolved every name before its
#: first write, so it answers the change at that table's turn. Dropped and made again, its name holds a table the
#: cascade never listed, and so held to no protection check, and the cascade refuses. Renamed out before a recoverable
#: cascade resolves its names, both tables have left and it detaches neither.
@pytest.mark.parametrize(
    ("trash_grace_days", "lands", "rival", "answers", "cascade_answer"),
    [
        pytest.param("0", _as_its_first_row_goes, _RENAMED_OUT, [_DONE, _REFUSED], _DONE, id="a-destroying-cascade"),
        pytest.param("7", _as_its_first_row_goes, _RENAMED_OUT, [_DONE, _REFUSED], _REFUSED, id="a-recoverable-cascade"),
        pytest.param(
            "7", _once_it_has_listed_its_tables, _RENAMED_OUT, [_DONE, _DONE], _DONE, id="a-recoverable-cascade-whose-tables-left-before-it-resolved-them"
        ),
        pytest.param("0", _as_its_first_row_goes, _MADE_AGAIN, [_DONE, _REFUSED], _REFUSED, id="a-destroying-cascade-one-of-whose-names-another-table-took"),
    ],
)
def test_a_cascade_removes_each_listed_table_under_its_fence_and_none_that_left_or_was_made_since(
    estate: Any,
    monkeypatch: pytest.MonkeyPatch,
    trash_grace_days: str,
    lands: Callable[[_Scene, Callable[[], object]], None],
    rival: _Rival,
    answers: list[tuple[int, int | None]],
    cascade_answer: tuple[int, int | None],
) -> None:
    from catalog.core.config import get_settings

    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", trash_grace_days)
    get_settings.cache_clear()
    recoverable = trash_grace_days != "0"
    stored = {name: Path(urlsplit(str(_create_table(estate, f"open${name}")["location"])).path) for name in ("a", "t")}
    assert all(path.exists() for path in stored.values()), "a table holds no object, so its removal would prove nothing"
    listed = {name: _object(estate, "table", f"open${name}") for name in stored}
    held = {name: _naming(estate, obj) for name, obj in listed.items()}
    answered: dict[str, tuple[int, int | None]] = {}

    def _act_on_both() -> None:
        for name in listed:
            answered[name] = rival.act(estate, name)

    lands(_Scene(estate, monkeypatch, listed["t"]), _act_on_both)

    dropped = _post(estate, "/v1/namespace/open/drop", "alice", json={"behavior": "Cascade"})

    assert sorted(answered.values(), key=str) == answers, answered
    left = [name for name in listed if answered[name] == _DONE]
    taken = [name for name in listed if name not in left]
    names = [f"{namespace}${name}" for namespace in ("open", "managed") for name in listed]
    standing = {name: _object(estate, "table", name) for name in names if _post(estate, f"/v1/table/{name}/describe", "alice", json={}).status_code == 200}
    tasks = {name: estate.client.get(f"/management/v1/table/open${name}/tasks", headers=estate.bearer("alice")) for name in listed}
    tracked = [name for name, answer in tasks.items() if answer.status_code == 200 and answer.json()]
    # A table the cascade was removing went with it: its row and bytes when it destroys, and its grants once the cascade
    # has finished; into the trash with its bytes and grants when it is recoverable. One the other request reached
    # first stands where that request left it, linked to that namespace and to no other and owned by alice, with no trash
    # record against it and no deletion tracked for it: the table the cascade listed when it was renamed out, and
    # another, which the cascade removes nothing of, when it was made again.
    entered = _object(estate, "namespace", rival.leaves_it_in)
    now = {name: str(standing.get(f"{rival.leaves_it_in}${name}")) for name in left}
    cascade_finished_destroying = cascade_answer == _DONE and not recoverable
    assert (
        (dropped.status_code, dropped.json().get("code")),
        list(standing),
        {name: _naming(estate, obj) for name, obj in now.items()},
        {name: obj == listed[name] for name, obj in now.items()},
        {name: _naming(estate, listed[name]) for name in taken},
        {name: path.exists() for name, path in stored.items()},
        tracked,
        dropped.json().get("transaction_id"),
    ) == (
        cascade_answer,
        [f"{rival.leaves_it_in}${name}" for name in left],
        {name: {*_edges(obj, entered), ("user:dex~alice", "owner", obj)} for name, obj in now.items()},
        dict.fromkeys(left, rival.the_listed_table),
        {name: set() if cascade_finished_destroying else held[name] for name in taken},
        {name: (name in left and rival.the_listed_table) or (name in taken and recoverable) for name in listed},
        taken if recoverable else [],
        None,
    ), dropped.text
