{{/*
EVERY JETSTREAM DURABLE THIS CHART CREATES, as one space-separated list.

[[LH-127]]. `nats-stream-job.yaml`'s orphan pass deletes a `*-durable` that nothing here created, and
the only safe key for "nothing here created it" is the set of names this chart RENDERS. Deriving it
from app-ids instead — the form the row originally asked for — is destructive: measured 2026-09-16,
three chart-owned durables are not named `<app-id>-durable` (`lineage-dlq-durable`,
`medallion-producer-control-durable`, `notifications-control-durable`), so stripping the suffix yields
names in no app-id set and the pass removes the dead-letter consumer and both control lanes. That is
the 2026-07-13 dead-subscription failure, produced by the loop built to prevent it.

ONE DERIVATION, TWO CONSUMERS. `dapr-component.yaml` spells durable names five different ways and the
Job needs the same answer; a second inline derivation is how this loop already shipped one defect —
CATALOG_CONTROL was excluded on the written ground that its consumers are ephemeral while two
chart-owned durables sat on it. `test_a_durable_the_chart_owns_is_a_durable_the_drift_loop_walks.py`
asserts this template and the rendered Components agree, so a new durable that forgets this list fails
a test rather than being deleted at the next release.

The CONDITIONALS must match the components' own, which is why each branch below names the same guard
its component carries rather than a convenient approximation.
*/}}
{{- define "lance.chartDurables" -}}
{{- $names := list -}}
{{- if .Values.medallion.enabled -}}
  {{- /* One `<appId>-durable` per subscriber that asks for `deliverPolicy: new` — the lineage service
      subscribes with `all` and deliberately carries NO durable, so a replay can rebuild the graph. */ -}}
  {{- $names = append $names (printf "%s-durable" .Values.medallion.producer.daprAppId) -}}
  {{- range .Values.medallion.stageRunners -}}
    {{- $names = append $names (printf "%s-durable" .daprAppId) -}}
  {{- end -}}
  {{- $names = append $names (printf "%s-control-durable" .Values.medallion.producer.daprAppId) -}}
{{- end -}}
{{- /* The literal the component uses, not a values lookup: `dapr-component.yaml:191` appends
    `(dict "appId" "notifications")` unconditionally, while its CONTROL-lane sibling at :139 carries a
    guard. Two different conditions on one service, so they are listed separately. */ -}}
{{- $names = append $names "notifications-durable" -}}
{{- if and .Values.dapr.enabled .Values.services.notifications -}}
  {{- $names = append $names "notifications-control-durable" -}}
{{- end -}}
{{- /* Maintenance's write-event subscription exists only with a work queue to put the result in — and
    that is exactly the conditional [[LH-127]] asks for on `maintenance-durable`: it leaves this set the
    moment `workTopic` empties, so the orphan pass removes the residue instead of leaving it to replay
    stale events into `plan_one` on the day somebody re-enables the lane. */ -}}
{{- if and .Values.maintenance.enabled .Values.maintenance.workTopic -}}
  {{- $names = append $names (printf "%s-durable" .Values.maintenance.daprAppId) -}}
{{- end -}}
{{- if .Values.dapr.resiliency.enabled -}}
  {{- $names = append $names (printf "%s-dlq-durable" .Values.services.lineage.daprAppId) -}}
{{- end -}}
{{- if and .Values.dapr.enabled .Values.maintenance.enabled .Values.maintenance.workTopic -}}
  {{- $names = append $names (printf "%s-work-durable" .Values.maintenance.daprAppId) -}}
{{- end -}}
{{- /* The index lane's durable, on the same conditional shape as the work queue's and for the same
    reason [[LH-127]] gives: it renders only while `indexTopic` is set, so emptying that value takes it
    out of this set and the orphan pass reclaims the consumer rather than leaving one bound to a
    subject nobody publishes. Listed separately from the work durable because the two components carry
    different ackWaits and either lane can be enabled without the other. */ -}}
{{- if and .Values.dapr.enabled .Values.maintenance.enabled .Values.maintenance.indexTopic -}}
  {{- $names = append $names (printf "%s-index-durable" .Values.maintenance.daprAppId) -}}
{{- end -}}
{{- join " " (uniq $names) -}}
{{- end -}}
