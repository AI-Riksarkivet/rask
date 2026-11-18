"""A caller refused at a door writing on a project writes nothing there (ADR 0245, `specs/catalog/Grants.tla`).

A door resolves the project's id to its live creation, asks its gate there with no write, and only then writes; a door
that wrote before its gate would let a caller with no right on another tenant's project write there
(`GrantsIntruderWrites`, `OnlyAuthorizedWrites`).

Against the catalog as deployed: a loopback Dex signing callers in, a real OpenFGA deciding on a store of the test's own,
and the store's own changelog read for what each request wrote.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest


PROJECT, ALICE, MALLORY = "acme", "alice", "mallory"
MEMBERS, ACCESS, ROLES = f"/management/v1/projects/{PROJECT}/members", f"/management/v1/projects/{PROJECT}/access", f"/management/v1/projects/{PROJECT}/roles"

#: Each door writing on project acme, as the caller with no right on it asks it.
DOORS = {
    "member-grant": ("PUT", MEMBERS, {"user": "dex~bob", "relation": "member"}),
    "member-revoke": ("DELETE", MEMBERS, {"user": "dex~bob", "relation": "member"}),
    "access-grant": ("POST", f"{ACCESS}/grant", {"user": "dex~bob", "relation": "decider"}),
    "role-create": ("POST", ROLES, {"name": "intruders"}),
}


def _changes(catalog: Any) -> int:  # noqa: ANN401 — the conftest's GovernedCatalog
    """How many tuple changes the store's changelog holds on a project or a role."""
    total = 0
    for kind in ("project", "role"):
        token = ""
        while True:
            page = httpx.get(f"{catalog.store.url}/stores/{catalog.store.store_id}/changes", params={"type": kind, "continuation_token": token}).json()
            if not page.get("changes"):
                break
            total += len(page["changes"])
            token = page["continuation_token"]
    return total


@pytest.fixture
def acme(governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    catalog = governed_catalog
    catalog.store.write((catalog.person(ALICE), "admin", "estate:rask"))
    assert catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(ALICE)).status_code == 200
    return catalog


@pytest.mark.parametrize("door", list(DOORS))
def test_a_caller_the_project_authorizes_for_nothing_is_refused_having_written_nothing_there(acme: Any, door: str) -> None:  # noqa: ANN401 — the fixture's GovernedCatalog
    method, path, body = DOORS[door]
    before = _changes(acme)

    answered = acme.client.request(method, path, json=body, headers=acme.bearer(MALLORY))

    assert (answered.status_code, _changes(acme) - before) in {(403, 0), (404, 0)}, answered.text
