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

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from catalog.services import roles, warehouses
from service_kit.storage_options import StorageOptions


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
    catalog.store.write((catalog.person("root"), "owner", "estate:rask"))
    _status(catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=catalog.bearer("root")), 200)
    _status(catalog.client.put("/management/v1/projects/acme/members", json={"user": "dex~alice", "relation": "admin"}, headers=catalog.bearer("root")), 200)
    warehouses.put_warehouse(str(tmp_path), StorageOptions(), {"id": "wh", "project": "acme", "bucket": "acme", "root_uri": str(tmp_path)})
    warehouses.bind_namespace(str(tmp_path), StorageOptions(), "lake", "wh", str(tmp_path))
    _status(catalog.client.post("/v1/namespace/lake/create", json={}, headers=alice), 200)

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
    _status(catalog.client.post("/management/v1/namespace/lake/access/grant", json={"user": member, "relation": "reader"}, headers=alice), 200)
    _status(catalog.client.post("/v1/namespace/lake/describe", json={}, headers=dave), 200)

    assert _status(catalog.client.request("DELETE", assignees, json={"user": "dex~dave"}, headers=alice), 200).json()["assignees"] == []
    _status(catalog.client.post("/v1/namespace/lake/describe", json={}, headers=dave), 403, PERMISSION_DENIED)

    _status(catalog.client.put(assignees, json={"user": "dex~dave"}, headers=alice), 200)
    deleted = _status(catalog.client.delete(f"{ROLES}/{role['id']}", headers=alice), 200).json()
    assert deleted["tuples_revoked"] == 3, deleted
    _status(catalog.client.post("/v1/namespace/lake/describe", json={}, headers=dave), 403, PERMISSION_DENIED)
    _status(catalog.client.get(f"{ROLES}/{role['id']}", headers=alice), 404, TABLE_NOT_FOUND)
    assert not [t for t in catalog.store.tuples() if f"role:{role['id']}" in (t[0].split("#")[0], t[2])], "a tuple of the deleted role survived"


def test_a_role_that_is_its_projects_only_admin_keeps_an_assignee_and_ends_with_its_project(governed_catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """A role holding a project's only direct ``admin`` is the project's administration: emptying or ending it would leave
    nobody to administer the project, so both are refused 409 (code 14), as the members door refuses the last admin
    ([[LH-434]]). Changing who is in it asks ``can_grant_admin``, so a security admin cannot make admins through it. The
    project's delete ends it with every tuple it held."""
    catalog = governed_catalog
    root, alice, grace = catalog.bearer("root"), catalog.bearer("alice"), catalog.bearer("grace")
    catalog.store.write((catalog.person("root"), "owner", "estate:rask"))
    _status(catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=root), 200)
    role = _status(catalog.client.post(ROLES, json={"name": "admins"}, headers=root), 201).json()
    assignees, member = f"{ROLES}/{role['id']}/assignees", f"role:{role['id']}#assignee"
    _status(catalog.client.put(assignees, json={"user": "dex~alice"}, headers=root), 200)
    _status(catalog.client.put("/management/v1/projects/acme/members", json={"user": member, "relation": "admin"}, headers=root), 200)
    _status(catalog.client.request("DELETE", "/management/v1/projects/acme/members", json={"user": "dex~root", "relation": "admin"}, headers=alice), 200)
    catalog.store.write((catalog.person("grace"), "security_admin", "project:acme"))

    _status(catalog.client.request("DELETE", assignees, json={"user": "dex~alice"}, headers=alice), 409, CONCURRENT_MODIFICATION)
    _status(catalog.client.delete(f"{ROLES}/{role['id']}", headers=alice), 409, CONCURRENT_MODIFICATION)
    _status(catalog.client.put(assignees, json={"user": "dex~dave"}, headers=grace), 404, TABLE_NOT_FOUND)

    _status(catalog.client.delete("/management/v1/projects/acme", headers=alice), 200)
    assert not [t for t in catalog.store.tuples() if f"role:{role['id']}" in (t[0].split("#")[0], t[2])], "a tuple of the project's role survived it"


def test_a_role_whose_ending_failed_part_way_is_ended_by_calling_its_door_again(governed_catalog: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """An ending that fails after its grants went leaves the role listed and deletable, so calling the door again ends
    it, and its name is free for a new role, which is another uuid."""
    catalog = governed_catalog
    root = catalog.bearer("root")
    catalog.store.write((catalog.person("root"), "owner", "estate:rask"))
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
