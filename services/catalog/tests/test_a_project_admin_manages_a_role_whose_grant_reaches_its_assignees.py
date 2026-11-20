"""A project admin manages a role end to end, and a grant to the role reaches its assignees ([[LH-557]]).

A project's role doors make, list, read, fill and end a role, so a project admin grants access to a persona rather than
person by person, and what a role is granted goes through the per-object grant doors as any subject's does.

On the catalog as deployed with authorization on, a real OpenFGA deciding on a store of the test's own ([[LH-235]]):
root owns the estate and creates ``acme``, and makes alice its admin through the members door. alice owns the namespace
``lake`` in acme's warehouse. Every outcome is what a caller reads: dave reads ``lake`` exactly while he is assigned the
role alice granted ``reader`` on it, and the assignee door refuses alice herself (a grant may not raise its granter's own
access, as the grant doors refuse it) and a role of another project.
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import lance_namespace
import pytest
from fastapi.testclient import TestClient

from catalog.schemas import RoleRecord
from catalog.services import roles, subject_tuples, warehouses
from lakehouse.governed import fga_writes
from lakehouse.storage.options import StorageOptions
from tests import seeding


#: The Lance Namespace spec's error codes a stock client reads (`lance_docs/ns_catalog/spec.yaml`).
NAMESPACE_ALREADY_EXISTS, TABLE_NOT_FOUND, INVALID_INPUT, CONCURRENT_MODIFICATION, PERMISSION_DENIED = 2, 4, 13, 14, 15
ROLES = "/management/v1/projects/acme/roles"


def _status(response: httpx.Response, status: int, code: int | None = None) -> httpx.Response:
    assert response.status_code == status, response.text
    if code is not None:
        assert response.json()["code"] == code, response.text
    return response


def test_a_project_admin_manages_a_role_and_its_grant_reaches_its_assignees(governed_catalog: Any, tmp_path: Path) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    catalog = governed_catalog
    alice, dave = catalog.bearer("alice"), catalog.bearer("dave")
    catalog.store.write((catalog.person("root"), "admin", "estate:rask"))
    _status(catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=catalog.bearer("root")), 200)
    _status(catalog.client.put("/management/v1/projects/acme/members", json={"user": "dex~alice", "relation": "admin"}, headers=catalog.bearer("root")), 200)
    seeding.put_warehouse(str(tmp_path), StorageOptions(), {"id": "acme-wh", "project": "acme", "bucket": "acme", "root_uri": str(tmp_path)})
    warehouses.ensure_catalog_row(lance_namespace.connect("dir", {"root": str(tmp_path)}), "acme-wh")
    catalog.store.write(("project:acme", "project", "warehouse:acme-wh"))
    _status(catalog.client.post("/v1/namespace/acme-wh$lake/create", json={}, headers=alice), 200)

    role = _status(catalog.client.post(ROLES, json={"name": "analysts", "description": "reads the lake"}, headers=alice), 201).json()
    assignees, member = f"{ROLES}/{role['id']}/assignees", f"role:{role['id']}#assignee"

    _status(catalog.client.post(ROLES, json={"name": "analysts"}, headers=alice), 409, NAMESPACE_ALREADY_EXISTS)
    assert [listed["name"] for listed in _status(catalog.client.get(ROLES, headers=alice), 200).json()["roles"]] == ["analysts"]
    _status(catalog.client.get(ROLES, headers=dave), 404, TABLE_NOT_FOUND)
    _status(catalog.client.put(assignees, json={"user": "dex~alice"}, headers=alice), 403, PERMISSION_DENIED)
    elsewhere = f"role:{uuid.uuid4()}"
    catalog.store.write(("project:beta", "project", elsewhere))
    _status(catalog.client.put(assignees, json={"user": f"{elsewhere}#assignee"}, headers=alice), 400, INVALID_INPUT)

    assert _status(catalog.client.put(assignees, json={"user": "dex~dave"}, headers=alice), 200).json()["assignees"] == [catalog.person("dave")]
    _status(catalog.client.post("/management/v1/namespace/acme-wh$lake/access/grant", json={"user": member, "relation": "reader"}, headers=alice), 200)
    _status(catalog.client.post("/v1/namespace/acme-wh$lake/describe", json={}, headers=dave), 200)

    assert _status(catalog.client.request("DELETE", assignees, json={"user": "dex~dave"}, headers=alice), 200).json()["assignees"] == []
    _status(catalog.client.post("/v1/namespace/acme-wh$lake/describe", json={}, headers=dave), 403, PERMISSION_DENIED)

    _status(catalog.client.put(assignees, json={"user": "dex~dave"}, headers=alice), 200)
    deleted = _status(catalog.client.delete(f"{ROLES}/{role['id']}", headers=alice), 200).json()
    assert deleted["tuples_revoked"] == 3, deleted
    _status(catalog.client.post("/v1/namespace/acme-wh$lake/describe", json={}, headers=dave), 403, PERMISSION_DENIED)
    _status(catalog.client.get(f"{ROLES}/{role['id']}", headers=alice), 404, TABLE_NOT_FOUND)
    assert not [t for t in catalog.store.tuples() if f"role:{role['id']}" in (t[0].split("#")[0], t[2])], "a tuple of the deleted role survived"


def test_a_role_nested_beneath_an_admin_role_is_filled_and_emptied_as_the_admin_role_is(governed_catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """Who administers a project is everyone its ``admin`` reaches through its roles, however deep ([[LH-597]]).

    ``admins`` holds acme's ``admin`` and reaches alice through ``leads`` and ``oncall``. Filling or emptying any role
    of that chain asks ``can_grant_admin`` as the admin role itself does, so grace, a security admin, makes and unmakes
    no admin through it, by filling, emptying or ending a role; and no door (an unassign, a role's delete, an offboarding, the members door) takes the last
    person the project's ``admin`` reaches, whatever other admin tuples reach nobody. A role nested beneath no admin
    is still grace's to fill, and the project's delete ends every role and leaves no tuple of its own."""
    catalog = governed_catalog
    root, alice, grace = catalog.bearer("root"), catalog.bearer("alice"), catalog.bearer("grace")
    members, offboard = "/management/v1/projects/acme/members", "/management/v1/access/principals/dex~alice"
    catalog.store.write((catalog.person("root"), "admin", "estate:rask"))
    _status(catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=root), 200)
    made = {
        name: _status(catalog.client.post(ROLES, json={"name": name}, headers=root), 201).json()["id"]
        for name in ("admins", "leads", "oncall", "empty", "staff", "analysts")
    }
    assignees = {name: f"{ROLES}/{role_id}/assignees" for name, role_id in made.items()}
    everyone_in = {name: f"role:{role_id}#assignee" for name, role_id in made.items()}
    _status(catalog.client.put(assignees["admins"], json={"user": everyone_in["leads"]}, headers=root), 200)
    _status(catalog.client.put(assignees["leads"], json={"user": everyone_in["oncall"]}, headers=root), 200)
    _status(catalog.client.put(assignees["oncall"], json={"user": "dex~alice"}, headers=root), 200)
    for admin in ("admins", "empty"):
        _status(catalog.client.put(members, json={"user": everyone_in[admin], "relation": "admin"}, headers=root), 200)
    _status(catalog.client.request("DELETE", members, json={"user": "dex~root", "relation": "admin"}, headers=alice), 200)
    catalog.store.write((catalog.person("grace"), "security_admin", "project:acme"))
    before = sorted(catalog.store.tuples())

    for role in ("oncall", "admins"):
        _status(catalog.client.put(assignees[role], json={"user": "dex~dave"}, headers=grace), 404, TABLE_NOT_FOUND)
    _status(catalog.client.request("DELETE", assignees["oncall"], json={"user": "dex~alice"}, headers=grace), 404, TABLE_NOT_FOUND)
    _status(catalog.client.delete(f"{ROLES}/{made['oncall']}", headers=grace), 404, TABLE_NOT_FOUND)
    _status(catalog.client.request("DELETE", assignees["oncall"], json={"user": "dex~alice"}, headers=alice), 409, CONCURRENT_MODIFICATION)
    _status(catalog.client.request("DELETE", assignees["admins"], json={"user": everyone_in["leads"]}, headers=alice), 409, CONCURRENT_MODIFICATION)
    for role in ("oncall", "leads", "admins"):
        _status(catalog.client.delete(f"{ROLES}/{made[role]}", headers=alice), 409, CONCURRENT_MODIFICATION)
    _status(catalog.client.delete(offboard, headers=root), 409, CONCURRENT_MODIFICATION)
    assert sorted(catalog.store.tuples()) == before, "a refused change wrote or removed a tuple"

    _status(catalog.client.put(assignees["staff"], json={"user": everyone_in["analysts"]}, headers=grace), 200)
    _status(catalog.client.put(assignees["analysts"], json={"user": "dex~dave"}, headers=grace), 200)
    assert _status(catalog.client.request("DELETE", assignees["analysts"], json={"user": "dex~dave"}, headers=grace), 200).json()["assignees"] == []

    _status(catalog.client.put(members, json={"user": "dex~bob", "relation": "admin"}, headers=alice), 200)
    _status(catalog.client.request("DELETE", assignees["oncall"], json={"user": "dex~alice"}, headers=catalog.bearer("bob")), 200)
    _status(
        catalog.client.request("DELETE", members, json={"user": "dex~bob", "relation": "admin"}, headers=catalog.bearer("bob")), 409, CONCURRENT_MODIFICATION
    )

    _status(catalog.client.delete("/management/v1/projects/acme", headers=catalog.bearer("bob")), 200)
    ended = {"project:acme", *(f"role:{role_id}" for role_id in made.values())}
    assert not [t for t in catalog.store.tuples() if {t[0].split("#")[0], t[2]} & ended], "a tuple of the project or its roles survived it"


def test_an_unassignment_and_an_admin_revoke_at_once_leave_the_project_administered(governed_catalog: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice administers acme through the role ``admins`` and bob directly. Each removes the other at once, and each
    judged a project the other still administers: both removals move the project's ``admin_revision`` in their own
    Write, so the store commits one and the other reads again and is refused, leaving the winner administering it
    ([[LH-597]])."""
    catalog = governed_catalog
    root, members = catalog.bearer("root"), "/management/v1/projects/acme/members"
    catalog.store.write((catalog.person("root"), "admin", "estate:rask"))
    _status(catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=root), 200)
    role = _status(catalog.client.post(ROLES, json={"name": "admins"}, headers=root), 201).json()
    assignees = f"{ROLES}/{role['id']}/assignees"
    _status(catalog.client.put(assignees, json={"user": "dex~alice"}, headers=root), 200)
    for user in (f"role:{role['id']}#assignee", "dex~bob"):
        _status(catalog.client.put(members, json={"user": user, "relation": "admin"}, headers=root), 200)
    _status(catalog.client.request("DELETE", members, json={"user": "dex~root", "relation": "admin"}, headers=catalog.bearer("bob")), 200)
    commit, judged, meeting = fga_writes.commit_unless_changed, [], asyncio.Barrier(2)

    async def commit_once_both_judged(client: Any, **change: Any) -> bool:  # noqa: ANN401 — fga_writes.commit_unless_changed's signature
        if len(judged) < 2:
            judged.append(change)
            await asyncio.wait_for(meeting.wait(), timeout=10)
        return await commit(client, **change)

    monkeypatch.setattr(fga_writes, "commit_unless_changed", commit_once_both_judged)
    removals = [
        ("alice", members, {"user": "dex~bob", "relation": "admin"}),
        ("bob", assignees, {"user": "dex~alice"}),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda removal: catalog.client.request("DELETE", removal[1], json=removal[2], headers=catalog.bearer(removal[0])), removals))

    # The loser judges again on what the winner left: alice finds herself the last administrator (409), and bob finds
    # himself no admin, so the role answers him as a role he may not fill (404).
    refused = {"alice": (409, CONCURRENT_MODIFICATION), "bob": (404, TABLE_NOT_FOUND)}
    (winner,) = [caller for (caller, _, _), answer in zip(removals, answers, strict=True) if answer.status_code == 200]
    (loser,) = {"alice", "bob"} - {winner}
    assert [(a.status_code, a.json().get("code")) for a in answers if a.status_code != 200] == [refused[loser]], [a.text for a in answers]
    _status(catalog.client.get(members, headers=catalog.bearer(winner)), 200)


