"""Maintenance reads the estate's grants and has them repaired only through the catalog's doors ([[LH-061]], [[XC-084]]).

Maintenance holds no OpenFGA credential: OpenFGA has no per-tuple access control, so a client that may write one tuple
may write any, and one able to grant itself an owner or estate rung could lift a hold through the catalog's own doors.
Its drift report reads the store's grant census at `GET /management/v1/grants/census`, which leaves out the objects the
deployment keeps for itself (the estate root and the default warehouse, which no registry names), and its rebuild asks
`POST /management/v1/grants/tenancy/rebuild`, which writes the edge a warehouse's own registry record names and nothing
for a warehouse no record names. The revoke of a ghost's grants is driven end to end by
`tests/integration/test_drift_repair_keeps_the_grants_of_a_table_in_the_trash.py`.

On a real OpenFGA, each caller a person holding the `reconciler` estate role as the chart's bootstrap hook seeds it for
maintenance's ServiceAccount.
"""

from __future__ import annotations

from typing import Any

from catalog.core.config import get_settings
from catalog.services import warehouses


def test_the_census_counts_each_objects_grants_and_leaves_out_the_deployments_own(governed_catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    catalog = governed_catalog
    root = get_settings().fga_root_object
    catalog.store.write(
        (catalog.person("mx"), "reconciler", root),
        (catalog.person("alice"), "admin", "project:acme"),
        (catalog.person("alice"), "owner", "warehouse:lance_catalog"),
    )

    census = catalog.client.get("/management/v1/grants/census", headers=catalog.bearer("mx"))

    assert census.status_code == 200, census.text
    counts = census.json()["counts_by_type"]
    root_type, _, root_id = root.partition(":")
    assert (census.json()["enabled"], counts.get("project"), root_id in counts.get(root_type, {}), "lance_catalog" in counts.get("warehouse", {})) == (
        True,
        {"acme": 1},
        False,
        False,
    ), counts


def test_the_tenancy_rebuild_writes_the_edge_a_warehouses_record_names_and_none_for_a_warehouse_no_record_names(
    governed_catalog: Any,  # noqa: ANN401 — the conftest's GovernedCatalog
) -> None:
    catalog = governed_catalog
    settings = get_settings()
    catalog.store.write((catalog.person("mx"), "reconciler", settings.fga_root_object))
    warehouses.put_warehouse(
        settings.registry_root,
        settings.storage_options(),
        {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme", "created_at": "2026-11-08T00:00:00Z"},
    )

    rebuilt = catalog.client.post("/management/v1/grants/tenancy/rebuild", json={"warehouses": ["wh-a", "wh-gone"]}, headers=catalog.bearer("mx"))

    assert rebuilt.status_code == 200, rebuilt.text
    assert (rebuilt.json()["written"], list(rebuilt.json()["unjustified"]), ("project:acme", "project", "warehouse:wh-a") in catalog.store.tuples()) == (
        ["project:acme project warehouse:wh-a"],
        ["wh-gone"],
        True,
    )
