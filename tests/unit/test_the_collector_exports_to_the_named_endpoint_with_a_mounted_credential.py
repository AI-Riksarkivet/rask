"""The Collector sends every signal to the endpoint the operator names, presenting a credential ESO mounts as files.

ADR 0153 (XC-127): the store, its dashboards and its alerting are the platform's, so rask's Collector is the seam and
`observability.export` is its whole contract: every pipeline leaves through an exporter aimed at `export.endpoint`, with
the backend's headers from values, and nothing else in the render points telemetry anywhere. [[XC-003]], owner decision
2026-10-10: the store refuses an unauthenticated write or delete, so every exporter presents the credential
(`secret/otel-collector-export` in the store, seeded create-if-absent by the dev OpenBao), which ESO writes into a
Secret and the Collector reads as mounted files through the basicauth extension's `*_file` settings, which it watches,
so a rotation reaches it without a restart. An exporter without the authenticator is refused by an enforcing store; an
extension missing from `service.extensions` stops the Collector at start; an inline password or an env-delivered one
breaks the secrets rule.
"""

from __future__ import annotations

import posixpath

import yaml

from tests.unit import chart_render
from tests.unit.chart_render import DEFAULT_ARGS, OBSERVABILITY_ARGS


_ENDPOINT = "https://otlp.platform.example/v1/otlp"
_NAMED = (
    *OBSERVABILITY_ARGS,
    "--set-string", f"observability.export.endpoint={_ENDPOINT}",
    "--set-string", "observability.export.headers.x-tenant=rask",
    "--set-string", "observability.export.traceHeaders.x-trace-route=spans",
    "--set-string", "observability.export.auditHeaders.x-log-table=audit",
)  # fmt: skip


def _named(docs: tuple[dict, ...], kind: str, name: str) -> dict:
    found = [d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"the chart renders {len(found)} {kind}/{name}"
    return found[0]


def test_every_signal_leaves_for_the_named_endpoint_presenting_a_credential_eso_mounts_as_a_file() -> None:
    docs = chart_render.render(*DEFAULT_ARGS, *_NAMED)

    config = yaml.safe_load(_named(docs, "ConfigMap", "rask-otel-collector")["data"]["config.yaml"])
    pod = _named(docs, "Deployment", "rask-otel-collector")["spec"]["template"]["spec"]
    [collector] = pod["containers"]
    used = {exporter for pipeline in config["service"]["pipelines"].values() for exporter in pipeline["exporters"]}
    exporters = {name: config["exporters"][name] for name in used}
    routes = {(name, spec["endpoint"], tuple(sorted(spec["headers"].items()))) for name, spec in exporters.items()}

    assert routes == {
        ("otlphttp", _ENDPOINT, (("x-greptime-db-name", "public"), ("x-tenant", "rask"))),
        (
            "otlphttp/traces",
            _ENDPOINT,
            (("x-greptime-db-name", "public"), ("x-greptime-pipeline-name", "greptime_trace_v1"), ("x-tenant", "rask"), ("x-trace-route", "spans")),
        ),
        (
            "otlphttp/audit",
            _ENDPOINT,
            (("x-greptime-db-name", "public"), ("x-greptime-log-table-name", "lance_audit"), ("x-log-table", "audit"), ("x-tenant", "rask")),
        ),
    }, f"a pipeline leaves for somewhere other than the named endpoint, or without its headers: {sorted(routes)}"
    authenticators = {name: (spec.get("auth") or {}).get("authenticator") for name, spec in exporters.items()}
    assert None not in authenticators.values(), f"an exporter writes the store unauthenticated: {authenticators}"
    [authenticator] = set(authenticators.values())
    assert authenticator in config["service"]["extensions"], f"{authenticator} is not started, so the Collector refuses its config"
    client = config["extensions"][authenticator]
    assert sorted(client) == ["client_auth"] and sorted(client["client_auth"]) == ["password_file", "username_file"], (
        f"the credential must come from watched files, never inline: {client}"
    )
    files = [client["client_auth"]["username_file"], client["client_auth"]["password_file"]]
    mounts = {(m["mountPath"], m["name"]) for m in collector["volumeMounts"] for f in files if posixpath.dirname(f) == m["mountPath"].rstrip("/")}
    assert len(mounts) == 1, f"{files} are not served by one volume the Collector mounts: {mounts}"
    [(mount, volume)] = mounts
    [secret] = [v["secret"] for v in pod["volumes"] if v["name"] == volume]
    readable = secret.get("defaultMode", 0o644) & 0o004 or (pod.get("securityContext") or {}).get("fsGroup") is not None
    assert readable, f"{mount} is not readable by uid {collector['securityContext']['runAsUser']}"
    external = _named(docs, "ExternalSecret", secret["secretName"])["spec"]
    delivered = {d["secretKey"] for d in external["data"]} | set((external["target"].get("template") or {}).get("data") or {})
    assert {posixpath.basename(f) for f in files} <= delivered, f"ESO writes {sorted(delivered)} into {secret['secretName']}, not the files the Collector reads"
    in_env = [e["name"] for e in collector.get("env") or [] if ((e.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name") == secret["secretName"]]
    assert not in_env, f"the store credential also reaches the Collector's environment: {in_env}"
