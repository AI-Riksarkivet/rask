"""A role from one tenant cannot be granted a rung on another tenant's namespace or warehouse.

[[LH-062]]. A role name was estate-global: `role:analyst#assignee` minted in one project could be
granted on any other project's namespace, and nothing downstream could tell. The tuple is
well-formed, the rung is real, and the grantor holds the owner rung on the object they are granting —
so every layer below honours it.

`type role` now carries `define project: [project]`, which is the fact the door reads. The model
grants nothing through that edge; the refusal is the door's.

FAIL-OPEN FOR AN UNSCOPED ROLE IS THE SUBJECT, NOT AN OVERSIGHT. A role with no project edge is
estate-wide and keeps exactly the reach it has today. Refusing every role whose tenant is unknown
would have broken every existing grant on the day the edge landed — which is how a control gets
reverted rather than adopted — so the guard refuses only where the model carries the fact to refuse
on. Both halves are asserted here, because the fail-open half is the one that would rot silently.

A ROLE IS NEVER GRANTED A RUNG THE MODEL HOLDS FOR USERS ALONE ([[LH-563]]). A decision rung (`decider`, `[user]`) names
one account, so a grant of it to a role's assignees is a malformed request, refused 400 before the store is asked, as the
store itself refuses the tuple; asked, the store's refusal would reach the caller as an outage.

Each case is judged by its OUTCOME on the real model, on a store of this test's own ([[LH-235]]): alice creates
`beta_bronze` through the door and grants the role a rung on it, and dave, the role's one assignee, then describes it.
A refused grant leaves dave refused; an admitted one lets him read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from catalog.services import warehouses
from service_kit.storage_options import StorageOptions


#: Each target's grant door and the door dave reads it through: a namespace's describe, a warehouse's own self-view.
DOORS = {
    "namespace": ("/management/v1/namespace/beta_bronze/access/grant", "/v1/namespace/beta_bronze/describe"),
    "warehouse": ("/management/v1/warehouse/wh/access/grant", "/management/v1/warehouse/wh/access/my-permissions"),
}


@pytest.mark.parametrize(
    ("target", "role_project", "tenant", "relation", "granted", "dave_reads", "named"),
    [
        pytest.param("namespace", "acme", "beta", "reader", 400, 403, ("acme", "beta"), id="a-role-owned-by-another-project-is-refused"),
        pytest.param("namespace", "beta", "beta", "reader", 200, 200, (), id="a-role-owned-by-the-same-project-is-granted"),
        pytest.param("namespace", None, "beta", "reader", 200, 200, (), id="an-estate-wide-role-carrying-no-project-is-granted"),
        pytest.param("namespace", "acme", None, "reader", 200, 200, (), id="a-namespace-whose-tenant-cannot-be-established-is-granted"),
        # A warehouse's own record names its tenant ([[LH-555]]).
        pytest.param("warehouse", "acme", "beta", "reader", 400, 403, ("acme", "beta"), id="a-role-owned-by-another-project-is-refused-on-a-warehouse"),
        pytest.param("warehouse", "beta", "beta", "reader", 200, 200, (), id="a-role-owned-by-the-warehouses-project-is-granted"),
        pytest.param(
            "warehouse",
            "beta",
            "beta",
            "decider",
            400,
            403,
            ("decider", "role:analyst#assignee"),
            id="a-rung-the-model-holds-for-users-alone-is-refused-to-a-role",
        ),
    ],
)
def test_a_role_is_granted_only_inside_its_own_tenant(
    governed_catalog: Any,  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    tmp_path: Path,
    target: str,
    role_project: str | None,
    tenant: str | None,
    relation: str,
    granted: int,
    dave_reads: int,
    named: tuple[str, ...],
) -> None:
    catalog, alice = governed_catalog, governed_catalog.bearer("alice")
    if tenant is not None:
        warehouses.put_warehouse(str(tmp_path), StorageOptions(), {"id": "wh", "project": tenant, "bucket": tenant, "root_uri": str(tmp_path)})
    if target == "namespace":
        if tenant is not None:
            warehouses.bind_namespace(str(tmp_path), StorageOptions(), "beta_bronze", "wh", str(tmp_path))
        assert catalog.client.post("/v1/namespace/beta_bronze/create", json={}, headers=alice).status_code == 200
    else:
        catalog.store.write((catalog.person("alice"), "admin", f"project:{tenant}"), (f"project:{tenant}", "project", "warehouse:wh"))
    catalog.store.write(
        (catalog.person("dave"), "assignee", "role:analyst"),
        *([(f"project:{role_project}", "project", "role:analyst")] if role_project is not None else []),
    )
    grant_door, read_door = DOORS[target]

    grant = catalog.client.post(grant_door, json={"user": "role:analyst#assignee", "relation": relation}, headers=alice)

    assert grant.status_code == granted, grant.text
    if granted == 400:
        assert (grant.headers["content-type"], grant.json()["code"]) == ("application/problem+json", 13), grant.text
        assert all(word in grant.json()["error"] for word in named), f"the refusal does not name {named}: {grant.text}"
    read = catalog.client.post(read_door, json={}, headers=catalog.bearer("dave"))
    assert read.status_code == dave_reads, read.text
