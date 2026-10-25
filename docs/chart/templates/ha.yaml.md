# chart/templates/ha.yaml

Rationale for `chart/templates/ha.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if .Values.podDisruptionBudget.enabled }}`

HA for the stateless request-serving services — the lakehouse pair AND the fleet, derived from
`.Values.services` rather than named. Off by default
(single-node kind); the prod overlay enables the PDB after bumping replicas. The stateful stores
(AGE, the object store) get HA from their operators or the platform, NOT from here. The
event-driven subscribers are intentionally excluded — eviction is safe there (Dapr redelivers, and
every hop converges on its idempotency key).

ONE RULE THROUGHOUT, and it is why every block below is gated: a `minAvailable: 1` PDB over a SINGLE
replica blocks every voluntary drain, so a node cordon hangs instead of proceeding. Scale first, bound
second. That gate used to apply only to the P5 micro-frontend zones (the cluster-routed north-south
edge, where an unbounded drain defeats the replica bump); the backend list was a literal and ungated,
so enabling PDBs without scaling wrote exactly the budget this warning describes.

## `{{- range $name, $svc := $.Values.services }}`

EVERY service in `.Values.services`, gated on replicas > 1 — the lakehouse pair (catalog,
lineage) and the fleet (notifications) alike, never a literal list. An ungated literal
list, with PDBs enabled and a service left at one replica, writes a `minAvailable: 1` budget over a single pod, which blocks EVERY voluntary drain
— a node cordon then hangs instead of proceeding. That is this file's own header warning, applied
to the backends rather than only to the zones. Scale first, bound second, never the inverse.

## `behavior:`

WITHOUT THIS THE HPA CHURNS. Kubernetes stabilises scale-UP by default and scale-DOWN not at
all, so a burst that clears the utilization target takes the replica count to maxReplicas and
the pass of that burst takes it straight back — repeatedly, on an I/O-bound service whose CPU
is spiky by nature. kubernetes.md's own worked example carries this exact field with a
five-minute cooldown, and the template had no `behavior` block at all.
