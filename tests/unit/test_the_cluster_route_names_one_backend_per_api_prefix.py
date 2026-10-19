"""The cluster route, not a rask process, maps each public `/api/*` prefix onto the service that serves it (ADR 0156, ADR 0165).

The table below is the public surface the zones, SSR and stock Lance clients address: a prefix, the Service and port it
reaches, and the internal prefix the route replaces it with (`URLRewrite` `ReplacePrefixMatch`), or `None` where the
service serves the public path itself. Compute's rows come from the compute fixture's own route, because the product
chart renders no part of a bring-your-own engine's companion (ADR 0155), and the annotator's from its own release's
route, because the lakehouse knows no annotation tool (ADR 0171). A render that still names a gateway object keeps a
second router in the request path.
"""

from __future__ import annotations

from tests.unit.chart_render import ANNOTATOR_FIXTURE_ARGS, DEFAULT_ARGS, render, render_fixture


#: public prefix -> (Service, port, internal prefix the route rewrites to, or None for unchanged)
_PRODUCT_API = {
    "/api/explorer/search": ("rask-search", 8102, "/api/search"),
    "/api/explorer": ("rask-viewer", 8101, "/api"),
    "/api/catalog": ("rask-catalog", 2333, "/"),
    "/api/lineage": ("rask-lineage", 8000, "/"),
    "/api/flows": ("rask-flows", 8840, None),
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
    product = render(*DEFAULT_ARGS, "--set", "explorer.enabled=true")
    compute = render_fixture("compute", "--set", "auth.oidc.issuer=https://auth.example.com/dex")
    annotator = render_fixture("annotator", *ANNOTATOR_FIXTURE_ARGS)

    assert _api_rows(product) == _PRODUCT_API
    assert _api_rows(compute) == _COMPUTE_API
    assert _api_rows(annotator) == _ANNOTATOR_API
    leftovers = [
        f"{d['kind']}/{d['metadata']['name']}"
        for d in (*product, *compute, *annotator)
        if d.get("kind") == "Ingress" or "gateway" in d.get("metadata", {}).get("name", "")
    ]
    assert leftovers == []
