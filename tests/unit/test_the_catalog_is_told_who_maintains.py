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

The provenance namespace's retention is filled the same way ([[LH-529]]), from the estate's own values, and so is the list
of the release's own accounts, the only ones whose `writer` the catalog's boot takes back ([[LH-563]]).
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from tests.unit.chart_render import DEFAULT_ARGS, REPO, containers, env_of, render
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


def test_the_provenance_tables_keep_the_versions_the_estate_declares() -> None:
    """The estate's catalog is told the provenance tables' retention, so maintenance keeps their history short. Rendered
    empty, the sweep's seven days of versions apply, and every open of a feed table lists them all."""
    estate = render("--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml"))
    catalog = next(
        env_of(container)
        for doc in estate
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith("-catalog")
        for container in doc["spec"]["template"]["spec"]["containers"]
        if container["name"] == "catalog"
    )

    assert catalog["LANCE_PROVENANCE_RETAIN_VERSIONS"] == "20"


def test_the_catalog_takes_back_a_writer_only_from_the_accounts_the_bootstrap_hook_converges() -> None:
    """The catalog's boot and the bootstrap hook converge one list of the release's own accounts, and every provenance
    writer the catalog is told is on it, so a writer the release stops naming is taken back at the next boot. Rendered
    empty, the boot takes back nothing and the retired writer keeps writing lineage's tables."""
    rendered = containers(render(*DEFAULT_ARGS))
    catalog = next(env_of(container) for workload, name, container in rendered if workload.endswith("-catalog") and name == "catalog")
    hook = next(env_of(container) for workload, _, container in rendered if workload.endswith("-bootstrap-admin"))
    own, writers = json.loads(catalog.get("LANCE_OWN_ACCOUNTS", "[]")), json.loads(catalog["LANCE_PROVENANCE_WRITERS"])

    assert own == json.loads(hook["FGA_OWN_ACCOUNTS"])
    assert writers and set(writers) <= set(own), f"provenance writers {writers} outside the release's own accounts {own}"
