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

import functools
import pathlib
import re
import sys
from typing import NamedTuple

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _first_party_deployments, _rendered_docs  # noqa: E402


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
#: because the target is what the chart writes — three mover Deployments share `medallion.mover:app`,
#: and they are one app, not three.
ROSTER: dict[str, str] = {
    "compute:app": "fleet",
    "controlplane:app": "fleet",
    "flows:app": "fleet",
    "ingest:create_app": "fleet",
    "notifications:app": "fleet",
    "gateway:app": "gateway",
    "catalog.main:app": "lance",
    "lineage.main:app": "lance",
    "medallion.producer:app": "lance",
    "medallion.mover:app": "lance",
    "maintenance.service:app": "lance",
    "viewer.main:app": "media",
    "search.main:app": "media",
    "annotator.main:app": "media",
}

#: First-party Deployments that run no uvicorn target, exempted BY NAME with the reason.
#:
#: ``-web-`` are the seven SvelteKit zones: a Bun server (`bun build/index.js`), not a Python app, and
#: they have their own gates under `frontend/`. ``-assist`` is a SEALED RUNNER (`runners/assist`) —
#: matched by no workspace glob on purpose, carrying its own lock and its own FastAPI server, so it
#: neither has nor may acquire a service-kit family. Anything else with no uvicorn target is an
#: unclassified app and fails.
_NOT_A_PYTHON_APP = ("-web-", "-assist")

#: `module.path:attribute`, the form uvicorn takes and the chart writes.
_TARGET = re.compile(r"^[\w.]+:\w+$")


class Deployed(NamedTuple):
    """One rendered container that runs a Python app."""

    deployment: str
    target: str
    under_launcher: bool


@functools.cache
def _render() -> tuple[tuple[Deployed, ...], tuple[str, ...]]:
    """The rendered chart's first-party containers, split into Python apps and named exemptions.

    Cached because rendering shells out to helm, and every test below needs the same answer.

    Derived from the render rather than listed, for the reason `_first_party_deployments` records:
    a hand-written tuple silently skipped four first-party pods for as long as nobody remembered to
    add them. A new Deployment is covered the moment it renders.
    """
    apps: list[Deployed] = []
    exempt: list[str] = []
    for doc in _first_party_deployments(_rendered_docs("explorer.enabled=true", "runners.enabled=true")):
        name = doc["metadata"]["name"]
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            command = list(container.get("command") or [])
            args = list(container.get("args") or [])
            if "uvicorn" not in command:
                if any(token in name for token in _NOT_A_PYTHON_APP):
                    exempt.append(name)
                    continue
                pytest.fail(f"{name}/{container['name']} is a first-party Deployment that runs no uvicorn target and is not a named non-Python exemption")
            assert args and _TARGET.match(args[0]), f"{name}/{container['name']} launches uvicorn with no `module:attr` target: {args}"
            apps.append(Deployed(name, args[0], command[0] == "opentelemetry-instrument"))
    return tuple(apps), tuple(exempt)


def _deployed() -> tuple[Deployed, ...]:
    """Every uvicorn target the rendered chart runs, with how its SDK is wired."""
    return _render()[0]


def _entry_module(target: str) -> pathlib.Path:
    """The source file behind a uvicorn target, derived from the layout rather than mapped.

    ``catalog.main:app`` -> ``services/catalog/src/catalog/main.py``; ``compute:app`` ->
    ``services/compute/src/compute/__init__.py``. Deriving it means a new app is only findable here if
    it lives where `rask-architecture` says a service lives.
    """
    module = target.split(":", 1)[0]
    top, *rest = module.split(".")
    base = REPO / "services" / top / "src" / top
    return base.joinpath(*rest).with_suffix(".py") if rest else base / "__init__.py"


def _family_of(target: str) -> str:
    """Classify an app by the assembly call its entry module actually makes.

    The roster says what the family SHOULD be; this reads what the code DOES, so the two can be
    compared. An entry module that calls no known factory and does not build its own ``FastAPI`` is
    the fifth shape this file exists to refuse.
    """
    source = _entry_module(target).read_text()
    matched = [name for name, family in FAMILIES.items() if family.assembly and family.assembly in source]
    if matched:
        assert len(matched) == 1, f"{target} calls more than one app factory: {matched}"
        return matched[0]
    if re.search(r"^app = FastAPI\(", source, re.MULTILINE):
        return "gateway"
    pytest.fail(
        f"{target} matches no known app family: its entry module calls none of {sorted(f.assembly for f in FAMILIES.values() if f.assembly)} and builds no FastAPI of its own"
    )


