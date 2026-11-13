"""The cluster route, not a rask process, maps each public prefix onto the service that serves it (ADR 0156, ADR 0165).

The table below is the lakehouse's public surface, which every client and stock Lance client addresses: a prefix, the Service and port it
reaches, and the internal prefix the route replaces it with (`URLRewrite` `ReplacePrefixMatch`), or `None` where the
service serves the public path itself. `/` is the console's, the lakehouse's own UI, and its Service reaches the
console's pods on their port; every `/api/*` prefix and the development Dex's `/dex` are longer and keep winning. The
catalog serves its own: a rewrite rebuilds the path from its decoded form, so the `%2B` and `%2F` its doors judge a name
by would arrive as `+` and `/` ([[LH-526]]), and the catalog strips the prefix its route forwards, named by the same
template value. A client's route is its own release's (ADR 0155, ADR 0171).
A render that still names a gateway object keeps a second router in the request path.

The route publishes each service's doors by name, never its whole root: a prefix over the root also publishes every
route the pod answers for its own platform (the kubelet's probes, the sidecar's `/dapr/subscribe`, the Dapr deliveries
and cron ticks), and every route the service mounts later. Which routes a service mounts follows the env its pod is
rendered with, so the gate boots each app under that env and holds the published set to what it mounts less those.
An `/api/*` path no rule names falls to the console's `/`, whose server answers it 404.

A route with no hostname claims every host its Gateway's listener accepts, and Gateway API settles two such claims on
one prefix by the oldest route, so a second release attached host-less to the same listener silently takes or loses
`/` and `/api`. A real deployment (`rask.isRealDeployment`) therefore names the hosts its routes answer, and the render
refuses one that names none ([[XC-146]]); a development estate's routes stay host-less.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, PROD_ARGS, render, render_text, selects
from tests.unit.served_routes import served


#: public prefix -> (Service, port, internal prefix the route rewrites to, or None for unchanged); docs off, as by default
_PRODUCT_ROUTES = {
    "/api/catalog/v1": ("rask-catalog", 2333, None),
    "/api/catalog/management/v1": ("rask-catalog", 2333, None),
    "/api/lineage/events": ("rask-lineage", 8000, "/events"),
    "/api/lineage/api/v1/lineage": ("rask-lineage", 8000, "/api/v1/lineage"),
    "/api/lineage/admin/dlq": ("rask-lineage", 8000, "/admin/dlq"),
    "/api/lineage/tables": ("rask-lineage", 8000, "/tables"),
    "/dex": ("rask-dex", 5556, None),
    "/": ("rask-console", 3100, None),
}

#: A console port other than the image's own 3000, so a server left on the image's port answers nothing the render names.
_CONSOLE_PORT = 3100


def _route_rows(docs: tuple[dict, ...]) -> dict[str, tuple[str, int, str | None]]:
    rows: dict[str, tuple[str, int, str | None]] = {}
    for doc in docs:
        if doc.get("kind") != "HTTPRoute":
            continue
        for rule in doc["spec"]["rules"]:
            (backend,) = rule["backendRefs"]
            rewrite = next((f["urlRewrite"]["path"]["replacePrefixMatch"] for f in rule.get("filters", []) if f["type"] == "URLRewrite"), None)
            for match in rule["matches"]:
                assert match["path"]["type"] == "PathPrefix", match
                rows[match["path"]["value"]] = (backend["name"], backend["port"], rewrite)
    return rows


def test_each_public_prefix_reaches_its_service_through_the_cluster_route_and_no_gateway_remains() -> None:
    product = render(*DEFAULT_ARGS, "--set", f"console.port={_CONSOLE_PORT}")

    assert _route_rows(product) == _PRODUCT_ROUTES
    [service] = [d for d in product if d.get("kind") == "Service" and d["metadata"]["name"] == "rask-console"]
    [(port, target)] = [(p["port"], p["targetPort"]) for p in service["spec"]["ports"]]
    pods = [
        d["spec"]["template"]["spec"]
        for d in product
        if d.get("kind") == "Deployment" and selects({"matchLabels": service["spec"]["selector"]}, d["spec"]["template"]["metadata"]["labels"])
    ]
    served = [
        (c["image"], p["containerPort"], {e["name"]: e.get("value") for e in c.get("env", [])}.get("PORT"))
        for pod in pods
        for c in pod["containers"]
        for p in c.get("ports", [])
        if p["name"] == target
    ]
    assert (port, served) == (_CONSOLE_PORT, [("web-console:dev", _CONSOLE_PORT, str(_CONSOLE_PORT))]), (
        "the console's Service reaches no console server listening on the port its route names"
    )
    catalog = next(d for d in product if d.get("kind") == "Deployment" and d["metadata"]["name"] == "rask-catalog")
    env = {e["name"]: e.get("value") for e in catalog["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["LANCE_PUBLIC_PATH_PREFIX"] == "/api/catalog", "the catalog strips another prefix than the one its route forwards"
    leftovers = [f"{d['kind']}/{d['metadata']['name']}" for d in product if d.get("kind") == "Ingress" or "gateway" in d.get("metadata", {}).get("name", "")]
    assert leftovers == []


#: What a pod answers for its platform alone, beside its Dapr deliveries and ticks: the kubelet's probes and the sidecar's
#: subscription read.
_PLATFORM_ROUTES = frozenset({"/livez", "/readyz", "/dapr/subscribe"})


def _reaches(prefix: str, path: str) -> bool:
    """Whether a `PathPrefix` on `prefix` matches `path`, element by element as Gateway API matches."""
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


@pytest.mark.parametrize(
    ("deployment", "module"), [pytest.param("rask-catalog", "catalog.main", id="catalog"), pytest.param("rask-lineage", "lineage.main", id="lineage")]
)
def test_the_route_publishes_each_door_the_service_mounts_and_nothing_only_its_pod_answers(deployment: str, module: str) -> None:
    product = render(*DEFAULT_ARGS, "--set", "docs.enabled=true")
    [template] = [d["spec"]["template"] for d in product if d.get("kind") == "Deployment" and d["metadata"]["name"] == deployment]
    env = {e["name"]: e["value"] for e in template["spec"]["containers"][0]["env"] if "value" in e}
    served_by_pod = served(module, env)
    # Dapr delivers a cron binding's tick to POST /<component name> on each app the component is scoped to.
    app_id = template["metadata"]["annotations"]["dapr.io/app-id"]
    ticks = {
        f"/{d['metadata']['name']}" for d in product if d.get("kind") == "Component" and d["spec"]["type"] == "bindings.cron" and app_id in d.get("scopes", [])
    }
    # The internal prefix each rule lands on: its rewrite, or the public match less the prefix the service strips itself.
    landings = {
        match: rewrite if rewrite is not None else match.removeprefix(env.get("LANCE_PUBLIC_PATH_PREFIX", ""))
        for match, (backend, _, rewrite) in _route_rows(product).items()
        if backend == deployment
    }

    published = {path for path in served_by_pod.paths if any(_reaches(landing, path) for landing in landings.values())}
    dead = sorted(match for match, landing in landings.items() if not any(_reaches(landing, path) for path in served_by_pod.paths))

    assert sorted(published) == sorted(served_by_pod.paths - _PLATFORM_ROUTES - served_by_pod.deliveries - ticks), (
        "the route publishes a path only the pod should answer, or leaves a door unpublished"
    )
    assert dead == [], "a rule publishes a prefix the service mounts nothing under"


def test_a_real_deployment_that_names_no_hostname_is_refused() -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*PROD_ARGS, "--set-json", "route.hostnames=[]")

    assert (
        "route.hostnames must name the hosts the platform's Gateway routes to this release on a real deployment: "
        "a route with no hostname claims every host its listener accepts"
    ) in refused.value.stderr


def test_each_route_of_a_real_deployment_carries_the_hostnames_it_names() -> None:
    routes = {doc["metadata"]["name"]: doc["spec"].get("hostnames") for doc in render(*PROD_ARGS) if doc.get("kind") == "HTTPRoute"}

    assert routes == {"rask-api": ["lance.example.com"], "rask-console": ["lance.example.com"]}
