# chart/templates/dapr-statestore.yaml

Rationale for `chart/templates/dapr-statestore.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.dapr.enabled .Values.stateStore.enabled }}`

The `dapr.enabled` half is not optional: this renders a dapr.io/v1alpha1 Component, served only
where a Dapr control plane is installed (a prerequisite; with `dapr.enabled` on and no dapr.io API the
render refuses, templates/dapr-component.yaml). Every sibling Dapr-CR template gates on `dapr.enabled`,
so `dapr.enabled=false` renders no Component a cluster without Dapr could not accept.

## `secretKeyRef:`

WITH NO DAPR SECRET STORE THIS RESOLVES AGAINST KUBERNETES SECRETS, and the chart declares
no `lance` Secret — so an install with `openbao.enabled=false` and no external address must
supply one itself. Nothing warns: daprd exits `Fatal error from runtime: failed to load
components: Secret "lance" not found`, which kills the SIDECAR, so every app in the release
fails its health check for a reason naming neither this component nor the flag behind it.
Measured 2026-09-24 on `e2e-ray`, which set `openbao.enabled=false` and supplied nothing:
medallion-producer and bronze-to-silver both died that way seven minutes into a deploy, and
the visible symptom was `TimeoutError: Dapr health check timed out`. The stack scripts now
keep the store on, and `tests/unit/test_a_dapr_component_names_a_secret_something_can_
resolve.py` holds every overlay they deploy to that rule.

## `{{- if include "lance.secretsViaDapr" . }}`

THE WHOLE BLOCK IS CONDITIONAL, not just its one child. `lance-secrets` is gated on
lance.secretsViaDapr (openbao.enabled or an external addr), so naming it unconditionally
pointed the state store at a component that need not exist — daprd then logs 'references a
secret store that isn't loaded', the state store silently does not load, and actor hosting
goes with it. Without a secret store Dapr falls back to Kubernetes Secrets, which is the
honest behaviour for an install that has no OpenBao.

Emitting the `auth:` key with its only child behind the guard renders YAML null, which the
Component CRD refuses outright:
  Component.dapr.io "lance-statestore" is invalid:
  auth: Invalid value: "null": auth in body must be of type object: "null"
Measured 2026-09-24 on `e2e-ray`, whose overlay sets `openbao.enabled=false` — so the apply
failed on the state store that hosts every actor, and the stack never came up.

## `{{- $scopes := .Values.stateStore.scopes | default list }}`

No stage runner is in scope: a Ray stage is planned and resolved without a workflow (CP-029), so no stage
runner hosts a workflow runtime or an actor. An app-id that hosts one is scoped; unscoped, its sidecar logs
"Workflow engine started" after "actor hosting disabled" and every workflow fails (measured live 2026-08-15).

## `{{- if .Values.medallion.qualityReview }}`

The PRODUCER's app-id under `qualityReview`, the one condition on which producer.py starts a workflow runtime:
`promotion_review` runs there, beside the approve route, because `raise_workflow_event` resolves the instance
through the calling app's app-id. A training run is a plan (CP-029) and needs no actor.
