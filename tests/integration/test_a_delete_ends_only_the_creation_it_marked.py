"""A delete, a change and a declaration act only on the creation they were decided on (ADR 0245, review T1, T2, T3, T7, T9;
`specs/catalog/Grants.tla`).

A project's or a warehouse's id is freed by its delete for another tenant's creation, so everything keyed by the id is
changed only under a read that found the creation it was decided on, and a delete refused before its point of no return
leaves its object live:

- A delete held across its creation's end and the id made again ends nothing of the tenant holding the id now
  (`GrantsDeleteTwiceById`, `DeleteEndsOnlyItsOwn`); nor does a protection change held the same way.
- A delete refused leaves its object live, before its mark or after it (`GrantsRefusalStuck`, `RefusedDeleteLeavesLive`),
  and nothing is written beneath a warehouse its delete has marked.
- A project's delete reads its warehouses again after its mark (U7, `GrantsCreatingUncounted`).
- A grant git declares is held on every creation its id names (`GrantsDeclaredStale`, `DeclaredOnLive`).
- A create that lost the race for its id made nothing.

Each interleaving holds one request at the step the trace names while the others run whole, and the harm is read over
HTTP. Driven on the deployed shape: the real app with authorization on (a loopback Dex, a real OpenFGA store of the test's
own) over an S3 store.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable
from typing import Any

import anyio.to_thread
import pytest

from catalog.api import fga_deps
from catalog.services import projects, tenancy, warehouses
from lakehouse.governed import fga
from lakehouse.governed.tenancy import Kind


IDP, ROOT, PROJECT, WAREHOUSE = "dex", "estate:rask", "acme", "wh-a"
#: A pipeline's service account, the kind of principal a declaration grants.
PIPELINE = "kubernetes~system:serviceaccount:default:pipeline"
WAIT = 30


def _admins(estate: Any, *people: str) -> list[dict[str, str]]:  # noqa: ANN401 — the conftest's Estate
    for person in people:
        estate.store.write((fga.user_object(f"{IDP}~{person}"), "admin", ROOT))
    return [estate.bearer(person) for person in people]


def _held(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, *, call: int = 1) -> tuple[threading.Event, threading.Event]:  # noqa: ANN401 — the module whose function is held
    """Hold the ``call``-th call of ``owner.name`` until released; a coroutine function is held off the event loop, which
    keeps serving the other requests meanwhile."""
    arrived, release, calls = threading.Event(), threading.Event(), [0]
    real = getattr(owner, name)

    def _arrive() -> bool:
        calls[0] += 1
        if calls[0] != call:
            return False
        arrived.set()
        return True

    def _wait() -> None:
        assert release.wait(WAIT), f"the held {name} was never released"

    if inspect.iscoroutinefunction(real):

        async def _held_async(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the held function's own arguments and answer
            if _arrive():
                await anyio.to_thread.run_sync(_wait)
            return await real(*args, **kwargs)

        monkeypatch.setattr(owner, name, _held_async)
    else:

        def _held_sync(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the held function's own arguments and answer
            if _arrive():
                _wait()
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, name, _held_sync)
    return arrived, release


def _in_thread(call: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    answered: list[Any] = []
    thread = threading.Thread(target=lambda: answered.append(call()))
    thread.start()
    return thread, answered


def _settings(estate: Any) -> Any:  # noqa: ANN401 — the conftest's Estate and the catalog's settings
    return estate.client.app.state.settings


def _live(estate: Any, kind: Kind, object_id: str) -> tenancy.Tenant:  # noqa: ANN401 — the conftest's Estate
    """The live creation ``object_id`` names; the test fails where none does."""
    settings = _settings(estate)
    live = tenancy.resolve(settings.registry_root, settings.storage_options(), kind, object_id)
    assert live is not None, f"no live {kind} {object_id!r}"
    return live


def _project_and_warehouse(estate: Any, headers: dict[str, str]) -> None:  # noqa: ANN401 — the conftest's Estate
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=headers).status_code == 200
    made = estate.client.post("/management/v1/warehouses", json={"id": WAREHOUSE, "project": PROJECT, "bucket": f"{WAREHOUSE}-bucket"}, headers=headers)
    assert made.status_code == 200, made.text


def test_a_projects_delete_held_across_its_end_ends_nothing_of_the_project_made_again(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    """Review T2: alice's delete of acme is held at its revoke; a second delete of hers runs whole; erin makes acme again
    and a role in it; the held delete, released, finds the id naming erin's creation and stops (404): erin's project, her
    role and her admin grant stand."""
    alice, erin = _admins(estate, "alice", "erin")
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    arrived, release = _held(monkeypatch, fga_deps, "end_object")
    deleting, answered = _in_thread(lambda: estate.client.delete(f"/management/v1/projects/{PROJECT}", headers=alice).status_code)
    assert arrived.wait(WAIT), "the first delete never reached its revoke"
    second = estate.client.delete(f"/management/v1/projects/{PROJECT}", headers=alice)
    remade = estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=erin)
    role = estate.client.post(f"/management/v1/projects/{PROJECT}/roles", json={"name": "analysts"}, headers=erin)
    release.set()
    deleting.join(WAIT)

    listed = estate.client.get(f"/management/v1/projects/{PROJECT}/roles", headers=erin)
    assert (second.status_code, remade.status_code, role.status_code) == (200, 200, 201), (second.text, remade.text, role.text)
    assert answered == [404], f"the held delete answered {answered} for a creation that ended"
    assert [r["name"] for r in listed.json()["roles"]] == ["analysts"], listed.text
    assert (fga.user_object(f"{IDP}~erin"), "admin", _live(estate, "project", PROJECT).obj) in estate.store.tuples()


@pytest.mark.parametrize("kind", ["project", "warehouse"])
def test_a_protection_change_held_across_its_objects_end_lands_on_no_object_made_again(estate: Any, monkeypatch: pytest.MonkeyPatch, kind: Kind) -> None:  # noqa: ANN401
    """Review T2: a protection change decided on one creation is written under a read that finds that creation, so one
    held across the object's delete and re-creation answers 404 and leaves the object made again unprotected."""
    (alice,) = _admins(estate, "alice")
    _project_and_warehouse(estate, alice)
    owner, name, path = (
        (projects, "set_project_protection", f"/management/v1/projects/{PROJECT}")
        if kind == "project"
        else (warehouses, "set_warehouse_protection", f"/management/v1/warehouses/{WAREHOUSE}")
    )
    if kind == "project":
        assert estate.client.delete(f"/management/v1/warehouses/{WAREHOUSE}", headers=alice).status_code == 200
    arrived, release = _held(monkeypatch, owner, name)
    protecting, answered = _in_thread(lambda: estate.client.post(f"{path}/protection", json={"protected": True}, headers=alice).status_code)
    assert arrived.wait(WAIT), "the protection change never reached its write"
    deleted = estate.client.delete(path, headers=alice)
    body = {"id": PROJECT} if kind == "project" else {"id": WAREHOUSE, "project": PROJECT, "bucket": f"{WAREHOUSE}-bucket"}
    remade = estate.client.post(f"/management/v1/{kind}s", json=body, headers=alice)
    release.set()
    protecting.join(WAIT)

    assert (deleted.status_code, remade.status_code) == (200, 200), (deleted.text, remade.text)
    assert answered == [404], f"the held protection change answered {answered}"
    assert _live(estate, kind, PROJECT if kind == "project" else WAREHOUSE).record.get("protected") is None, "the object made again is protected"


