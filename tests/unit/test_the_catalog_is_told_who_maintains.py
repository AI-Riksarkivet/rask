"""The grant lists the catalog reads are actually FILLED by the chart.

[[LH-165]]. The catalog's warehouse seed writes one `maintainer` tuple per subject in `LANCE_FGA_MAINTAINERS`.
It defaults to an EMPTY list, deliberately — an estate that declares nothing keeps exactly today's tuples —
which means a list the chart never renders is indistinguishable, from
inside the catalog, from an operator who chose to declare nothing. The code writes no tuple, no error
is raised, and every unit test of the seeding function still passes because it supplies the list itself.

THAT IS NOT HYPOTHETICAL, IT IS WHAT HAPPENED. Measured 2026-09-15: the catalog had no maintainer list
at all, so the seed never wrote the sweep's grant, and the 93 of 97 warehouses that carried one
had it written BY HAND into the live OpenFGA store on 2026-09-08. The four created since had none and
nothing reported it; a tenant onboarded that day was silently unmaintainable.

SO THE GATE READS THE RENDER, not the settings class. A test that constructs `Settings(...)` with a
list proves the seeding works when told; only the render proves anything ever tells it.

The provenance namespace's lists are filled the same way ([[LH-529]]): its retention, and the ServiceAccounts outside
this release that compact its tables, named in the estate's own values. That a named account is the one a maintenance
job runs as is that job's release's to keep.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from tests.unit.chart_render import REPO, env_of, render
from tests.unit.test_invariants import _helm_template


def _catalog_env() -> dict[str, str]:
    """The catalog container's rendered environment, on a default-shaped estate."""
    raw = _helm_template("dapr.enabled=true", "maintenance.enabled=true")
    for doc in yaml.safe_load_all(raw):
        if not doc or doc.get("kind") != "Deployment" or not doc["metadata"]["name"].endswith("-catalog"):
            continue
        for container in (doc["spec"]["template"].get("spec") or {}).get("containers", []) or []:
            env: dict[str, Any] = {e["name"]: e.get("value", "") for e in (container.get("env") or [])}
            if container["name"] == "catalog":
                return env
    raise AssertionError("no catalog container in the render")


def test_the_catalog_seeds_maintenance_no_data_rung() -> None:
    """Maintenance is told to the catalog as a maintainer and nothing else, and no service is told to it as a writer.

    Rendered-but-empty is the same outcome as unrendered, and reads as deliberate, so this asserts the VALUE, not the
    key: on an estate with maintenance enabled, the sweep's identity is the list the catalog grants from.
    `can_maintain` neither implies nor is implied by `can_write_data` — maintenance rewrites HOW a
    dataset is stored and must not change WHAT it says. A warehouse-wide writer list rendered beside the
    maintainer list is how a service would be handed a data rung at every warehouse create, silently.
    """
    env = _catalog_env()

    assert "LANCE_FGA_CASCADE_WRITERS" not in env, "the catalog is told a service writer list, granted a data rung at every warehouse create"
    assert json.loads(env["LANCE_FGA_MAINTAINERS"]) == ["kubernetes~system:serviceaccount:default:release-name-sa-maintenance"], (
        f"LANCE_FGA_MAINTAINERS renders as {env['LANCE_FGA_MAINTAINERS']!r}, not maintenance alone"
    )


def test_the_provenance_tables_are_maintained_by_the_accounts_the_estate_names() -> None:
    """The estate's catalog keeps the provenance tables' declared versions and grants their compaction to exactly the
    ServiceAccount a compaction job's values name, `<namespace>:<name>` rendered as that account's subject. A list
    rendered empty grants nobody, and the tables grow unbounded with nothing reporting it. The job's own release supplies
    the account (XC-191), so the estate's overlay is given one here as that release would give it."""
    estate = render(
        "--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml"),
        "--set-json", 'services.lineage.provenanceMaintainers=["default:a-compactor"]',
    )  # fmt: skip
    catalog = next(
        env_of(container)
        for doc in estate
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith("-catalog")
        for container in doc["spec"]["template"]["spec"]["containers"]
        if container["name"] == "catalog"
    )

    assert json.loads(catalog["LANCE_PROVENANCE_MAINTAINERS"]) == ["kubernetes~system:serviceaccount:default:a-compactor"]
    assert catalog["LANCE_PROVENANCE_RETAIN_VERSIONS"] == "20"
