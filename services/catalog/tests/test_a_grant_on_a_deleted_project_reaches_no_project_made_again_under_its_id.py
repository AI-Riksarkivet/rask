"""A change decided on a project reaches no project made again under its id (ADR 0245, `specs/catalog/Grants.tla`).

Once a project is deleted another tenant may make one under the same id. A door resolves the id to its live creation,
asks its gate there, then writes there: a delete and a re-creation that run between the gate and the write leave the
write on the dead creation, which nothing resolves to, and the door, resolving the id again, answers the deleted
project's 404.

- A grant (`GrantsById`, `NoLeakAcrossIncarnations`): a person the deleted tenant's admin was granting holds no rung on
  another tenant's project.
- A revoke (`GrantsRevokeById`, `NoNarrowAcrossIncarnations`): the deleted tenant's admin takes no rung the new tenant
  granted.
- The answer (`GrantsAnswersUnread`, `GrantAnswersTruthfully`): no door answers 200 for a change on a project that is gone.
- A project being deleted (`GrantsResolvesDeleting`, `GrantAnswersTruthfully`): no door resolves it, so its admin changes nothing.
- A role's create (`GrantsChildReadFirst`, `ChildUnderLiveParent`): a role of the deleted tenant is listed, and
  grantable, in no project made again.

Each door is held at the step the trace names while the others run whole, and the harm is read over HTTP. Against the
catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding on a store of the test's own
([[LH-235]]).
"""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import Any

import pytest
from lance_namespace import ServiceUnavailableError

from catalog.api import fga_deps
from catalog.services import announcement_intents, projects, tenancy, tenancy_converge
from catalog.services import roles as role_records
from lakehouse.governed import fga
from lakehouse.governed.tenancy import UUID


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


def _project(catalog: Any) -> dict[str, Any]:  # noqa: ANN401 — the conftest's GovernedCatalog
    settings = catalog.client.app.state.settings
    record = tenancy.resolve(settings.registry_root, settings.storage_options(), "project", PROJECT)
    assert record is not None
    return record.record


def _holds(catalog: Any, rung: str) -> bool:  # noqa: ANN401 — the conftest's GovernedCatalog
    """Whether bob holds ``rung`` on the project the id names now."""
    obj = f"project:{_project(catalog)[UUID]}"
    held = catalog.client.post("/management/v1/access/check", json={"user": catalog.person(BOB), "relation": rung, "object": obj}, headers=catalog.bearer(ERIN))
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
    assert answered == [404], f"the {door} door answered {answered} for a change on a project deleted while it ran"


@pytest.mark.parametrize("door", [door for door, (*_, granting) in DOORS.items() if granting])
def test_a_project_being_deleted_is_changed_at_none_of_its_doors(acme: Any, door: str) -> None:  # noqa: ANN401 — the fixture's GovernedCatalog
    """A project marked ``deleting`` resolves at no door (`GrantsResolvesDeleting`, `GrantAnswersTruthfully`): its admin, whose
    tuples its delete has yet to revoke, is asked the door's gate on the object nobody holds, as for an id no project
    holds (review T4), so is refused 403 and writes nothing on it."""
    catalog, (_, _, _, opens, _) = acme, DOORS[door]
    settings, dying = catalog.client.app.state.settings, f"project:{_project(acme)[UUID]}"
    record = projects.get_project_record(settings.registry_root, settings.storage_options(), PROJECT) or {}
    delete = tenancy.DeleteRun.of("project", record)
    judged = catalog.client.portal.call(tenancy.judged, settings, delete)
    catalog.client.portal.call(partial(tenancy.mark, settings, delete, judged, by=catalog.person(ALICE)))

    answered = _change(catalog, door)

    held = catalog.client.post(
        "/management/v1/access/check", json={"user": catalog.person(BOB), "relation": opens, "object": dying}, headers=catalog.bearer(ERIN)
    )
    assert (answered, held.status_code, held.json().get("allowed")) == (403, 200, False), held.text


