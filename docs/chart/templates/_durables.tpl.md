# chart/templates/_durables.tpl

Rationale for `chart/templates/_durables.tpl`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- define "lance.chartDurables" -}}`

EVERY JETSTREAM DURABLE THIS CHART CREATES, as one space-separated list.

[[LH-127]]. `nats-stream-job.yaml`'s orphan pass deletes a `*-durable` that nothing here created, and
the only safe key for "nothing here created it" is the set of names this chart RENDERS. Deriving it
from app-ids instead is destructive: several chart-owned durables are not named `<app-id>-durable`
(`lineage-dlq-durable`, `notifications-control-durable`, maintenance's `-work-durable` and
`-index-durable`; measured 2026-09-16 on the set then rendered), so stripping the suffix yields
names in no app-id set and the pass removes the dead-letter consumer and the control lane. That is
the 2026-07-13 dead-subscription failure, produced by the loop built to prevent it.

ONE DERIVATION, TWO CONSUMERS. `dapr-component.yaml` spells durable names five different ways and the
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

## `{{- $names = append $names "notifications-durable" -}}`

The literal the component uses, not a values lookup: `dapr-component.yaml:191` appends
`(dict "appId" "notifications")` unconditionally, while its CONTROL-lane sibling at :139 carries a
guard. Two different conditions on one service, so they are listed separately.

## `{{- if and .Values.maintenance.enabled .Values.maintenance.workTopic -}}`

Maintenance's write-event subscription exists only with a work queue to put the result in — and
that is exactly the conditional [[LH-127]] asks for on `maintenance-durable`: it leaves this set the
moment `workTopic` empties, so the orphan pass removes the residue instead of leaving it to replay
stale events into `plan_one` on the day somebody re-enables the lane.

## `{{- if and .Values.dapr.enabled .Values.maintenance.enabled .Values.maintenance.indexTopic -}}`

The index lane's durable, on the same conditional shape as the work queue's and for the same
reason [[LH-127]] gives: it renders only while `indexTopic` is set, so emptying that value takes it
out of this set and the orphan pass reclaims the consumer rather than leaving one bound to a
subject nobody publishes. Listed separately from the work durable because the two components carry
different ackWaits and either lane can be enabled without the other.