def test_a_role_made_an_admin_while_it_is_ended_is_emptied_only_by_who_may_unmake_admins(governed_catalog: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """grace, a security admin, ends ``staff``, which administers nothing when her ending first reads it. acme's
    ``admin`` is granted to the role after its grants are revoked and before its assignees are withdrawn, so that
    withdrawal unmakes dave an administrator: it is judged on its own read and refused her, as an unassignment is."""
    catalog = governed_catalog
    root, grace = catalog.bearer("root"), catalog.bearer("grace")
    catalog.store.write((catalog.person("root"), "admin", "estate:rask"))
    _status(catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=root), 200)
    role = _status(catalog.client.post(ROLES, json={"name": "staff"}, headers=root), 201).json()
    assignees, everyone_in = f"{ROLES}/{role['id']}/assignees", f"role:{role['id']}#assignee"
    _status(catalog.client.put(assignees, json={"user": "dex~dave"}, headers=root), 200)
    catalog.store.write((catalog.person("grace"), "security_admin", "project:acme"))
    revoke = subject_tuples.revoke_naming

    async def revoke_then_the_role_is_made_an_admin(*args: Any, **kwargs: Any) -> list[fga_writes.ClientTuple]:  # noqa: ANN401 — revoke_naming's signature
        removed = await revoke(*args, **kwargs)
        await asyncio.to_thread(catalog.store.write, (everyone_in, "admin", "project:acme"))
        return removed

    monkeypatch.setattr(subject_tuples, "revoke_naming", revoke_then_the_role_is_made_an_admin)

    _status(catalog.client.delete(f"{ROLES}/{role['id']}", headers=grace), 404, TABLE_NOT_FOUND)

    assert (catalog.person("dave"), "assignee", f"role:{role['id']}") in catalog.store.tuples()


