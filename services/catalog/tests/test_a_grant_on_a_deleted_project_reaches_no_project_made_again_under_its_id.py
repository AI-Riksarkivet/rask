"""A change decided on a project reaches no project made again under its id (ADR 0241, `specs/catalog/Grants.tla`).

OpenFGA names a project by its id (`project:<id>`), and once a project is deleted another tenant may make one under the
same id. A door asks its caller's authority, then writes: a delete that runs between the two ends what it reads, and
the door's write lands after it, onto the project another estate admin made again.

- A grant (`GrantsUnfenced`): a person the deleted tenant's admin was granting holds a rung on another tenant's project.
- A revoke (`GrantsRevokeUnfenced`): the deleted tenant's admin takes a rung the new tenant granted.
- A role's create (`GrantsEdgeUnfenced`): a role of the deleted tenant is listed, and grantable, in the new tenant.
- A grant run whole while the delete is held before its retire (`GrantsRetireLast`): what the grant wrote before the
  retire is drained with everything else, and nothing outlives the delete.

Each door is held at the step the trace names while the others run whole, and the harm is read over HTTP. Against the
catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding on a store of the test's own
([[LH-235]]).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import pytest

from catalog.api.v1.endpoints import roles as role_doors
from catalog.services import announcement_intents
from catalog.services import roles as role_records
from lakehouse.governed import fga


PROJECT = "acme"
#: acme's first admin, who deletes it; the estate admin who makes it again; the person the change is about.
ALICE, ERIN, BOB = "alice", "erin", "bob"
WAIT = 30
MEMBERS, ACCESS = f"/management/v1/projects/{PROJECT}/members", f"/management/v1/projects/{PROJECT}/access"


class _Gate:
    def __init__(self) -> None:
        self.arrived, self.release = threading.Event(), threading.Event()

    def hold(self) -> None:
        if not self.arrived.is_set():
            self.arrived.set()
            assert self.release.wait(WAIT), "the held step was never released"


#: Each door changing a rung on a project: how it is asked, the rung, the relation that rung opens, and whether it grants.
DOORS = {
    "members-admin": ("PUT", MEMBERS, "admin", "can_administer", True),
    "members-member": ("PUT", MEMBERS, "member", "member", True),
    "access-decider": ("POST", f"{ACCESS}/grant", "decider", "can_decide_access", True),
    "members-revoke-member": ("DELETE", MEMBERS, "member", "member", False),
    "access-revoke-decider": ("POST", f"{ACCESS}/revoke", "decider", "can_decide_access", False),
}


def _holds(catalog: Any, rung: str) -> bool:  # noqa: ANN401 — the conftest's GovernedCatalog
    held = catalog.client.post(
        "/management/v1/access/check", json={"user": catalog.person(BOB), "relation": rung, "object": f"project:{PROJECT}"}, headers=catalog.bearer(ERIN)
    )
    assert held.status_code == 200, held.text
    return bool(held.json()["allowed"])


def _change(catalog: Any, door: str, *, by: str = ALICE) -> int:  # noqa: ANN401 — the conftest's GovernedCatalog
    method, path, rung, _, _ = DOORS[door]
    return catalog.client.request(method, path, json={"user": f"dex~{BOB}", "relation": rung}, headers=catalog.bearer(by)).status_code


def _grant_of(door: str) -> str:
    """The granting door of the rung ``door`` changes."""
    return {"members-revoke-member": "members-member", "access-revoke-decider": "access-decider"}.get(door, door)


def _delete_and_make_again(catalog: Any) -> tuple[int, int]:  # noqa: ANN401 — the conftest's GovernedCatalog
    deleted = catalog.client.delete(f"/management/v1/projects/{PROJECT}", headers=catalog.bearer(ALICE))
    remade = catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(ERIN))
    return deleted.status_code, remade.status_code


def _in_thread(call: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    answered: list[Any] = []
    thread = threading.Thread(target=lambda: answered.append(call()))
    thread.start()
    return thread, answered


@pytest.fixture
def acme(governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    catalog = governed_catalog
    catalog.store.write((catalog.person(ALICE), "admin", "estate:rask"), (catalog.person(ERIN), "admin", "estate:rask"))
    assert catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(ALICE)).status_code == 200
    return catalog


@pytest.mark.parametrize("door", list(DOORS))
def test_a_change_held_across_its_projects_delete_reaches_no_project_made_again_under_the_id(acme: Any, monkeypatch: pytest.MonkeyPatch, door: str) -> None:  # noqa: ANN401 — the fixture's GovernedCatalog
    catalog, (_, _, rung, opens, granting) = acme, DOORS[door]
    if not granting:
        assert _change(catalog, _grant_of(door)) == 200
    gate, real_file = _Gate(), announcement_intents.file

    def _file(*args: Any, door: str, **kwargs: Any) -> str:  # noqa: ANN401 — announcement_intents.file's own arguments
        if door in {"member_grant", "access_grant", "member_revoke", "access_revoke"}:
            gate.hold()
        return real_file(*args, door=door, **kwargs)

    monkeypatch.setattr(announcement_intents, "file", _file)
    changing, answered = _in_thread(lambda: _change(catalog, door))
    assert gate.arrived.wait(WAIT), "the change never reached its write"
    made = _delete_and_make_again(catalog)
    if not granting:
        # The new tenant grants the same rung, which the deleted tenant's revoke must not take.
        assert _change(catalog, _grant_of(door), by=ERIN) == 200
    gate.release.set()
    changing.join(WAIT)

    assert made == (200, 200)
    assert _holds(catalog, opens) is not granting, f"the door answered {answered}, and erin's project {'holds' if granting else 'lost'} bob's {rung}"


@pytest.mark.parametrize("held", ["before-its-edge", "between-its-edge-and-its-record"])
def test_a_role_made_across_its_projects_delete_is_made_in_no_project_made_again_under_the_id(acme: Any, monkeypatch: pytest.MonkeyPatch, held: str) -> None:  # noqa: ANN401 — the fixture's GovernedCatalog
    """Held before its `project` edge is written, the role's create finds its fence drained (`GrantsEdgeUnfenced`); held
    between the edge and its record, the delete's drain revokes the edge, which names the project as subject
    (`GrantsDeleteMissesEdges`), and the create, finding its edge gone once its record is written, ends the record."""
    catalog, gate = acme, _Gate()
    if held == "before-its-edge":
        real_mint = role_doors._mint_role

        async def _mint(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — _mint_role's own arguments
            await asyncio.to_thread(gate.hold)
            return await real_mint(*args, **kwargs)

        monkeypatch.setattr(role_doors, "_mint_role", _mint)
    else:
        real_record = role_records.create_role_record

        def _record(*args: Any) -> None:  # noqa: ANN401 — create_role_record's own arguments
            gate.hold()
            real_record(*args)

        monkeypatch.setattr(role_records, "create_role_record", _record)
    roles = f"/management/v1/projects/{PROJECT}/roles"
    making, answered = _in_thread(lambda: catalog.client.post(roles, json={"name": "analysts"}, headers=catalog.bearer(ALICE)).status_code)
    assert gate.arrived.wait(WAIT), "the role's create never reached the held step"
    made = _delete_and_make_again(catalog)
    gate.release.set()
    making.join(WAIT)

    listed = catalog.client.get(roles, headers=catalog.bearer(ERIN))
    assert (made, listed.status_code) == ((200, 200), 200), listed.text
    edges = [t for t in catalog.store.tuples() if t[0] == f"project:{PROJECT}" and t[1] == "project" and t[2].startswith("role:")]
    assert answered == [409], f"the role door answered {answered}"
    assert (listed.json()["roles"], edges) == ([], []), "erin's project holds alice's role"


def test_a_grant_run_whole_while_the_delete_is_held_before_its_retire_outlives_no_delete(acme: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401 — the fixture's GovernedCatalog
    catalog, gate, real_retire = acme, _Gate(), fga.retire_tokens

    async def _retire(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 — retire_tokens's own arguments
        await asyncio.to_thread(gate.hold)
        await real_retire(*args, **kwargs)

    monkeypatch.setattr(fga, "retire_tokens", _retire)
    deleting, answered = _in_thread(lambda: catalog.client.delete(f"/management/v1/projects/{PROJECT}", headers=catalog.bearer(ALICE)).status_code)
    assert gate.arrived.wait(WAIT), "the delete never reached its retire"
    granted = _change(catalog, "members-member")
    gate.release.set()
    deleting.join(WAIT)
    remade = catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(ERIN))

    assert (granted, answered, remade.status_code) == (200, [200], 200), remade.text
    assert not _holds(catalog, "member"), "bob's grant, made while the delete was held, holds on erin's project"
