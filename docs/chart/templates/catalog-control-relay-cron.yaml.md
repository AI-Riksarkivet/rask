# chart/templates/catalog-control-relay-cron.yaml

Rationale for `chart/templates/catalog-control-relay-cron.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.dapr.enabled .Values.catalog.controlEmit .Values.catalog.controlOu...`

THE CONTROL LANE'S RELAY (the other half of the #4 outbox, for `catalog.control.v1`).
The catalog STAGES every control event to `LANCE_CONTROL_OUTBOX_URI` before publishing and drops
it only on ack, so a NATS blip leaves the event durable. Until this Component existed nothing read
that prefix: the estate's only outbox drain is lineage's reconcile cron, which parses each staged
object as an OpenLineage RunEvent and DELETES what it cannot — so a control event handed to it is
destroyed as poison rather than relayed. (The catalog now refuses to boot with the two prefixes
equal, for exactly that reason.)

What that cost: a subscriber that acts on `table_published` learns of a publication only from this
lane, so one lost publish was a publication nobody downstream heard of, with the tag advanced, the
route 200 and every pod green.

Delivered to `POST /<component name>` at the catalog's pod ROOT (never under a prefix), guarded by
require_dapr_token so only the sidecar's cron may drive the replay. The Component's name, the app's
LANCE_CONTROL_RELAY_BINDING_NAME and the served path are ONE string.

Mirrors the notifications and maintenance crons: schedule as Component config so the
cadence is a values edit rather than an app redeploy, scoped to the one consuming app-id, gated on
dapr.enabled (no control plane, no Component CRD, and the release would fail to apply).
