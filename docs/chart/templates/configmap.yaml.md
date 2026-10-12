# chart/templates/configmap.yaml

Rationale for `chart/templates/configmap.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `RASK_DOCS: {{ .Values.docs.enabled | quote }}`

Interactive API docs, off unless a deployment asks (see values.yaml `docs`). The gateway reads
this one: its aggregated Swagger is served under the Ingress-published /api prefix.

## `RASK_FLOWS_URL: {{ printf "http://%s-flows:%v" (include "rask.fullname" .) .Values.services.flows.port | qu...`

The studio flow-builder's server half. Without this the gateway's /api/flows row falls back to its
code default (127.0.0.1:8840 — the gateway pod itself) and every call 502s, which the builder shows
as "engine offline": graceful, but it means no server-side or durable runs at all.

## `RASK_NOTIFICATIONS_URL: {{ printf "http://%s-notifications:%v" (include "rask.fullname" .) .Values.services...`

The notification plane. Same failure as the two rows above if omitted — the gateway's
/api/notifications row falls back to its code default (127.0.0.1:8850, the gateway pod itself) and
every inbox call answers a 404 indistinguishable from an unrouted path, while the service is up and
healthy one Service name away.

## `RASK_NOTIFICATIONS_PUBSUB: {{ include "lance.subPubsub" (list . "notifications") | quote }}`

The per-subscriber pubsub COMPONENT this app subscribes `lineage.events.v1` on, derived from the
same `lance.subPubsub` helper that names the Component itself (dapr-component.yaml), so the two cannot
disagree. The service's own default happens to
equal this render at `pubsub.name: lineage-pubsub`, which is exactly what makes the drift silent:
override `pubsub.name` and the app would subscribe on a component that does not exist, which daprd
reports as a startup error and the pod survives — a Ready pod with a dead subscription.

## `RASK_NOTIFICATIONS_LINEAGE_TOPIC: {{ .Values.pubsub.topic | quote }}`

The topic, from the same `pubsub.topic` value every other consumer of this stream reads
(services.yaml LINEAGE_DAPR_TOPIC, maintenance.yaml) —
a subscriber that names the topic itself is a subscriber that can be retargeted halfway.

## `RASK_NOTIFICATIONS_CONTROL_PUBSUB: {{ printf "%s-notifications" (.Values.pubsub.controlName | default "cata...`

v3 targeting's ingress. Rendered from the SAME expression that names the Component
(dapr-component.yaml) for the reason the row above carries: the app's default happens to equal
this render today, which is exactly what makes the drift silent — override `pubsub.controlName`
and the app would subscribe on a component that does not exist, which daprd reports as a startup
error while the pod stays Ready. A Ready pod with a dead subscription.

## `RASK_NOTIFICATIONS_DLQ_TOPIC: "dlq.notifications"`

Dapr-native DLQ, rendered under the SAME condition as the Resiliency CRD and never on its own:
a `deadLetterTopic` with no retry policy behind it parks on the FIRST transient failure, which is
strictly worse than no dead-lettering at all (measured on the medallion publication head). The app
ships this setting EMPTY for exactly that reason, so this line is where the two facts meet.
Its own subject rather than a shared one — a per-subTopic DLQ once had two apps parking on each
other's dead letters and counting them — and `dlq.>` is already bound by the
DLQ stream, so no new NATS stream is needed. The parking subscription rides this app's OWN pubsub
component (the medallion shape, not lineage's `-dlq` clone): that component is already
deliverPolicy=new + durable, which is the property lineage needed a second component to obtain,
and `notifications-durable` is free on the DLQ stream because NATS scopes consumer names per stream.

## `RASK_NOTIFICATIONS_LINEAGE_URL: {{ printf "http://%s-lineage:%v" (include "lance.fullname" .) .Values.servi...`

The reconciler's origin — lineage's OWN Service, never the gateway. The feed poll authenticates at
lineage's SERVICE door, and the gateway is a public front door whose invocations that door refuses
by construction (`dapr_auth.is_public_caller`), so pointing this at the gateway would produce a 403
that reads like a missing grant. Omit it and the app falls back to 127.0.0.1:8000 — its own pod —
where the walk finds nothing and the reconciler runs cleanly forever while reconciling nothing.

## `RASK_NOTIFICATIONS_BINDING_NAME: {{ .Values.services.notifications.reconcileBindingName | default "notifica...`

The route the cron Component is delivered to. Rendered from the SAME values key that names the
Component (`notifications-cron.yaml`), because a binding whose component name and route path
disagree is a cron that fires into a 404 on every tick while both halves look right in isolation.

## `RASK_CATALOG_API_URL: {{ printf "http://%s-catalog:%v" (include "lance.fullname" .) .Values.services.catalo...`

lance-plane gateway upstreams (the P1 gateway fold, docs/adr/0117-nginx-is-retired-and-the-fastapi-gateway-is-the-in-cluster.md): the fleet gateway carries
/api/catalog, /api/lineage
and the /api/explorer/* namespace. Service names
use lance.fullname — that is what services.yaml/explorer.yaml name their Services.

## `RASK_LINEAGE_SIDECAR_ONLY_ROUTES: {{ include "lance.lineageSidecarOnlyRoutes" . | quote }}`

The sidecar-only lineage routes the gateway must 403 at the edge (the retired nginx blocklist,
now Python middleware). ONE source: the lance.lineageSidecarOnlyRoutes helper.