def _protect_a_namespace(estate: Any, headers: dict[str, str]) -> None:  # noqa: ANN401 — the conftest's Estate
    assert estate.client.post(f"/v1/namespace/{WAREHOUSE}$guarded/create", json={}, headers=headers).status_code == 200
    protected = estate.client.post(f"/management/v1/namespace/{WAREHOUSE}$guarded/protection", json={"protected": True}, headers=headers)
    assert protected.status_code == 200, protected.text


def _usable(estate: Any, headers: dict[str, str]) -> tuple[int, int, int]:  # noqa: ANN401 — the conftest's Estate
    """Whether the warehouse is live to its doors: a grant on it, a namespace made in it, and a table declared in one."""
    granted = estate.client.post(f"/management/v1/warehouse/{WAREHOUSE}/access/grant", json={"user": f"{IDP}~bob", "relation": "reader"}, headers=headers)
    made = estate.client.post(f"/v1/namespace/{WAREHOUSE}$after/create", json={}, headers=headers)
    declared = estate.client.post(f"/v1/table/{WAREHOUSE}$after$t/declare", json={}, headers=headers)
    return granted.status_code, made.status_code, declared.status_code


def test_a_cascade_refused_by_a_protected_namespace_leaves_its_warehouse_live(estate: Any) -> None:  # noqa: ANN401
    """Review T3: the subtree's protection is judged before the mark, so the refusal (409) leaves the warehouse live: its
    grant door, a namespace's create and a table's declare in it all answer as before."""
    (alice,) = _admins(estate, "alice")
    _project_and_warehouse(estate, alice)
    _protect_a_namespace(estate, alice)

    refused = estate.client.delete(f"/management/v1/warehouses/{WAREHOUSE}", params={"cascade": "true"}, headers=alice)

    assert (refused.status_code, refused.json()["code"]) == (409, 3), refused.text
    assert _usable(estate, alice) == (200, 200, 200)


