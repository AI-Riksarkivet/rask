"""Every deployed Python app belongs to one of FOUR declared families — docs/DECISIONS.md "The Python estate audit" X1.

X1 was parked as "three entrypoint families, two error taxonomies, three health conventions and two
OTel wiring paths". Two of those axes have since been answered by the code and are pinned elsewhere:
the error taxonomy is one shape for all fourteen apps
(`test_the_fleet_speaks_one_error_envelope.py` for the fleet five, `test_one_lance_service_assembly.py`
for the lance five, `test_one_media_service_seam.py` for the media three, and the gateway takes
`register_handlers` whole), and the operational probe pair is one router mounted by every family
(`test_fleet_probes.py`, `test_probe_paths_are_served.py`).

What survives is a SPLIT THAT IS ARGUED RATHER THAN ACCIDENTAL. `service_kit.lance_app`'s module
docstring states the case for three factories: the fleet plane mounts under ``RASK_API_PREFIX`` and
reads ``service_kit.config.Settings``; the media plane mounts at the root, reads ``MediaSettings`` and
must expose the Range headers a browser needs to seek video; the lance plane mounts at the root under
each service's own paths with per-service middleware ordering. The gateway is a fourth shape because
it is a proxy that owns no state and must not claim a Lance error code.

The ruling on X1 was to CLOSE it with this gate rather than to converge the planes. So this file does
not argue the split is right — it pins it. A fifteenth app may join any of the four families; it may
not invent a fifth one silently, and it may not be reachable from the chart while belonging to none.

WHAT MAKES THIS MORE THAN A LIST. Every row is cross-checked against a source the roster does not
own: the deployed set comes from the rendered chart (a Deployment whose container names a uvicorn
target), the family comes from the entry module's own assembly call, and the OTel path has to agree
between the chart's container command and the factory's code. A row that is merely written down here
proves nothing; a row that is written down and contradicted by any of those three fails.

THE OTEL AXIS IS THE ONE STILL GENUINELY SPLIT, and this file records where the seam falls rather
than closing it. The fleet five and the gateway wire the SDK in process through
`service_kit.setup_otel`; the lance five and the media three are launched under
`opentelemetry-instrument` and wire nothing themselves. That has a consequence with teeth, asserted
below: `server_request_hook` — the seam that joins the estate's `X-Request-ID` to the span it belongs
to — is a Python callable passed to `FastAPIInstrumentor.instrument_app`, and the launcher has no way
to supply one. So the eight launcher-run apps carry the id in their LOGS (`RequestIDMiddleware` sets
the context var `setup_logging`'s filter reads) and on NO span.
"""

from __future__ import annotations

import pathlib
from typing import NamedTuple

import pytest


REPO = pathlib.Path(__file__).resolve().parents[2]

SERVICE_KIT = REPO / "packages/service-kit/src/service_kit"


class Family(NamedTuple):
    """One app shape: how it is assembled, how it gets the OTel SDK, what badge it serves."""

    #: The assembly call an entry module of this family must make. ``None`` for the gateway, which
    #: builds its own ``FastAPI`` — the one family whose shape IS a bare constructor.
    assembly: str | None
    #: The file that owns the assembly, and therefore decides whether the family reaches `setup_otel`.
    seam: pathlib.Path
    #: ``True`` when the chart launches the container under ``opentelemetry-instrument`` (the SDK is
    #: wired by the launcher, outside the app) rather than in process.
    otel_via_launcher: bool
    #: ``True`` when the app mounts `service_kit.health.make_health_router` — the frontend-facing
    #: ``{prefix}/health`` badge, which is NOT the ``/livez`` + ``/readyz`` pair every family serves.
    serves_the_shared_badge: bool


FAMILIES: dict[str, Family] = {
    "fleet": Family("make_service_app(", SERVICE_KIT / "app.py", otel_via_launcher=False, serves_the_shared_badge=True),
    "lance": Family("build_lance_service_app(", SERVICE_KIT / "lance_app.py", otel_via_launcher=True, serves_the_shared_badge=False),
    "media": Family("build_media_app(", SERVICE_KIT / "media/app.py", otel_via_launcher=True, serves_the_shared_badge=False),
    "gateway": Family(None, REPO / "services/gateway/src/gateway/__init__.py", otel_via_launcher=False, serves_the_shared_badge=False),
}

#: The roster: every uvicorn target the chart runs, and the family that owns it. Keyed by target
#: because the target is what the chart writes — three stage runner Deployments share `medallion.stage_runner:app`,
#: and they are one app, not three.
ROSTER: dict[str, str] = {
    "compute:app": "fleet",
}


@pytest.mark.parametrize("target", sorted(ROSTER))
def test_the_health_badge_is_the_family_convention(target: str) -> None:
    """The ``{prefix}/health`` badge is a FAMILY property, not a per-service choice.

    Distinct from `/livez` + `/readyz`, which every family mounts from `service_kit.probes`. The badge
    is the frontend-facing liveness the chart's default ``healthPath`` points at, and only the fleet
    plane serves it — the lance and media planes are reached through their own paths and the gateway
    answers `/healthz` itself. A service that mounts it because a sibling did, or omits it because a
    sibling did, is how an ingest pod once sat at 1/2 forever on a 404 probe.
    """
    package = REPO / "services" / target.split(":", 1)[0].split(".")[0] / "src"
    # The CALL, not the import: a module that imports the factory and then builds its own router has
    # left the convention while still reading as though it follows it.
    mounts = any("make_health_router()" in path.read_text() for path in package.rglob("*.py"))
    assert mounts is FAMILIES[ROSTER[target]].serves_the_shared_badge, (
        f"{target} {'mounts' if mounts else 'does not mount'} service_kit.health.make_health_router, which contradicts the `{ROSTER[target]}` family convention"
    )
