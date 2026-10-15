# chart/templates/network-policy.yaml

Rationale for `chart/templates/network-policy.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $openbaoClients := list "catalog" "lineage" "maintenance" }}`

stateStore.scopes is APPENDED + uniq'd (app-id == component label for these
pods — media.yaml keys them identically), so this list and the secret-store
component's scope (dapr-component.yaml) cannot drift.

## `{{- if .Values.openbao.devMode }}`

the dev seed reads the signing pairs from the outgoing OpenBao pod (openbao.yaml)

## `{{- $ageClients := list "catalog" "openfga-migrate" "backup-pg" }}`

stateStore.scopes appended + uniq'd, same derivation as the openbao rule above.

## `{{- if and .Values.observability.enabled .Values.stateStore.enabled }}`

The OTel Collector is a 5432 client too: its `sqlquery/daprstate`
receiver reads the workflow-history volume and age that nothing else can
measure (daprd exports no workflow or state-store metric at all). Blocked here
and the receiver retries forever, DaprWorkflowStateMetricsMissing fires, and
the retention alert it guards goes blind. Gated on the conditions that render
the Collector and its receiver, so an estate without them carries no
allow-entry for a pod it never deploys.

## `name: {{ include "lance.fullname" . }}-allow-dapr-control-plane-egress`

Every sidecar dials the Dapr control plane (Sentry for its certificate, placement, the scheduler, the operator's
API), which is a prerequisite in its own namespace (`dapr.controlPlaneNamespace`, ADR 0153), so the intra-namespace
egress allow no longer covers it. One rule for the whole namespace rather than per component: the plane's ports are
the platform's to choose.

## `name: {{ include "lance.fullname" . }}-allow-telemetry-export-egress`

The Collector exports to `observability.export.endpoint`, the platform's backend, usually outside the namespace;
default-deny egress would drop that silently while the Collector's queue retries into nothing. The port is derived
from the URL (`lance.urlPort`: explicit, else 443 for https, 80 for http). `prod-render-check` asserts it on :4318.

## `name: {{ include "lance.fullname" $ }}-allow-telemetry-query-egress`

The zones read the store's SQL and PromQL API at `observability.query.url` (the audit viewer, admin pages and
experiment curves); the same derivation opens its port for the zone pods only.
