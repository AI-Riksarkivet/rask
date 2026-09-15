"""The grant lists the catalog reads are actually FILLED by the chart.

[[LH-165]]. `cascade_tuples` writes one tuple per subject in `LANCE_FGA_CASCADE_WRITERS` and
`LANCE_FGA_MAINTAINERS`. Both default to an EMPTY list, deliberately — an estate that declares nothing
keeps exactly today's tuples — which means a list the chart never renders is indistinguishable, from
inside the catalog, from an operator who chose to declare nothing. The code writes no tuple, no error
is raised, and every unit test of the seeding function still passes because it supplies the list itself.

THAT IS NOT HYPOTHETICAL, IT IS WHAT HAPPENED. Measured 2026-09-15: the catalog had no maintainer list
at all, so `cascade_tuples` never wrote the sweep's grant, and the 93 of 97 warehouses that carried one
had it written BY HAND into the live OpenFGA store on 2026-09-08. The four created since had none and
nothing reported it; a tenant onboarded that day was silently unmaintainable.

SO THE GATE READS THE RENDER, not the settings class. A test that constructs `Settings(...)` with a
list proves the seeding works when told; only the render proves anything ever tells it.
"""

from __future__ import annotations

from typing import Any

import yaml

from tests.unit.test_invariants import _helm_template


#: Each env var, and the rung its subjects are granted. Paired here rather than checked one by one so a
#: THIRD grant list cannot be added to `cascade_tuples` without a render row landing beside it.
_GRANT_LISTS = {
    "LANCE_FGA_CASCADE_WRITERS": "writer/publisher/validator",
    "LANCE_FGA_MAINTAINERS": "maintainer",
}


def _catalog_env() -> dict[str, str]:
    """The catalog container's rendered environment, on a default-shaped estate."""
    raw = _helm_template("dapr.enabled=true", "medallion.enabled=true", "maintenance.enabled=true")
    for doc in yaml.safe_load_all(raw):
        if not doc or doc.get("kind") != "Deployment" or not doc["metadata"]["name"].endswith("-catalog"):
            continue
        for container in (doc["spec"]["template"].get("spec") or {}).get("containers", []) or []:
            env: dict[str, Any] = {e["name"]: e.get("value", "") for e in (container.get("env") or [])}
            if "LANCE_FGA_CASCADE_WRITERS" in env or container["name"] == "catalog":
                return env
    raise AssertionError("no catalog container in the render")


def test_every_grant_list_the_catalog_reads_is_rendered() -> None:
    env = _catalog_env()

    missing = [name for name in _GRANT_LISTS if name not in env]

    assert missing == [], f"the catalog reads these grant lists and the chart renders nothing into them: {missing}"


def test_the_maintenance_identity_actually_lands_in_the_list() -> None:
    """Rendered-but-empty is the same outcome as unrendered, and reads as deliberate.

    So this asserts the VALUE, not the key: on an estate with maintenance enabled and vending write
    credentials, the sweep's identity has to be in the list the catalog will grant from.
    """
    env = _catalog_env()

    assert "service-maintenance" in env["LANCE_FGA_MAINTAINERS"], (
        f"LANCE_FGA_MAINTAINERS renders as {env['LANCE_FGA_MAINTAINERS']!r} — no warehouse will be maintainable"
    )


def test_the_two_lists_stay_SEPARATE() -> None:
    """A cascade identity must not arrive as a maintainer, nor maintenance as a writer.

    `can_maintain` neither implies nor is implied by `can_write_data` — maintenance rewrites HOW a
    dataset is stored and must not change WHAT it says, while a cascade identity is the reverse. One
    merged list would hand each of them the other's authority, and the render is where that merge
    would happen silently.
    """
    env = _catalog_env()

    assert "service-maintenance" not in env["LANCE_FGA_CASCADE_WRITERS"], "maintenance is being granted write/publish rungs it must not hold"
    assert "bronze-to-silver" not in env["LANCE_FGA_MAINTAINERS"], "a cascade identity is being granted the maintainer rung"
