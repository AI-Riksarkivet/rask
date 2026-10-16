# chart/templates/dapr-statestore.yaml

Rationale for `chart/templates/dapr-statestore.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.dapr.enabled .Values.stateStore.enabled }}`

The `dapr.enabled` half is not optional: this renders a dapr.io/v1alpha1 Component, served only
where a Dapr control plane is installed (a prerequisite; with `dapr.enabled` on and no dapr.io API the
render refuses, templates/dapr-component.yaml). Every sibling Dapr-CR template gates on `dapr.enabled`,
so `dapr.enabled=false` renders no Component a cluster without Dapr could not accept.

## `secretKeyRef:`

WITH NO DAPR SECRET STORE THIS WOULD RESOLVE AGAINST KUBERNETES SECRETS, and the chart declares
no `lance` Secret: daprd exits `Fatal error from runtime: failed to load components: Secret "lance"
not found`, which kills the SIDECAR, so every app in the release fails its health check for a
reason naming neither this component nor the flag behind it. Measured 2026-09-24 on `e2e-ray`,
an overlay that named no store: medallion-producer and bronze-to-silver both died that way seven
minutes into a deploy, and the visible symptom was `TimeoutError: Dapr health check timed out`.
So every render names a store, the dev OpenBao (`openbao.enabled`) or the platform's
(`openbao.externalAddr`, which `lance.platformStoreAddr` requires otherwise), and
`tests/unit/test_a_dapr_component_names_a_secret_something_can_resolve.py` holds every overlay the
stack scripts deploy to that rule.

## `auth:`

UNCONDITIONAL, like the `lance-secrets` component it names: every render names a store, so the state
store never names a component the render does not hold. Naming one that is absent makes daprd log
'references a secret store that isn't loaded', the state store silently does not load, and actor
hosting goes with it.

## `{{- $scopes := .Values.stateStore.scopes | default list }}`

An app-id that hosts a workflow runtime or an actor is scoped; unscoped, its sidecar logs
"Workflow engine started" after "actor hosting disabled" and every workflow fails (measured live 2026-08-15).