def test_every_deployed_app_is_on_the_roster() -> None:
    """A fifteenth app cannot reach the chart without being classified here."""
    exempt = _render()[1]
    # A token that matches nothing is a widened exemption nobody notices: it keeps standing after the
    # Deployment it was written for is renamed or removed, and the next name that happens to contain it
    # is waved through as "not a Python app".
    unused = [token for token in _NOT_A_PYTHON_APP if not any(token in name for name in exempt)]
    assert not unused, f"these non-Python exemptions match no rendered Deployment and now only widen the gate: {unused}"

    rendered = {row.target for row in _deployed()}
    assert rendered - set(ROSTER) == set(), f"deployed apps that no roster row claims: {sorted(rendered - set(ROSTER))}"
    assert set(ROSTER) - rendered == set(), (
        f"roster rows the chart no longer runs — a dead row makes every gate below vacuous for it: {sorted(set(ROSTER) - rendered)}"
    )


@pytest.mark.parametrize("target", sorted(ROSTER))
def test_every_app_is_assembled_by_the_family_it_claims(target: str) -> None:
    """The roster's family and the entry module's own assembly call must be the same thing."""
    assert _family_of(target) == ROSTER[target], f"{target} is rostered as `{ROSTER[target]}` but its entry module assembles as `{_family_of(target)}`"


@pytest.mark.parametrize("target", sorted(ROSTER))
def test_only_the_gateway_hand_assembles_its_own_app(target: str) -> None:
    """DUP-12's shape, kept shut: eight entry modules once opened `app = FastAPI(` and repeated the
    same five boot steps in comments copied between them. The gateway is the one that still does, and
    it does so because it is a proxy — no ``Settings``, no Lance error code, no api prefix.
    """
    hand_assembled = bool(re.search(r"^app = FastAPI\(", _entry_module(target).read_text(), re.MULTILINE))
    # Asserted in BOTH directions: a `return` for the gateway row would leave the one app this rule is
    # written around untested, so the day it moves onto a factory the family declaration has to move too.
    assert hand_assembled is (ROSTER[target] == "gateway"), (
        f"{target} {'hand-assembles its own FastAPI app instead of using' if hand_assembled else 'no longer hand-assembles its own FastAPI app, so it is not'} the `{ROSTER[target]}` shape"
    )


def test_the_chart_and_the_code_agree_on_who_wires_otel() -> None:
    """TWO SOURCES, and a disagreement between them is silent in both directions.

    An app whose family wires the SDK in process AND is launched under ``opentelemetry-instrument``
    gets two TracerProviders, of which only the first one set is honoured. One whose family wires
    nothing and is launched plainly exports no telemetry at all while every pod stays Ready — the
    failure mode `rask.otelEnv` already records for the fleet.
    """
    disagreements = [
        f"{row.deployment} runs {row.target} {'under opentelemetry-instrument' if row.under_launcher else 'with a plain uvicorn command'}, "
        f"but its `{ROSTER[row.target]}` family wires OTel {'via the launcher' if FAMILIES[ROSTER[row.target]].otel_via_launcher else 'in process'}"
        for row in _deployed()
        # An unrostered target is `test_every_deployed_app_is_on_the_roster`'s failure to report; it
        # has no family here, so asking which OTel path that family declares would only mask it.
        if row.target in ROSTER and row.under_launcher != FAMILIES[ROSTER[row.target]].otel_via_launcher
    ]
    assert not disagreements, disagreements

    for name, family in FAMILIES.items():
        wires_in_process = "setup_otel(" in family.seam.read_text()
        assert wires_in_process is not family.otel_via_launcher, (
            f"`{name}`'s seam {family.seam.relative_to(REPO)} {'calls' if wires_in_process else 'does not call'} setup_otel, which contradicts its declared OTel path"
        )


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


def test_the_request_id_span_hook_rides_only_the_in_process_otel_path() -> None:
    """`server_request_hook` is a Python callable, so the LAUNCHER cannot supply one.

    `opentelemetry-instrument` swaps `fastapi.FastAPI` for a subclass that calls
    `FastAPIInstrumentor.instrument_app(self, **kwargs)` with the kwargs the entry point was loaded
    with — and there is no env var carrying a callable. So the hook exists on exactly the apps whose
    family calls `setup_otel`, and the eight launcher-run apps join `X-Request-ID` to their logs but
    to no span.

    Asserted as a SINGLE SITE rather than as a count of apps: the day the estate converges on one OTel
    path, the family declarations above move and this stays true. What it refuses is a second place
    that passes the hook, which would make "who has trace correlation" answerable only by reading
    every app.
    """
    sites = sorted(
        str(path.relative_to(REPO))
        for root in ("packages", "services")
        for path in (REPO / root).rglob("*.py")
        # Shipped code only: a test that installs its own hook to drive the seam is exercising it,
        # not becoming a second owner of it.
        if "/tests/" not in str(path) and "server_request_hook=" in path.read_text()
    )
    assert sites == ["packages/service-kit/src/service_kit/otel.py"], f"the span hook is passed from more than the one seam that owns it: {sites}"

    launcher_apps = sorted(target for target, family in ROSTER.items() if FAMILIES[family].otel_via_launcher)
    assert launcher_apps, (
        "no app is launcher-instrumented any more — the OTel split is closed, and the prose in service_kit/otel.py and services/gateway/__init__.py that records it must be rewritten with this file"
    )
