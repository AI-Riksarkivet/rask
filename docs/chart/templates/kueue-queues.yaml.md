# chart/templates/kueue-queues.yaml

Rationale for `chart/templates/kueue-queues.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $weightPre := "-5" }}`

Default Kueue queue topology, applied by a post-install/post-upgrade hook.

Why a hook + retry instead of plain templated CRs: Kueue's ResourceFlavor and
ClusterQueue admission webhooks have failurePolicy=Fail, so creating those CRs
before the kueue-controller-manager's webhook endpoints are Ready fails the apply
(and would fail `helm install`). On a cold install the controller (a subchart) and
these CRs land in the same Helm pass, so we cannot rely on ordering. The hook Job
runs after the main resources and retries `kubectl apply` until the webhook accepts
it — making a clean `helm install` reproduce the queues with no manual step.

Idempotent (apply), self-cleaning (hook-succeeded), least-privilege RBAC scoped to
the three Kueue resource kinds.

## `- apiGroups: ["apps"]`

[[XC-065]] `rollout restart` PATCHES the Deployment's pod template and `rollout status` reads
it plus the ReplicaSets it owns. Granted here, beside the container that uses them, because a
control whose permission lives in another file is one nobody notices was never granted.

## `nominalQuota: {{ if include "rask.gpuEnabled" . }}{{ index .Values.kueue.queues.nominalQuota "nvidia.com/gp...`

Derived from the ONE GPU signal: the values comment demands this equal the
cluster's total schedulable nvidia.com/gpu ("never set it above the real GPU
count — Kueue would admit a pod that can't schedule"), and on a GPU-less estate
that total is 0. Hardcoding 1 there admits a GPU RayService that then sits
Pending forever, which reads as a Kueue bug rather than a missing GPU.

## `initContainers:`

THE CA MUST BE RE-PATCHED BEFORE THE APPLY, and retrying cannot do it ([[XC-065]]).
kueue's controller writes its self-signed CA into the CRDs' conversion `caBundle` when it
BOOTS; this upgrade re-applies those CRDs from the subchart and overwrites the patch, so the
API server then holds a CA that does not match the certificate the webhook serves. Measured
live 2026-09-21: the apply below crash-looped nine times over 21 minutes on `x509:
certificate signed by unknown authority ... "kueue-ca"` and the release was recorded failed.
A restart re-runs the patch — observed to recover in 68 seconds — and doing it as an INIT
container is what orders it, so the queues apply against a webhook the API server trusts
rather than racing it.

## `args:`

A restart nobody waits on is a race the apply loses: it would meet the OLD webhook.
The timeout is generous because the wait is cheap and a premature give-up reintroduces
exactly the failure above; the observed recovery was 68s.
