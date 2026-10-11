# chart/templates/network-policy.yaml

Rationale for `chart/templates/network-policy.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $openbaoClients := list "catalog" "lineage" "maintenance" "maintenance-worker" "medallion-producer" }}`

stateStore.scopes is APPENDED + uniq'd (app-id == component label for these
pods — media.yaml keys them identically), so this list and the secret-store
component's scope (dapr-component.yaml) cannot drift.

## `{{- if .Values.openbao.devMode }}`

the dev seed reads the signing pairs from the outgoing OpenBao pod (openbao.yaml)

## `{{- $ageClients := list "lineage" "catalog" "openfga-migrate" "backup-pg" }}`

stateStore.scopes appended + uniq'd, same derivation as the openbao rule above.

## `{{- if and .Values.observability.enabled .Values.stateStore.enabled }}`

The OTel Collector is a 5432 client too: its `sqlquery/daprstate`
receiver reads the workflow-history volume and age that nothing else can
measure (daprd exports no workflow or state-store metric at all). Blocked here
and the receiver retries forever, DaprWorkflowStateMetricsMissing fires, and
the retention alert it guards goes blind. Gated on the conditions that render
the Collector and its receiver, so an estate without them carries no
allow-entry for a pod it never deploys.
