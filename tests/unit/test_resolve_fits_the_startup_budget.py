"""A booting service's wait for its authorization model fits inside the fleet's startup budget ([[LH-201]]).

`fga.resolve` runs in the FastAPI lifespan, before uvicorn binds its port, so every second of it is a
startup-probe failure: a wait longer than the budget gets the pod killed mid-wait and crash-looping instead
of failing closed. The wait is a wall-clock deadline, and this pins it against the probe numbers the chart
actually renders.
"""

from __future__ import annotations

import re
from pathlib import Path

from service_kit.governed.fga import RESOLVE_DEADLINE_SECONDS


HELPERS = Path(__file__).resolve().parents[2] / "chart" / "templates" / "_helpers.tpl"

#: The lifespan's other worst cases, per `lance.appProbes`' own note: the Dapr secret fetch alone ~80 s,
#: then the AGE pool, the DDL and the rest of the boot.
_DAPR_SECRET_FETCH_SECONDS = 80.0
_REST_OF_THE_BOOT_SECONDS = 60.0


def _startup_budgets() -> dict[str, float]:
    text = HELPERS.read_text(encoding="utf-8")
    budgets: dict[str, float] = {}
    for name, body in re.findall(r'\{\{-? define "([^"]+)" -?\}\}(.*?)\{\{-? end -?\}\}', text, flags=re.DOTALL):
        probe = re.search(r"startupProbe:\n(?:  .*\n)*?  periodSeconds: (\d+)\n  failureThreshold: (\d+)", body)
        if probe:
            budgets[name] = float(probe.group(1)) * float(probe.group(2))
    return budgets


def test_the_chart_still_renders_startup_probes() -> None:
    """Without this the assertion below would pass by comparing against nothing."""
    assert set(_startup_budgets()) >= {"rask.fleetProbes", "lance.appProbes"}


def test_resolve_fits_the_startup_budget_beside_the_rest_of_the_boot() -> None:
    tightest = min(_startup_budgets().values())

    assert tightest >= RESOLVE_DEADLINE_SECONDS + _DAPR_SECRET_FETCH_SECONDS + _REST_OF_THE_BOOT_SECONDS, (
        f"resolve waits up to {RESOLVE_DEADLINE_SECONDS}s inside a {tightest}s startup budget that also holds the Dapr secret fetch"
    )
