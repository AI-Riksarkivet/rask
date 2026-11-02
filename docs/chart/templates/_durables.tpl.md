# chart/templates/_durables.tpl

Rationale for `chart/templates/_durables.tpl`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- define "lance.chartDurables" -}}`

EVERY JETSTREAM DURABLE THIS CHART CREATES, as one space-separated list.

[[LH-127]]. `nats-stream-job.yaml`'s orphan pass deletes a `*-durable` that nothing here created, and
the only safe key for "nothing here created it" is the set of names this chart RENDERS. Deriving it
from app-ids instead is destructive: the dead-letter consumer, `lineage-dlq-durable`, is not named
`<app-id>-durable`, so stripping the suffix yields a name in no app-id set and the pass removes it.
That is the 2026-07-13 dead-subscription failure, produced by the loop built to prevent it.

ONE DERIVATION, TWO CONSUMERS. `dapr-component.yaml` spells durable names three different ways and the
Job needs the same answer; a second inline derivation is how this loop already shipped one defect —
CATALOG_CONTROL was excluded on the written ground that its consumers are ephemeral while two
chart-owned durables sat on it. `test_a_durable_the_chart_owns_is_a_durable_the_drift_loop_walks.py`
asserts this template and the rendered Components agree, so a new durable that forgets this list fails
a test rather than being deleted at the next release.

The CONDITIONALS must match the components' own, which is why each branch below names the same guard
its component carries rather than a convenient approximation.

## `{{- $names := list (printf "%s-durable" .Values.services.lineage.daprAppId) -}}`

Lineage's own subscriber, unconditional like its component ([[LH-303]]): durable so a NATS restart
cannot unsubscribe it. Left out of this set, the orphan pass would delete it on every release and
the re-created durable would replay the whole stream.

## `{{- if and .Values.maintenance.enabled .Values.maintenance.arrivalEnabled -}}`

Maintenance's write-event durable, on the conditional [[LH-127]] asks for: it leaves this set the
moment `arrivalEnabled` turns off, so the orphan pass removes the residue instead of leaving it to
replay stale events on the day somebody re-enables the lane.
