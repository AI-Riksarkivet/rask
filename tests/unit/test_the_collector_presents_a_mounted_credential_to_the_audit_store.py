"""The in-chart Collector presents a credential on every exporter that writes the audit store, from a file ESO delivers.

[[XC-003]], owner decision 2026-10-10: the audit store's contract is that it refuses an unauthenticated write or delete
(values.yaml `observability`), and the Collector is the seam rask owns, so it is the Collector that must present one. The
credential lives in the store (`secret/otel-collector-greptime`, seeded create-if-absent by the dev OpenBao), ESO writes it into
a Secret, and the Collector reads it as mounted files through the basicauth extension's `*_file` settings, which it watches,
so a rotation reaches it without a restart. An exporter without the authenticator is refused by an enforcing store; an
extension missing from `service.extensions` stops the Collector at start; an inline password or an env-delivered one breaks
the secrets rule.
"""

from __future__ import annotations

import posixpath

import yaml

from tests.unit import chart_render
from tests.unit.chart_render import DEFAULT_ARGS


def _named(docs: tuple[dict, ...], kind: str, name: str) -> dict:
    found = [d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"the chart renders {len(found)} {kind}/{name}"
    return found[0]


def test_every_audit_store_exporter_presents_a_credential_eso_mounts_as_a_file() -> None:
    docs = chart_render.render(*DEFAULT_ARGS)

    config = yaml.safe_load(_named(docs, "ConfigMap", "rask-otel-collector")["data"]["config.yaml"])
    pod = _named(docs, "Deployment", "rask-otel-collector")["spec"]["template"]["spec"]
    [collector] = pod["containers"]
    store_exporters = {name: spec for name, spec in config["exporters"].items() if name.startswith("otlphttp/greptime")}
    authenticators = {name: (spec.get("auth") or {}).get("authenticator") for name, spec in store_exporters.items()}

    assert sorted(store_exporters) == ["otlphttp/greptime", "otlphttp/greptime_audit", "otlphttp/greptime_traces"]
    assert None not in authenticators.values(), f"an exporter writes the audit store unauthenticated: {authenticators}"
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
