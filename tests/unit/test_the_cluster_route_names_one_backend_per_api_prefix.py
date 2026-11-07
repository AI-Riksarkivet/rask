"""The cluster route, not a rask process, maps each public prefix onto the service that serves it (ADR 0156, ADR 0165).

The table below is the lakehouse's public surface, which every client and stock Lance client addresses: a prefix, the Service and port it
reaches, and the internal prefix the route replaces it with (`URLRewrite` `ReplacePrefixMatch`), or `None` where the
service serves the public path itself. `/` is the console's, the lakehouse's own UI, and its Service reaches the
console's pods on their port; every `/api/*` prefix and the development Dex's `/dex` are longer and keep winning. The
catalog serves its own: a rewrite rebuilds the path from its decoded form, so the `%2B` and `%2F` its doors judge a name
by would arrive as `+` and `/` ([[LH-526]]), and the catalog strips the prefix its route forwards, named by the same
template value. A client's route is its own release's (ADR 0155, ADR 0171).
A render that still names a gateway object keeps a second router in the request path.

A route with no hostname claims every host its Gateway's listener accepts, and Gateway API settles two such claims on
one prefix by the oldest route, so a second release attached host-less to the same listener silently takes or loses
`/` and `/api`. A real deployment (`rask.isRealDeployment`) therefore names the hosts its routes answer, and the render
refuses one that names none ([[XC-146]]); a development estate's routes stay host-less.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, PROD_ARGS, render, render_text, selects


#: public prefix -> (Service, port, internal prefix the route rewrites to, or None for unchanged)
_PRODUCT_ROUTES = {
    "/api/catalog": ("rask-catalog", 2333, None),
    "/api/lineage": ("rask-lineage", 8000, "/"),
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
