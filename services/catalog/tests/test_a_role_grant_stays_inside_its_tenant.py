"""A role from one tenant cannot be granted a rung on another tenant's namespace.

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

Each case is judged by its OUTCOME on the real model, on a store of this test's own ([[LH-235]]): alice creates
`beta_bronze` through the door and grants the role `reader` on it, and dave, the role's one assignee, then describes it.
A refused grant leaves dave refused; an admitted one lets him read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from catalog.services import warehouses


@pytest.mark.parametrize(
    ("role_project", "namespace_project", "granted", "dave_reads"),
    [
        pytest.param("acme", "beta", 400, 403, id="a-role-owned-by-another-project-is-refused"),
        pytest.param("beta", "beta", 200, 200, id="a-role-owned-by-the-same-project-is-granted"),
        pytest.param(None, "beta", 200, 200, id="an-estate-wide-role-carrying-no-project-is-granted"),
        pytest.param("acme", None, 200, 200, id="a-namespace-whose-tenant-cannot-be-established-is-granted"),
    ],
)
def test_a_role_is_granted_on_a_namespace_only_inside_its_own_tenant(
    governed_catalog: Any,  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    tmp_path: Path,
    role_project: str | None,
    namespace_project: str | None,
    granted: int,
    dave_reads: int,
) -> None:
    catalog, alice = governed_catalog, governed_catalog.bearer("alice")
    if namespace_project is not None:
        warehouses.put_warehouse(str(tmp_path), {}, {"id": "wh", "project": namespace_project, "bucket": namespace_project, "root_uri": str(tmp_path)})
        warehouses.bind_namespace(str(tmp_path), {}, "beta_bronze", "wh", str(tmp_path))
    assert catalog.client.post("/v1/namespace/beta_bronze/create", json={}, headers=alice).status_code == 200
    catalog.store.write(
        (catalog.person("dave"), "assignee", "role:analyst"),
        *([(f"project:{role_project}", "project", "role:analyst")] if role_project is not None else []),
    )

    grant = catalog.client.post(
        "/management/v1/namespace/beta_bronze/access/grant", json={"user": "role:analyst#assignee", "relation": "reader"}, headers=alice
    )

    assert grant.status_code == granted, grant.text
    if granted == 400:
        assert "acme" in grant.json()["error"] and "beta" in grant.json()["error"], f"the refusal does not name both tenants: {grant.text}"
    read = catalog.client.post("/v1/namespace/beta_bronze/describe", json={}, headers=catalog.bearer("dave"))
    assert read.status_code == dave_reads, read.text
