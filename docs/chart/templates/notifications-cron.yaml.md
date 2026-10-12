# chart/templates/notifications-cron.yaml

Rationale for `chart/templates/notifications-cron.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.dapr.enabled .Values.services.notifications }}`

The notification plane's SECOND ingress (open_notifications.md §2/S2). The bus subscription
alone is provably incomplete: Ray TRAIN and every external OpenLineage producer emit lineage
over HTTP only — those lanes reach lineage's
durable feed and never `lineage.events.v1`. This cron ticks the notifications service's
sidecar-only reconcile route (POST /<bindingName>, guarded by require_dapr_token), which walks
`GET /events` down from a persisted cursor and closes the HTTP-only lanes at <= one tick.

It doubles as the catch-up path after a notifications outage, which is why the schedule is
component config here rather than a scheduler thread in the service: changing the cadence is a
values edit, not a redeploy of application code.

Mirrors the maintenance and compute crons exactly: schedule in the Component, scoped to the one
consuming app-id, gated on dapr.enabled (no control plane, no Component CRD, and the release
would fail to apply). The service still renders without it — its reconcile route simply never
fires, and the bus lane keeps working.
