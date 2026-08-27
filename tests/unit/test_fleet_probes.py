"""Every fleet app must serve /livez + /readyz, and the kubelet must actually ask for them.

open_fastapi-audit — "`make_service_app` mounts no probes router, so four of the five services it
composes have no /readyz at all and the chart uses a static liveness badge as the readiness probe".

TWO HALVES, and the second is the one the finding itself calls the sharper point.

**The router is unmounted.** `service_kit.probes` exists precisely so the estate has ONE drain-aware
probe pair — its docstring records that the same twenty lines had already drifted three ways across
six hand-rolled copies. But `make_service_app` never mounts it, so compute, controlplane, flows and
ingest expose no `/livez` and no `/readyz` at all. Only notifications root-mounts it by hand.

**And where /readyz DOES exist, nothing asks for it.** `fleet.yaml` points BOTH probes at
`$svc.healthPath | default "/api/health"`, and `values.yaml` sets `healthPath` for exactly one
service. So notifications' `make_probes_router(actor_plane_ready)` — which reports `shutting_down`,
`starting`, and a degraded actor plane — is probed by nothing: the estate pays for a readiness
surface and then wires the kubelet past it. Worse, `/api/health` is a STATIC badge (`compute/health.py`
returns a bare `Liveness()`), so the readiness probe answers 200 while the pod is draining. That is
the readiness contract inverted: `health-checks.md` says shutting down MUST be 503 and out of
rotation.

The reference is explicit that these are two endpoints with two purposes and must not be conflated:
liveness is "am I alive? No deps", readiness is "can I serve? Checks deps". One path serving both,
and that path being a constant, means neither question is actually being asked.

`/api/health` stays exactly as it is — it is the frontend-facing badge the estate documents it to be,
and this gate does not touch it.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

#: The apps composed through `make_service_app`. The gateway is absent from THIS list only because it
#: is not composed by the factory — it builds its own FastAPI. It serves the same pair and is probed
#: at it; see the gateway block at the end of this file.
FACTORY_APPS = ["compute", "controlplane", "flows", "notifications"]


def _app(module: str):
    import importlib

    mod = importlib.import_module(module)
    return mod.create_app() if hasattr(mod, "create_app") else mod.app


@pytest.mark.parametrize("module", [*FACTORY_APPS, "ingest"], ids=[*FACTORY_APPS, "ingest"])
def test_the_app_serves_both_probes(module: str) -> None:
    """One shared, drain-aware pair — not a per-service hand-roll, and not nothing.

    ASKED OVER HTTP, not read off `app.routes`. This FastAPI version keeps included routers as
    `_IncludedRouter` wrappers rather than flattening them into `APIRoute`s, so a route-object scan
    finds nothing and reports every app as unprobed — a gate that cannot pass. What matters is whether
    the path ANSWERS, so the test asks it.

    No lifespan is run, deliberately: `/readyz` answering 503 with `starting` is the correct response
    for a process that has not completed startup, and it proves the endpoint reports lifecycle state
    rather than returning a constant. A 404 is the failure this catches.
    """
    client = TestClient(_app(module))
    for path in ("/livez", "/readyz"):
        status = client.get(path).status_code
        assert status != 404, (
            f"{module} serves no {path} — `service_kit.probes` exists so the estate has one drain-aware probe pair, and this app never mounted it"
        )


@pytest.mark.parametrize("module", [*FACTORY_APPS, "ingest"], ids=[*FACTORY_APPS, "ingest"])
def test_readiness_is_not_a_constant(module: str) -> None:
    """The distinction the whole finding turns on: `/api/health` returns a static badge, so probing it
    reports "ready" while the pod drains. `/readyz` must answer from lifecycle state."""
    client = TestClient(_app(module))
    body = client.get("/readyz").json()
    assert body.get("status") in {"starting", "shutting_down", "unhealthy", "degraded", "healthy", "ready"}, (
        f"{module} /readyz returned {body!r} — a readiness answer must report state, not a constant"
    )


def test_the_chart_probes_READINESS_at_readyz_not_the_static_badge() -> None:
    """A readiness probe pointed at a constant cannot report draining, which is its whole job."""
    from test_invariants import _rendered_docs  # noqa: PLC0415

    wrong: dict[str, str] = {}
    for doc in _rendered_docs():
        if doc.get("kind") != "Deployment" or "rask-" not in doc["metadata"]["name"]:
            continue
        name = doc["metadata"]["name"]
        if name.endswith(("-gateway", "-web-annotator", "-web-compute", "-web-explorer", "-web-home", "-web-lakehouse", "-web-models", "-web-studio")):
            continue  # the gateway has a recorded exception; the web zones are not fleet apps
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            probe = (container.get("readinessProbe") or {}).get("httpGet") or {}
            path = probe.get("path")
            if path and path != "/readyz":
                wrong[f"{name}/{container['name']}"] = path

    assert not wrong, (
        f"these readiness probes point somewhere other than /readyz: {wrong} — `/api/health` is a "
        f"static badge, so it answers 200 while the pod drains and the kubelet keeps sending traffic"
    )


def test_liveness_and_readiness_are_not_the_SAME_path() -> None:
    """The reference's first rule: two endpoints, two purposes, don't conflate them.

    NO EXEMPTION LIST. This skipped `-gateway` by name, which is a blind spot rather than a rule: a
    second front-door service is covered only if someone remembers to add it, and a gateway that
    later grows the shared pair stays unchecked forever. The follow-up finding asks for the exemption
    to be DERIVED from what an app serves — and deriving it removed the need for one, because the
    gateway now serves the pair like everything else.
    """
    from test_invariants import _rendered_docs  # noqa: PLC0415

    conflated: dict[str, str] = {}
    for doc in _rendered_docs():
        if doc.get("kind") != "Deployment" or "rask-" not in doc["metadata"]["name"]:
            continue
        name = doc["metadata"]["name"]
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            live = ((container.get("livenessProbe") or {}).get("httpGet") or {}).get("path")
            ready = ((container.get("readinessProbe") or {}).get("httpGet") or {}).get("path")
            if live and ready and live == ready:
                conflated[f"{name}/{container['name']}"] = live

    assert not conflated, f"liveness and readiness share one path: {conflated}"


def test_the_gateway_probe_REPORTS_the_drain() -> None:
    """The gateway keeps `/healthz` for both probes, and that exception is recorded and correct —
    probing a proxied path would couple its readiness to an upstream. But a constant cannot say
    "draining", and the gateway is the INGRESS: a rolling update that keeps it in rotation through
    SIGTERM drops in-flight requests at the one hop every request passes through.

    open_fastapi-audit names this explicitly: "even it should set `app.state.shutting_down` so its
    `/healthz` can report the drain".
    """
    import gateway

    client = TestClient(gateway.app)
    assert client.get("/healthz").status_code == 200

    gateway.app.state.shutting_down = True
    try:
        response = client.get("/healthz")
    finally:
        gateway.app.state.shutting_down = False

    assert response.status_code == 503, (
        "the gateway answers /healthz 200 while draining, so the kubelet keeps routing to it through "
        "SIGTERM — at the one hop every request in the estate passes through"
    )


# ── the gateway's exception, re-decided ─────────────────────────────────────────────────────────
#
# open_fastapi-audit — "notifications is the one fleet service that serves the standard `/livez` +
# `/readyz` pair, and the chart probes neither — both probes point at its liveness badge".
#
# The routing half of that finding closed with the split above. Its Fix has a SECOND clause: the gate
# must assert the two probes differ "for any service that mounts the probes router" — i.e. the
# exemption must be DERIVED from what an app serves, not matched on its name. `test_liveness_and_
# readiness_are_not_the_SAME_path` did the latter (`name.endswith("-gateway")`), which is a blind
# spot rather than a rule: a second front-door service is exempted only if someone remembers, and a
# gateway that later grows the shared pair stays unchecked forever.
#
# Deriving it turns out to delete the exemption instead of improving it. The gateway is the one fleet
# app still hand-rolling its drain check — built with a bare `FastAPI(...)`, serving neither probe,
# with a `/healthz` that re-implements the `shutting_down` branch `service_kit.probes` owns. That is
# the exact duplication probes.py's docstring says it exists to delete ("the same twenty lines had
# already drifted three ways across six hand-rolled copies"), and the ingress is the worst place to
# keep a private copy.
#
# THE RECORDED REASON SURVIVES INTACT, which is why this is not a reversal of values.yaml's note.
# That note says probing a PROXIED path would couple gateway readiness to an upstream — true, and
# `/readyz` is not proxied: `make_probes_router()` with no `ready_check` reports `starting` and
# `shutting_down` and touches nothing else. The gateway gets the estate's readiness contract without
# acquiring a dependency. `/healthz` stays as its public badge, the way `/api/health` stayed as the
# fleet's.


def test_the_gateway_serves_the_shared_probe_pair() -> None:
    """The last hand-rolled probe in the estate, at the hop every request passes through."""
    import gateway

    # THE LIFESPAN RUNS, because `/readyz` gates on `startup_complete` and a bare `TestClient(app)`
    # never starts it — the probe would answer `starting` forever and the test would be measuring
    # the harness rather than the app.
    with TestClient(gateway.app) as client:
        assert client.get("/livez").status_code == 200, "the gateway serves no /livez — it is the one fleet app that never got the shared, drain-aware pair"
        assert client.get("/readyz").status_code == 200, "the gateway serves no /readyz"


def test_the_gateway_readyz_REPORTS_the_drain() -> None:
    """A readiness probe that cannot say "draining" is the inversion this whole finding is about."""
    import gateway

    with TestClient(gateway.app) as client:
        assert client.get("/readyz").status_code == 200, "the app never became Ready, so the drain proves nothing"
        gateway.app.state.shutting_down = True
        try:
            draining = client.get("/readyz")
        finally:
            gateway.app.state.shutting_down = False

    assert draining.status_code == 503, "the gateway stays Ready through SIGTERM at the estate's only ingress"
    assert draining.json()["status"] == "shutting_down"


def test_the_gateway_readiness_still_does_not_ASK_an_upstream() -> None:
    """values.yaml's recorded reason, pinned rather than argued.

    The exception was recorded because probing a proxied path would couple the front door's readiness
    to a backend — so a front-door-only install (no domain services) would never go Ready. The fix
    must not quietly undo that. `_routes()` is populated but every upstream here is unreachable, and
    readiness must still answer 200: the probe is lifecycle-only, with no `ready_check` behind it.
    """
    import gateway

    with TestClient(gateway.app) as client:
        assert gateway.app.state.routes, "no routes were built, so this proves nothing about upstream coupling"
        ready = client.get("/readyz")

    assert ready.status_code == 200, "gateway readiness now depends on an upstream — the exact coupling values.yaml recorded the exception to avoid"


def test_the_chart_probes_the_gateway_at_the_pair_it_now_serves() -> None:
    """The app half is worthless if the kubelet still asks the badge."""
    from test_invariants import _rendered_docs  # noqa: PLC0415

    container = next(
        c
        for doc in _rendered_docs()
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith("-gateway")
        for c in doc["spec"]["template"]["spec"]["containers"]
        if c["name"] == "gateway"
    )
    assert container["livenessProbe"]["httpGet"]["path"] == "/livez"
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"


# ── the boot window ─────────────────────────────────────────────────────────────────────────────
#
# open_fastapi-audit — "The six fleet pods have no startupProbe, and controlplane also missed the
# measured probe-timeout fix — the lakehouse helper carries both and the fleet templates carry
# neither".


#: `periodSeconds × failureThreshold` for the lakehouse plane's `lance.appProbes` — 30 × 10s. The
#: fleet's boot budget is held to the same floor rather than a new number, because the two planes run
#: the same uvicorn against the same dependencies and a second figure would only be a second thing to
#: keep in step.
BOOT_BUDGET_SECONDS = 300


def _fleet_service_names() -> set[str]:
    """The services `fleet.yaml` and `controlplane.yaml` render, read from the chart itself.

    DERIVED, because a literal list here is the very failure this finding is about: controlplane is
    invisible to anything keyed on `services.*`, and it is precisely the row that missed two probe
    fixes in a row. `$lakehouse` is parsed out of fleet.yaml rather than restated, so moving a service
    between the two planes cannot leave this gate asserting about the wrong set.
    """
    import re  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    template = (pathlib.Path(__file__).resolve().parents[2] / "chart/templates/fleet.yaml").read_text()
    match = re.search(r"\$lakehouse := list ([^\n]+)", template)
    assert match, "fleet.yaml no longer declares $lakehouse — this gate can no longer tell the planes apart"
    lakehouse = set(re.findall(r'"([^"]+)"', match.group(1)))

    values = yaml.safe_load((pathlib.Path(__file__).resolve().parents[2] / "chart/values.yaml").read_text())
    return (set(values["services"]) - lakehouse) | {"controlplane"}


def _http_probed_containers() -> list[tuple[str, dict]]:
    """Every FLEET container the kubelet probes over HTTP, derived from the render.

    TWO DELIBERATE BOUNDS, both narrower than "every first-party pod", and neither silent.

    The seven web pods use `lance.tcpProbes` (a TCP accept on the Bun server's port). This finding is
    about the HTTP fleet, and widening to them would be a different claim from a different boot
    profile smuggled in under this one.

    The lakehouse plane (`lance.appProbes`: catalog, lineage, maintenance, the producer, the movers)
    is excluded for the same reason and is the finding's own EXEMPLAR — it is what already carries a
    startupProbe. It does keep k8s's 1s probe timeout, which the #136 measurement argues against for
    any pod under memory pressure; that is a separate observation, recorded rather than fixed here.
    """
    from test_invariants import _first_party_deployments, _rendered_docs  # noqa: PLC0415

    fleet = _fleet_service_names()
    found = []
    for doc in _first_party_deployments(_rendered_docs()):
        name = doc["metadata"]["name"]
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            if container["name"] not in fleet:
                continue
            if ((container.get("livenessProbe") or {}).get("httpGet") or {}).get("path"):
                found.append((f"{name}/{container['name']}", container))
    assert len(found) >= len(fleet), f"only {len(found)} of {len(fleet)} fleet containers were found in the render — the gate is testing less than it claims"
    return found


def test_every_http_probed_pod_gets_a_boot_window() -> None:
    """Uvicorn runs the lifespan BEFORE it binds, so every pre-bind second is a connection refused.

    `lance.appProbes` states the hazard and defends against it — "liveness … SIGKILLs a
    still-initializing pod into CrashLoopBackOff exactly when a dependency is already slow" — and the
    fleet templates, which compose services with slower lifespans than the lakehouse plane's, carried
    no startupProbe at all. Liveness alone gives a hard `initialDelaySeconds 15 + 3 × 20s ≈ 55s`
    ceiling: notifications does an OIDC discovery fetch, two OpenFGA provision round-trips (at
    first-install time, when OpenFGA is itself starting), actor registration and a 10s proxy warm-up
    inside that.
    """
    naked = [name for name, container in _http_probed_containers() if not container.get("startupProbe")]
    assert not naked, f"these pods have no startupProbe, so liveness starts SIGKILLing them ~55s into a boot that can legitimately take longer: {naked}"


def test_the_boot_window_is_actually_long_enough() -> None:
    """A startupProbe present but tight is the same failure with an extra field."""
    short = {}
    for name, container in _http_probed_containers():
        probe = container.get("startupProbe") or {}
        budget = probe.get("periodSeconds", 10) * probe.get("failureThreshold", 3)
        if budget < BOOT_BUDGET_SECONDS:
            short[name] = budget
    assert not short, f"boot budgets below the lakehouse plane's {BOOT_BUDGET_SECONDS}s: {short}"


def test_no_probe_keeps_the_one_second_default_that_was_MEASURED_to_misfire() -> None:
    """#136: under memory pressure the handler missed k8s's 1s deadline, so OOM kills presented as
    CrashLoopBackOff probe failures. The fix was recorded in fleet.yaml and applies to every probe on
    every first-party pod — a startupProbe fires during the slowest window of all."""
    tight = {}
    for name, container in _http_probed_containers():
        for kind in ("startupProbe", "readinessProbe", "livenessProbe"):
            probe = container.get(kind)
            if probe and probe.get("timeoutSeconds", 1) < 5:
                tight[f"{name}:{kind}"] = probe.get("timeoutSeconds", 1)
    assert not tight, f"these probes keep a timeout below the measured 5s: {tight}"


def test_controlplane_is_probed_through_the_SAME_helper_as_the_fleet() -> None:
    """The second hand-written probe block is what let controlplane miss two fixes in a row.

    It is rendered by its own template rather than fleet.yaml's range, so it needed the `timeoutSeconds`
    fix copied by hand and never got the startupProbe at all. Asserting the two render IDENTICALLY is
    what makes a third divergence impossible — a shared helper is only worth anything if nothing is
    allowed to drift from it.
    """
    probed = dict(_http_probed_containers())
    controlplane = next(container for name, container in probed.items() if name.endswith("-controlplane/controlplane"))
    compute = next(container for name, container in probed.items() if name.endswith("-compute/compute"))

    for kind in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert controlplane.get(kind) == compute.get(kind), (
            f"controlplane's {kind} differs from the fleet's: {controlplane.get(kind)} vs {compute.get(kind)} — the hand-written second copy has drifted again"
        )
