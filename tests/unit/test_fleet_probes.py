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


def test_the_gateway_probe_REPORTS_the_drain() -> None:
    """The gateway keeps `/healthz` for both probes, and that exception is recorded and correct —
    probing a proxied path would couple its readiness to an upstream. But a constant cannot say
    "draining", and the gateway is the INGRESS: a rolling update that keeps it in rotation through
    SIGTERM drops in-flight requests at the one hop every request passes through.

    open_fastapi-audit names this explicitly: "even it should set `app.state.shutting_down` so its
    `/healthz` can report the drain".
    """
    import gateway

    # THROUGH THE LIFESPAN, which is what makes this order-independent. `gateway.app` is a module
    # singleton and the lifespan's `finally` sets `shutting_down = True`, so any earlier test that ran
    # a TestClient context leaves the flag set — this test then read 503 for its 200 precondition and
    # failed on a state it never established. Entering the lifespan sets it False the way a real boot
    # does, rather than patching the flag to paper over the leak.
    with TestClient(gateway.app) as client:
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
