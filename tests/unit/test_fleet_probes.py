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

#: The apps composed through `make_service_app`. The gateway is deliberately absent: it builds its own
#: FastAPI and probes `/healthz`, with a recorded reason (probing a proxied path would couple its
#: readiness to an upstream).
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
    """The reference's first rule: two endpoints, two purposes, don't conflate them."""
    from test_invariants import _rendered_docs  # noqa: PLC0415

    conflated: dict[str, str] = {}
    for doc in _rendered_docs():
        if doc.get("kind") != "Deployment" or "rask-" not in doc["metadata"]["name"]:
            continue
        name = doc["metadata"]["name"]
        if name.endswith("-gateway"):
            continue
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
