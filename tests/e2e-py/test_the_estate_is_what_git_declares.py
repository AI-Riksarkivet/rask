"""The estate is what git declares: the catalog converges the release's declaration, and its doors leave it be ([[XC-213]]).

The declaration is the release's `catalog.declared` values, rendered to the ConfigMap `<release>-catalog-declaration`
the catalog mounts (`chart/templates/catalog-declaration.yaml`). The module reads it off the release with `kubectl` and
parses it with the catalog's own model (`catalog.services.declaration.Declaration`), so what it holds the estate to is
what the catalog was handed. Then, read back on the deployed catalog with no call to a door that makes anything:

- each declared project and warehouse stands, its record marked `managed_by: git`, and each tuple the declaration asks
  for (a project's admins, each declared grant, the estate's administrator and its services' roles, [[XC-011]]) is held
  on its object, read through the estate admin's tuple door;
- a door asked to change what git declares of a warehouse refuses: a deactivation answers 403 code 15 naming git, and
  the warehouse's record reads the same after it.

NOTHING HERE CHANGES THE DECLARATION, because it is git's: drift corrected by a tick, a grant removed from git revoked,
and a project removed from git released are read back by hand after a values change. A release that declares no
warehouse fails the refusal leg rather than skipping it, so a `--require-live` drive cannot pass without one.

Run via ``bash scripts/e2e_live.sh tests/e2e-py/test_the_estate_is_what_git_declares.py``.
"""

from __future__ import annotations

import json

import pytest
from chaos import RELEASE, kubectl
from lance_namespace import ErrorCode
from refusals import assert_refused
from scratch_tenant import ADMIN_USER, CATALOG, DEX, Identity, ok, tuples_on

from catalog.services.declaration import Declaration
from lakehouse.problem import PROBLEM_JSON


#: The estate root and the estate's own warehouse: the defaults of `RASK_FGA_ROOT_OBJECT` and
#: `RASK_FGA_DEFAULT_WAREHOUSE_OBJECT`, which no chart value changes.
ESTATE_ROOT, ESTATE_WAREHOUSE = "estate:rask", "warehouse:lance_catalog"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (CATALOG and DEX), reason="set LANCE_E2E_CATALOG_URL and LANCE_E2E_DEX (a deployed estate)"),
]


@pytest.fixture(scope="module")
def declaration() -> Declaration:
    """The declaration the release renders for its catalog, as the catalog reads it."""
    rendered = json.loads(kubectl("get", f"configmap/{RELEASE}-catalog-declaration", "-o", "json"))
    return Declaration.model_validate_json(rendered["data"]["declaration.json"])


@pytest.fixture(scope="module")
def admin() -> Identity:
    """The estate admin, who reads every project, warehouse and tuple."""
    return Identity(ADMIN_USER)


def test_every_declared_project_warehouse_and_grant_stands_managed_by_git(declaration: Declaration, admin: Identity) -> None:
    projects = {
        project.id: ok(admin.http.get(f"{CATALOG}/management/v1/projects/{project.id}", timeout=60)).get("managed_by") for project in declaration.projects
    }
    warehouses = {
        warehouse.id: ok(admin.http.get(f"{CATALOG}/management/v1/warehouses/{warehouse.id}", timeout=60)).get("managed_by")
        for warehouse in declaration.warehouses
    }
    wanted = declaration.tuples(root=ESTATE_ROOT, estate_warehouse=ESTATE_WAREHOUSE)
    held = {obj: tuples_on(admin, obj) for obj in sorted({grant.object for grant in wanted})}
    missing = sorted(f"{grant.user} {grant.relation} {grant.object}" for grant in wanted if (grant.user, grant.relation) not in held[grant.object])

    assert (projects, warehouses, missing) == (
        dict.fromkeys((project.id for project in declaration.projects), "git"),
        dict.fromkeys((warehouse.id for warehouse in declaration.warehouses), "git"),
        [],
    )


def test_a_declared_warehouse_is_refused_a_deactivation_at_its_door(declaration: Declaration, admin: Identity) -> None:
    assert declaration.warehouses, f"the release {RELEASE} declares no warehouse (`catalog.declared.warehouses`), so git's hold on one cannot be read"
    warehouse = declaration.warehouses[0].id

    refused = assert_refused(
        lambda: admin.http.post(f"{CATALOG}/management/v1/warehouses/{warehouse}/deactivate", timeout=60),
        status=403,
        code=ErrorCode.PERMISSION_DENIED,
        unchanged=lambda: ok(admin.http.get(f"{CATALOG}/management/v1/warehouses/{warehouse}", timeout=60)),
        foreign=(),
    )

    assert (refused.headers["content-type"], refused.json()["detail"]) == (
        PROBLEM_JSON,
        f"warehouse:{warehouse} is declared in git, which owns its status, deletion protection, delete profile and existence. Change the "
        "declaration (the chart's `catalog.declared` values) and the catalog converges it; a door cannot.",
    )