def test_a_role_whose_ending_failed_part_way_is_ended_by_calling_its_door_again(governed_catalog: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """An ending that fails after its grants went leaves the role listed and deletable, so calling the door again ends
    it, and its name is free for a new role, which is another uuid."""
    catalog = governed_catalog
    root = catalog.bearer("root")
    catalog.store.write((catalog.person("root"), "admin", "estate:rask"))
    _status(catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=root), 200)
    role = _status(catalog.client.post(ROLES, json={"name": "analysts"}, headers=root), 201).json()
    _status(catalog.client.put(f"{ROLES}/{role['id']}/assignees", json={"user": "dex~dave"}, headers=root), 200)
    ending = roles.end_role_record

    def fails_once(*args: object, **kwargs: object) -> None:
        monkeypatch.setattr(roles, "end_role_record", ending)
        raise OSError("the store refused the write")

    monkeypatch.setattr(roles, "end_role_record", fails_once)
    as_a_caller_sees_it = TestClient(catalog.client.app, raise_server_exceptions=False)
    as_a_caller_sees_it.portal = catalog.client.portal
    _status(as_a_caller_sees_it.delete(f"{ROLES}/{role['id']}", headers=root), 500)
    assert [listed["id"] for listed in _status(catalog.client.get(ROLES, headers=root), 200).json()["roles"]] == [role["id"]]

    _status(catalog.client.delete(f"{ROLES}/{role['id']}", headers=root), 200)
    again = _status(catalog.client.post(ROLES, json={"name": "analysts"}, headers=root), 201).json()
    assert [listed["id"] for listed in _status(catalog.client.get(ROLES, headers=root), 200).json()["roles"]] == [again["id"]] != [role["id"]]
    assert not [t for t in catalog.store.tuples() if f"role:{role['id']}" in (t[0].split("#")[0], t[2])], "a tuple of the ended role survived it"


def test_an_ending_that_lost_a_race_ends_no_role_it_did_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A role's record is stamped ended under the ETag of the read that found it, never deleted (ADR 0172's release
    rule). So of two endings of one role, with a create taking its name between the slower one's read and its write, the
    slower one reads again, finds the new role, and leaves it standing."""
    root, so = str(tmp_path), StorageOptions()
    ended = RoleRecord(id=uuid.uuid4(), project="acme", name="analysts", created_at=datetime.now(UTC), created_by="dex~alice")
    taken = RoleRecord(id=uuid.uuid4(), project="acme", name="analysts", created_at=datetime.now(UTC), created_by="dex~bob")
    roles.create_role_record(root, so, ended)
    read = roles.read_json

    def read_then_the_rivals_land(control_root: str, storage_options: StorageOptions, key: str) -> tuple[dict[str, Any], str] | None:
        found = read(control_root, storage_options, key)
        monkeypatch.setattr(roles, "read_json", read)
        roles.end_role_record(root, so, ended)
        roles.create_role_record(root, so, taken)
        return found

    monkeypatch.setattr(roles, "read_json", read_then_the_rivals_land)
    roles.end_role_record(root, so, ended)

    assert roles.list_roles(root, so, "acme") == [taken]