@pytest.mark.parametrize("held", ["before-its-record", "and-dying-after-its-record"])
def test_a_role_made_across_its_projects_delete_is_made_in_no_project_made_again_under_the_id(acme: Any, monkeypatch: pytest.MonkeyPatch, held: str) -> None:  # noqa: ANN401 — the fixture's GovernedCatalog
    """Held before its record is written, the role's create writes it naming acme's first creation, resolves the id
    again, finds another creation, ends its record and answers 404 (`ChildUnderLiveParent`). A create that dies after
    its record and before that resolve leaves it standing; it names the creation alice's gate was asked on, so erin's
    project lists no such role, and the converge tick ends it."""
    catalog, gate = acme, _Gate()
    real_record = role_records.create_role_record

    def _record(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 — create_role_record's own arguments
        gate.hold()
        real_record(*args, **kwargs)

    monkeypatch.setattr(role_records, "create_role_record", _record)
    if held == "and-dying-after-its-record":

        async def _dies(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401 — still's own arguments
            raise ServiceUnavailableError("the process died here")

        monkeypatch.setattr(tenancy, "still", _dies)
    roles = f"/management/v1/projects/{PROJECT}/roles"
    making, answered = _in_thread(lambda: catalog.client.post(roles, json={"name": "analysts"}, headers=catalog.bearer(ALICE)).status_code)
    assert gate.arrived.wait(WAIT), "the role's create never reached the held step"
    made = _delete_and_make_again(catalog)
    gate.release.set()
    making.join(WAIT)
    monkeypatch.undo()

    listed = catalog.client.get(roles, headers=catalog.bearer(ERIN))
    assert (made, listed.status_code) == ((200, 200), 200), listed.text
    erins = f"project:{_project(catalog)[UUID]}"
    edges = [t for t in catalog.store.tuples() if t[0] == erins and t[1] == "project" and t[2].startswith("role:")]
    assert answered == [503 if held == "and-dying-after-its-record" else 404], f"the role door answered {answered}"
    assert (listed.json()["roles"], edges) == ([], []), "erin's project holds alice's role"
    if held == "and-dying-after-its-record":
        settings = catalog.client.app.state.settings
        so = settings.storage_options()
        left = [r for r in role_records.every_stored_role(settings.registry_root, so)[0] if r.project == PROJECT]
        assert [r.project_uuid for r in left] not in ([], [_project(catalog)[UUID]]), "no record was left"
        converge = partial(
            tenancy_converge.converge, settings, catalog.client.app.state.fga, bound=timedelta(0), connections=catalog.client.app.state.connections
        )
        ended = catalog.client.portal.call(converge)
        retaken = catalog.client.post(roles, json={"name": "analysts"}, headers=catalog.bearer(ERIN))
        assert (len(ended.ended), retaken.status_code) == (1, 201), retaken.text


def _types_the_dsl_lets_name(kind: str) -> set[str]:
    """Every type whose relation the model's DSL lets name a ``kind`` object directly as its user, read from the text."""
    named: set[str] = set()
    current = ""
    for line in fga.load_model_dsl().splitlines():
        if match := re.match(r"^type (\w+)", line):
            current = match.group(1)
        elif (match := re.match(r"^\s+define \w+: \[([^\]]*)\]", line)) and kind in {entry.strip().split(" ")[0] for entry in match.group(1).split(",")}:
            named.add(current)
    return named


def test_a_projects_end_revokes_a_tuple_of_every_type_the_model_lets_name_it(acme: Any) -> None:  # noqa: ANN401 — the fixture's GovernedCatalog
    """Review T8: what names a project as its user is read from the authorization model, not kept by hand, so a type the
    model admits a project under later is revoked with the project too. A tuple of each such type, naming acme's creation,
    goes with acme's delete."""
    catalog = acme
    creation = f"project:{_project(catalog)[UUID]}"
    naming = fga_deps.naming("project")
    planted = [(creation, "project", f"{kind}:{uuid.uuid4().hex}") for kind in naming]
    catalog.store.write(*planted)

    deleted = catalog.client.delete(f"/management/v1/projects/{PROJECT}", headers=catalog.bearer(ALICE))

    assert (set(naming), deleted.status_code) == (_types_the_dsl_lets_name("project"), 200), deleted.text
    assert [t for t in planted if t in catalog.store.tuples()] == []
