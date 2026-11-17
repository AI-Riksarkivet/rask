"""Each lakehouse service sends its telemetry to the endpoint the deployer names, presenting its own ServiceAccount token.

The telemetry pipeline (the Collector, the store, how the audit trail is routed) is the platform's (ADR 0153, ADR 0199),
so the product chart renders none of it, and its contract is the service's half alone: every process launched under
`opentelemetry-instrument` exports OTLP to `observability.export.endpoint`, and its exporters load the
`rask_service_account` session, which presents the token the kubelet projects for `observability.identity.audience`
from the file `RASK_OTLP_TOKEN_FILE` names. That file must sit in a projected token volume of that audience mounted into
the same container, or every export is refused at the platform's gateway (or, with no file, never sent).
"""

from __future__ import annotations

import posixpath

from tests.chart import chart_render
from tests.chart.chart_render import DEFAULT_ARGS


_ENDPOINT = "https://otlp.platform.example"
_AUDIENCE = "platform-otlp"
_NAMED = (
    "--set", "observability.enabled=true",
    "--set-string", f"observability.export.endpoint={_ENDPOINT}",
    "--set-string", f"observability.identity.audience={_AUDIENCE}",
)  # fmt: skip


def test_every_instrumented_process_exports_to_the_named_endpoint_with_its_own_projected_token() -> None:
    docs = chart_render.render(*DEFAULT_ARGS, *_NAMED)
    pods = {f"{doc['kind']}/{doc['metadata']['name']}": doc["spec"]["template"]["spec"] for doc in docs if doc.get("kind") == "Deployment"}

    found = {}
    for workload, pod in pods.items():
        audiences = {
            volume["name"]: source["serviceAccountToken"].get("audience")
            for volume in pod.get("volumes") or []
            for source in (volume.get("projected") or {}).get("sources") or []
            if "serviceAccountToken" in source
        }
        for container in pod["containers"]:
            if "opentelemetry-instrument" not in (container.get("args") or []):
                continue
            env = {e["name"]: e.get("value") for e in container.get("env") or []}
            token = env.get("RASK_OTLP_TOKEN_FILE") or ""
            mounted = [audiences.get(m["name"]) for m in container.get("volumeMounts") or [] if posixpath.dirname(token) == m["mountPath"].rstrip("/")]
            found[f"{workload}/{container['name']}"] = (
                env.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
                env.get("OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER"),
                mounted,
            )

    assert found == dict.fromkeys(
        ("Deployment/rask-catalog/catalog", "Deployment/rask-lineage/lineage", "Deployment/rask-maintenance-worker/worker"),
        (_ENDPOINT, "rask_service_account", [_AUDIENCE]),
    )
