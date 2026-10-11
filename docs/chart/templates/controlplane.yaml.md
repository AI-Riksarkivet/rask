# chart/templates/controlplane.yaml

Rationale for `chart/templates/controlplane.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `replicas: {{ (hasKey .Values.controlplane "replicas") | ternary .Values.controlplane.replicas 1 }}`

A LITERAL until 2026-08-27, so this service could not be scaled from values at all — the
same "second hand-written template" that kept it out of every `services.*`-keyed probe fix.
Default 1: the prod overlay is what bumps it, alongside its PDB.

## `terminationGracePeriodSeconds: {{ .Values.lifecycle.terminationGracePeriodSeconds }}`

The drain budget, from the same `lifecycle` block as preStop and the sidecar block —
see the arithmetic in values.yaml. These pods declared none, so they silently took the
kubelet's 30s default: a number nobody chose and no comment describes, on pods that all
run a Dapr sidecar whose flush the default does not budget for.

## `{{- include "lance.governedOidcEnv" . | nindent 12 }}`

THE USER DOOR. `GET /api/projects/` enumerates every tenant in the estate — slug,
team, workload type, k8s namespace and each project's LIVE INGRESS HOST — and the
gateway carries it to the public edge. It answered anonymously until 2026-08-26
because this service declared no settings class with the estate's auth knobs, so
there was no `RASK_OIDC_ENABLED` to bind and no values change could gate it.
It now declares ControlplaneSettings(GovernedAuthSettings, BaseSettings) and gates
its router on the estate reader tier; this env is what feeds that door. Emitting
the block without the code, or the code without the block, both look wired and do
nothing — the `explorer.yaml` failure. Prefix is LANCE_, matching the mixin.

## `{{- include "rask.fleetProbes" dict | nindent 10 }}`

Two questions, two paths — see fleet.yaml. `/api/health` is a static badge, so probing
it for READINESS reports "ready" while the pod drains; `/readyz` reports lifecycle
state. This file renders its own container and so had its own copy of the conflated
pair.

The SAME helper fleet.yaml uses. This block was hand-written and drifted twice: it
needed the measured `timeoutSeconds: 5` fix copied across by hand, and it never got a
startupProbe at all. `dict` = every default, which is what this service wants.

## `{{- include "lance.securityContext" . | nindent 10 }}`

Same hardening + scratch pair as the fleet, and for the same reason: this file called
neither, so the container rendered with no securityContext while values.yaml claimed
every app container got one. See fleet.yaml for the full note.
