"""The cluster route, not a rask process, maps each public `/api/*` prefix onto the service that serves it (ADR 0156, ADR 0165).

The table below is the public surface the zones, SSR and stock Lance clients address: a prefix, the Service and port it
reaches, and the internal prefix the route replaces it with (`URLRewrite` `ReplacePrefixMatch`), or `None` where the
service serves the public path itself. The catalog serves its own: a rewrite rebuilds the path from its decoded form, so
the `%2B` and `%2F` its doors judge a name by would arrive as `+` and `/` ([[LH-526]]), and the catalog strips the
prefix its route forwards, named by the same template value. Compute's rows come from the compute fixture's own route, because the product
chart renders no part of a bring-your-own engine's companion (ADR 0155), and the annotator's from its own release's
route, because the lakehouse knows no annotation tool (ADR 0171). A render that still names a gateway object keeps a
second router in the request path.

A route with no hostname claims every host its Gateway's listener accepts, and Gateway API settles two such claims on
one prefix by the oldest route, so a second release attached host-less to the same listener silently takes or loses
`/` and `/api`. A real deployment (`rask.isRealDeployment`) therefore names the hosts its routes answer, and the render
refuses one that names none ([[XC-146]]); a development estate's routes stay host-less.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.chart_render import ANNOTATOR_FIXTURE_ARGS, DEFAULT_ARGS, PROD_ARGS, render, render_fixture, render_text


#: public prefix -> (Service, port, internal prefix the route rewrites to, or None for unchanged)
_PRODUCT_API = {
    "/api/catalog": ("rask-catalog", 2333, None),
    "/api/lineage": ("rask-lineage", 8000, "/"),
    "/api/notifications": ("rask-notifications", 8850, None),
}
_COMPUTE_API = {
    "/api/ray": ("rask-compute", 8804, None),
    "/api/serve": ("rask-compute", 8804, None),
}
_ANNOTATOR_API = {
    "/api/explorer/annotations": ("rask-annotator", 8103, "/api/annotations"),
}


def _api_rows(docs: tuple[dict, ...]) -> dict[str, tuple[str, int, str | None]]:
    rows: dict[str, tuple[str, int, str | None]] = {}
    for doc in docs:
        if doc.get("kind") != "HTTPRoute":
            continue
        for rule in doc["spec"]["rules"]:
            (backend,) = rule["backendRefs"]
            rewrite = next((f["urlRewrite"]["path"]["replacePrefixMatch"] for f in rule.get("filters", []) if f["type"] == "URLRewrite"), None)
            for match in rule["matches"]:
                assert match["path"]["type"] == "PathPrefix", match
                prefix = match["path"]["value"]
                if prefix.startswith("/api"):
                    rows[prefix] = (backend["name"], backend["port"], rewrite)
    return rows


def test_each_api_prefix_reaches_its_service_through_the_cluster_route_and_no_gateway_remains() -> None:
    product = render(*DEFAULT_ARGS)
    compute = render_fixture("compute", "--set", "auth.oidc.issuer=https://auth.example.com/dex")
    annotator = render_fixture("annotator", *ANNOTATOR_FIXTURE_ARGS)

    assert _api_rows(product) == _PRODUCT_API
    catalog = next(d for d in product if d.get("kind") == "Deployment" and d["metadata"]["name"] == "rask-catalog")
    env = {e["name"]: e.get("value") for e in catalog["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["LANCE_PUBLIC_PATH_PREFIX"] == "/api/catalog", "the catalog strips another prefix than the one its route forwards"
    assert _api_rows(compute) == _COMPUTE_API
    assert _api_rows(annotator) == _ANNOTATOR_API
    leftovers = [
        f"{d['kind']}/{d['metadata']['name']}"
        for d in (*product, *compute, *annotator)
        if d.get("kind") == "Ingress" or "gateway" in d.get("metadata", {}).get("name", "")
    ]
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

    assert routes == {"rask-api": ["lance.example.com"]}
