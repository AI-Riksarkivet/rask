# chart/templates/observability.yaml

Rationale for `chart/templates/observability.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: dapr.io/v1alpha1`

THE SIDECAR Configuration. Named for tracing because that is all it carried when it was written; it
also carries the sidecar metric-cardinality bound and the api-logging policy, and it renders whether
or not telemetry ships.

**Why it is no longer gated on otelEnabled.** A sidecar may reference exactly ONE `dapr.io/config`,
so everything configured per-sidecar has to live in one object, and only the tracing stanza is about
telemetry. The gate lives on the tracing STANZA, which is the thing
that is actually about telemetry, and the annotation in `rask.daprAnnotations` is unconditional to
match. That direction is safe: the guard the old comment protected was against a DANGLING
`dapr.io/config` reference, and a Configuration that always exists cannot dangle.

NO EDGE-AUTH SECRET FOR THE TELEMETRY UIs: no cluster route rule publishes them, so they are
cluster-internal (port-forward) and the store, its dashboards and their auth are the platform's (ADR 0153).
An edge that re-exposes them carries the auth itself, wired to that edge's own machinery.

## `{{- $cfgRoot := . }}`

ONE CONFIGURATION PER SCOPED APP-ID ([[XC-072]]). Dapr's secret scoping has nowhere else to live:
`spec.secrets.scopes` is a CONFIGURATION field, a sidecar may reference exactly one `dapr.io/config`, and
Dapr's default for an unlisted store is ALLOW. Measured live 2026-09-23 under one shared Configuration that
named no scope, the `medallion-producer` pod read eight identities' credentials from its own sidecar.

THE SPEC BODY IS INCLUDED, NOT COPIED. HotReload, the tracing exporter and the metric-cardinality bound
are each a measured decision made once above; a per-app copy would drift from
`lance-tracing` silently and each sidecar would get whichever version its document carried.

EACH APP'S SCOPE DENIES THE SIDECAR'S SECRETS API WHOLE (`lance.daprAppSpec`: `defaultAccess: deny` and no allow
list, [[XC-165]], [[XC-199]]). No app reads a secret through its sidecar: a service reads under its own login to the
store, whose policy names exactly its secrets (`lance.storeReads`). That policy is also all that bounds a sidecar's own
login, since a Configuration's scope never binds a component's `secretKeyRef`.
