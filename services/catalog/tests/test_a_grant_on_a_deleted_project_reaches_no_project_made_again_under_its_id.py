"""A grant decided on a project lands on no project made again under its id (ADR 0241, `specs/catalog/Grants.tla`).

OpenFGA names a project by its id (`project:<id>`), and once a project is deleted another tenant may make one under the
same id. A grant door asks the granter's authority, then writes: a delete that runs between the two revokes what it
reads, and the grant lands after it. The model's trace (`GrantsUnfenced`): the grant is checked, the delete retires,
reads, revokes and removes the record, a project is made again under the id, and the grant's write lands on it, so a
person the deleted tenant's admin was granting holds a rung on another tenant's project.

The grant is held after its check, at the filing of what it will announce, while the delete runs to its end and
another estate admin makes the project again; the harm is read over HTTP at the estate admin's check door.

Against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding on a store of the test's
own ([[LH-235]]).
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from catalog.services import announcement_intents


PROJECT = "acme"
#: acme's first admin, who deletes it; the estate admin who makes it again; the person the first admin grants.
ALICE, ERIN, BOB = "alice", "erin", "bob"
WAIT = 30

#: Each door granting a rung on a project, and the relation that rung opens there.
DOORS = {
    "members-admin": ("PUT", f"/management/v1/projects/{PROJECT}/members", "admin", "can_administer"),
    "members-member": ("PUT", f"/management/v1/projects/{PROJECT}/members", "member", "member"),
    "access-decider": ("POST", f"/management/v1/projects/{PROJECT}/access/grant", "decider", "can_decide_access"),
}


class _Gate:
    def __init__(self) -> None:
        self.arrived, self.release = threading.Event(), threading.Event()


@pytest.mark.parametrize("door", list(DOORS))
def test_a_grant_held_across_its_projects_delete_lands_on_no_project_made_again_under_the_id(
    governed_catalog: Any,  # noqa: ANN401 — the conftest's GovernedCatalog
    monkeypatch: pytest.MonkeyPatch,
    door: str,
) -> None:
    catalog = governed_catalog
    catalog.store.write((catalog.person(ALICE), "admin", "estate:rask"), (catalog.person(ERIN), "admin", "estate:rask"))
    assert catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(ALICE)).status_code == 200
    method, path, rung, opens = DOORS[door]

    gate, real_file = _Gate(), announcement_intents.file

    def _file(*args: Any, door: str, **kwargs: Any) -> str:  # noqa: ANN401 — announcement_intents.file's own arguments
        if door in {"member_grant", "access_grant"} and not gate.arrived.is_set():
            gate.arrived.set()
            assert gate.release.wait(WAIT), "the held grant was never released"
        return real_file(*args, door=door, **kwargs)

    monkeypatch.setattr(announcement_intents, "file", _file)
    answered: list[int] = []
    grant = threading.Thread(
        target=lambda: answered.append(
            catalog.client.request(method, path, json={"user": f"dex~{BOB}", "relation": rung}, headers=catalog.bearer(ALICE)).status_code
        )
    )
    grant.start()
    assert gate.arrived.wait(WAIT), "the grant never reached its write"
    deleted = catalog.client.delete(f"/management/v1/projects/{PROJECT}", headers=catalog.bearer(ALICE))
    remade = catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(ERIN))
    gate.release.set()
    grant.join(WAIT)

    held = catalog.client.post(
        "/management/v1/access/check",
        json={"user": catalog.person(BOB), "relation": opens, "object": f"project:{PROJECT}"},
        headers=catalog.bearer(ERIN),
    )
    assert (deleted.status_code, remade.status_code, held.status_code) == (200, 200, 200), (deleted.text, remade.text, held.text)
    assert not held.json()["allowed"], f"the grant door answered {answered}, and bob holds {rung} on the project erin made again under the id alice deleted"