def test_nothing_is_written_beneath_a_warehouse_its_delete_has_marked(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    """Review T3: a declare into a warehouse being deleted is refused (409, code 14): a table landing there would be left
    behind, or taken by, the delete emptying it. Its names are still read meanwhile."""
    (alice,) = _admins(estate, "alice")
    _project_and_warehouse(estate, alice)
    assert estate.client.post(f"/v1/namespace/{WAREHOUSE}$ns/create", json={}, headers=alice).status_code == 200
    from catalog.api.v1.endpoints import warehouses as doors

    arrived, release = _held(monkeypatch, doors, "_namespaces_in", call=2)
    deleting, answered = _in_thread(
        lambda: estate.client.delete(f"/management/v1/warehouses/{WAREHOUSE}", params={"cascade": "true"}, headers=alice).status_code
    )
    assert arrived.wait(WAIT), "the delete never read its namespaces after its mark"
    declared = estate.client.post(f"/v1/table/{WAREHOUSE}$ns$t/declare", json={}, headers=alice)
    described = estate.client.post(f"/v1/namespace/{WAREHOUSE}$ns/describe", json={}, headers=alice)
    release.set()
    deleting.join(WAIT)

    assert ((declared.status_code, declared.json().get("code")), described.status_code, answered) == ((409, 14), 200, [200]), declared.text


def test_a_projects_delete_reads_its_warehouses_again_after_its_mark(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    """Review T7, U7's after-mark read: a warehouse made to completion while the project's delete is held at its mark is
    found by the read after the mark, which refuses the delete (409) and puts the project back live, with its warehouse
    under it (`ChildUnderLiveParent`)."""
    (alice,) = _admins(estate, "alice")
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    arrived, release = _held(monkeypatch, projects, "mark_deleting")
    deleting, answered = _in_thread(lambda: estate.client.delete(f"/management/v1/projects/{PROJECT}", headers=alice).status_code)
    assert arrived.wait(WAIT), "the delete never reached its mark"
    made = estate.client.post("/management/v1/warehouses", json={"id": WAREHOUSE, "project": PROJECT, "bucket": f"{WAREHOUSE}-bucket"}, headers=alice)
    release.set()
    deleting.join(WAIT)

    assert (made.status_code, answered) == (200, [409]), made.text
    assert estate.client.get(f"/management/v1/projects/{PROJECT}", headers=alice).json()["warehouses"][0]["id"] == WAREHOUSE


@pytest.mark.parametrize(("kind", "relation"), [("warehouse", "writer"), ("project", "decider")])
def test_a_declared_grant_is_held_on_the_object_made_again_under_its_id(estate: Any, kind: Kind, relation: str) -> None:  # noqa: ANN401
    """Review T1: git declares a grant on an id alice made at the doors; a tick writes it on that creation; alice deletes
    the object and makes it again; the next tick writes it on the new creation, the ledger's record of the old one
    forgotten, and names nothing among its failures."""
    (alice,) = _admins(estate, "alice")
    _project_and_warehouse(estate, alice)
    object_id = PROJECT if kind == "project" else WAREHOUSE
    estate.declare(grants=[{"object": f"{kind}:{object_id}", "relation": relation, "principal": PIPELINE}])
    first, before = estate.tick(), _live(estate, kind, object_id).obj
    assert estate.client.delete(f"/management/v1/warehouses/{WAREHOUSE}", headers=alice).status_code == 200
    if kind == "project":
        assert estate.client.delete(f"/management/v1/projects/{PROJECT}", headers=alice).status_code == 200
        assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    else:
        remade = estate.client.post("/management/v1/warehouses", json={"id": WAREHOUSE, "project": PROJECT, "bucket": f"{WAREHOUSE}-bucket"}, headers=alice)
        assert remade.status_code == 200, remade.text

    again = estate.tick()

    after = _live(estate, kind, object_id).obj
    assert ((first.grants_written, first.failed), after != before) == ((1, []), True)
    assert ((again.grants_written, again.failed), (fga.user_object(PIPELINE), relation, after) in estate.store.tuples()) == ((1, []), True)


def test_a_project_create_that_lost_the_race_for_its_id_made_nothing(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    """Review T9: erin's create of acme reads no record, alice's create makes it, and erin's mint is refused by the store:
    erin's create answers the id that exists (409, code 2) and seeds nothing, so no admin of hers stands on any creation."""
    alice, erin = _admins(estate, "alice", "erin")
    arrived, release = _held(monkeypatch, projects, "create_project_record")
    racing, answered = _in_thread(lambda: estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=erin))
    assert arrived.wait(WAIT), "erin's create never reached its mint"
    monkeypatch.undo()
    won = estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice)
    release.set()
    racing.join(WAIT)

    lost = answered[0]
    assert (won.status_code, lost.status_code, lost.json()["code"]) == (200, 409, 2), lost.text
    assert [t for t in estate.store.tuples() if t[0] == fga.user_object(f"{IDP}~erin") and t[2].startswith("project:")] == []
    assert (fga.user_object(f"{IDP}~alice"), "admin", _live(estate, "project", PROJECT).obj) in estate.store.tuples()
