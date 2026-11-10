# chart/templates/dapr-component.yaml

Rationale for `chart/templates/dapr-component.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: dapr.io/v1alpha1`

Dapr pub/sub components — the catalog→lineage topic and the control broadcast on NATS JetStream. The
sidecar owns retry/backoff here (config, not app code); subscriptions are programmatic (each app
serves GET /dapr/subscribe via dapr-ext-fastapi), so no declarative Subscription CRDs.

PUBLISH-only component: the catalog publishes through it. Consumer
metadata (queue group / deliver policy) lives on the per-SUBSCRIBER components below, never here.
Every pub/sub component is one app's NATS connection, with that app's credential (values.yaml `nats.auth`).

## `{{- if and .Values.maintenance.enabled .Values.maintenance.lineageEmit (not .Values.maintenance.arrivalEnabled) }}`

Maintenance publishes its lineage through its own component; one is rendered for it here when the write-event lane does not give it one.

## `apiVersion: dapr.io/v1alpha1`

BROADCAST component for the catalog's OWN control-plane change-events (grants/warehouses/policies/tables).
DEDICATED (not the shared lineage-pubsub) and with NO queueGroupName → jetstream sets no DeliverGroup, so
EVERY catalog replica receives EVERY event and appends it to its per-replica ring buffer (GET /management/v1/events).
The catalog is BOTH publisher (core/control_emit.py) and subscriber (api/dapr.py) on this one component.
deliverPolicy=new: a booting/redeployed replica must NOT replay 168h of past governance events into a fresh in-memory
buffer — the buffer is a live-refresh hint window, not a log.

## `- { name: durableName, value: "catalog-control-{podName}" }`

Each replica's consumer is a durable it names for its pod ([[LH-305]]): daprd fills `{podName}` from `POD_NAME`, which
the injector sets from `metadata.name` (Dapr 1.18.1 `pkg/runtime/meta/meta.go:88-93`,
`pkg/injector/patcher/sidecar_container.go:264`). An ephemeral consumer is deleted after 5 s without interest (nats-server
v2.14.2 `consumer.go:1462-1467`) and Dapr's jetstream component binds a consumer once and never re-creates it
(components-contrib v1.18.0 `jetstream.go:268-279`), so a connection that dropped and reconnected left the replica deaf
until its pod restarted; measured on the nats-auth rig, a 10 s cut silenced the ephemeral and the durable delivered again.
The name carries no `-durable` suffix, so the stream Job's orphan and drift passes never touch it.

## `- { name: streamName, value: "CATALOG_CONTROL_BROADCAST" }`

The durables live on a mirror of CATALOG_CONTROL whose consumer limit reaps a deleted pod's durable after
`catalog.controlConsumerInactiveSeconds`. Dapr gives a consumer no inactivity threshold, and a stream's limit applies to
every consumer on it (nats-server v2.14.2 `consumer.go:663-667`), so on CATALOG_CONTROL it would reap every other
consumer of the control topic as well. Naming the stream also makes Dapr skip its subject lookup, which would answer CATALOG_CONTROL (a mirror
captures no subject). The catalog still publishes through this component onto CATALOG_CONTROL, since a publish names
only its subject.

## `{{- include "lance.brokerWindows" . | nindent 4 }}`

The broadcast's broker windows, the ones every retried subscriber carries ([[LH-559]]). The subscription parks a
delivery its retries cannot get taken on `catalog.controlDlqTopic` (`LANCE_CONTROL_DLQ_TOPIC`), and the Resiliency CR retries
this component, so while the sidecar spends its 480 s schedule the broker must hold the delivery: unset, components-contrib
v1.19.0 sets no `AckWait` or `MaxDeliver` on the consumer (`pubsub/jetstream/jetstream.go:174-181`), nats-server's 30 s
ack window redelivers underneath the retry, and its unlimited `MaxDeliver` redelivers a poison event without end. The park
is published through this component like the catalog's own events; a publish names only its subject, so it lands on the
DLQ stream (`dlq.>`), and `CatalogControlLaneDeadLettering` pages on the sidecar's count of it.

## `{{- if and .Values.maintenance.enabled .Values.maintenance.controlEmit }}`

The control topic's other publisher, on its own component and so as its own NATS user: maintenance's #79 purge
announcements. A publisher a component does not scope is refused by its sidecar, silently.

## `{{- $subscribers := list (dict "appId" .Values.services.lineage.daprAppId "deliverPolicy" "all") }}`

One component PER SUBSCRIBER app-id (see the lance.subPubsub helper): queueGroupName makes that
app's replicas a competing-consumer group (single delivery per app — without it every replica gets
EVERY message: jetstream only sets a DeliverGroup when queueGroupName is non-empty), and it CANNOT
live on one shared component because lineage and maintenance (under `maintenance.arrivalEnabled`) both consume
lineage.events.v1 — one shared queue group would split those messages across the two apps. deliverPolicy differs on
purpose: `all` for lineage (a NEW durable starts at the stream's first message, so its first attach,
or a rebuild after `nats consumer rm LINEAGE lineage-durable`, replays the retained stream into the
idempotent ingest), `new` for the other subscribers (a replayed event acts twice).

EVERY subscriber is DURABLE + queue group ([[LH-303]]). An ephemeral consumer dies with its last
member and with a NATS restart, and the sidecar does not re-create it: measured 2026-09-26 on Dapr
1.18.1, NATS scaled to 0 and back left lineage deaf to the bus until its pod restarted, while the
durables resumed. Durable + queue group keeps its cursor across pod death, redeploys and NATS
restarts, with none of the "consumer name already in use" orphans of a durable WITHOUT a queue group
(chaos-verified 2026-07-06). Pinned by tests/unit/test_every_bus_subscriber_survives_a_bus_restart.py.

A REFUSAL REPEATS ON A REPLAY. The two unrepairable classes (bytes that do not parse, a run carrying
no author) are ACKED and counted rather than parked, because nothing can change their answer and a
park would write a duplicate per replay. A named PERSON who lacks a grant still parks: a tuple can be
written and the same event then succeeds.

## `{{- if eq .appId $.Values.services.lineage.daprAppId }}`

LINEAGE'S SUBSCRIPTION DELIVERS IN PARALLEL ([[LH-533]]). Unset, the JetStream component hands its app
one message at a time (components-contrib v1.19.0, `pubsub/jetstream/metadata.go:63-65`): measured on the
live estate 2026-10-23, 61 of 61 deliveries ran back to back, so a backlog was recorded one commit per
event, and the one delivery a catalog kill failed held every later message through its 120 s retry. With
`parallel` each message is handled on its own, the events a commit is in flight for land together in the
next (`ProvenanceStore.record`), and a retrying event holds no other back. The bound is the durable's own
`maxAckPending` (`services.lineage.deliveriesInFlight`), so JetStream hands out no more than that and
nothing waits in the sidecar while its `ackWait` runs. A sidecar cannot rebind a durable whose config
changed, so nats-stream-job.yaml recreates `lineage-durable` when its `max_ack_pending` differs, and its
`deliverPolicy: all` replays the retained stream into the keyed ingest. `concurrencyMode` is the
component's alone and changes no durable. The other subscribers keep one at a time.

## `{{- if and .Values.maintenance.enabled .Values.maintenance.arrivalEnabled }}`

MAINTENANCE's write-event subscription: a write holds that table's pass on maintenance's work queue
(ADR 0198), which refreshes its due record (the tables needing compaction or reindexing, ADR 0162). It
rewrites nothing; the rewrite is the user's job. Its dead letters go to `maintenance.arrivalDlqTopic`.

`new`, because a replayed event acts twice: replaying the retained stream on a pod restart would
queue a pass for every table written in the window, hundreds of manifest opens to reach answers each
table's own scheduled pass reaches anyway. A missed window costs at most that table's next pass, within
`maintenance.queue.recheckHours`; a replayed one costs the walk this lane exists to avoid.

## `{{- range $subscribers }}`

NO `protectedTopics` / `publishingScopes` / `subscriptionScopes` HERE, and the reason is a
measurement rather than an omission ([[LH-064]]). Deny-by-default on `lineage.events.v1` was
built, applied and rolled on 2026-09-16 and BROKE DELIVERY: `subscriptionScopes` is NOT additive.
Naming an app in it makes that list the app's COMPLETE allowlist for the component — including
topics that are not protected at all. The live refusal, on a topic `protectedTopics` never named:
"Retry failed for subscription pubsub lineage-pubsub-notifications, topic dlq.notifications:
subscription to topic 'dlq.notifications' ... is not allowed". 47 denials on medallion-producer,
16 on notifications, 4 on lineage before the revert.
So a future attempt must enumerate EVERY topic each app uses on its component, in BOTH
directions, measured off `/v1.0/metadata` and the publish call sites — not just the one topic
being protected. The scoping the estate already has is the document-level `scopes:` below, which
closes each component to one app-id.

## `- { name: durableName, value: "{{ .appId }}-durable" }`

One durable per subscriber app-id, for the reasons in the block above.

## `{{- include "lance.brokerWindows" $ | nindent 4 }}`

The subscriber's broker windows, from the one helper every retried component uses (`_helpers.tpl.md`).

## `apiVersion: dapr.io/v1alpha1`

The lineage DLQ PARKING consumer — its own component, never the app's main one. The main
lineage component starts at the stream's first message (`all`), which a parking subscription must
not: riding it, a replay re-parks up to 168h of already-parked dead letters, spiking ERROR logs and
DEAD_LETTERED counts with no new loss. deliverPolicy=new plus its own durable parks each dead letter
exactly once. Consumer config matches the
resiliency-mode values the nats-stream-job's durable-drift reconcile expects (EXP_MAXD/EXP_BOFF) —
this component only renders when resiliency is on, which is also the only time LINEAGE_DLQ_TOPIC
exists.

Dapr secret store fronting the dev OpenBao or the platform's Vault or OpenBao (Vault-API compatible). Apps
read secrets via their sidecar (GET /v1.0/secrets/lance-secrets/<name>) instead of plaintext env — the
secret-handling fix. The chart renders no store ([[XC-149]]): it names the dev fixture's (`openbao.enabled`)
or the platform's (`openbao.externalAddr`), and the apps read either one through this component.

## `- { name: vaultAuthMethod, value: kubernetes }`

EACH SIDECAR LOGS IN AS ITS OWN APP ([[XC-165]], ADR 0169). Dapr's Vault store logs in with the store's Kubernetes auth
method from 1.19 on (components-contrib#4448), presenting the pod's own ServiceAccount token and renewing its session in
the background, logging in again with the kubelet's current token when the session's max TTL ends. So no store token
exists anywhere: none rides this component, a Kubernetes Secret or a file, and none needs minting or rotating. The
static token Dapr 1.18 offered was read once at init and never renewed (v1.18.0 `vault.go:400-421`), so it could not be
both scoped to one app and short-lived.

## `- { name: vaultKubernetesRole, value: {{ include "lance.storeRole" (list . "{appID}") | quote }} }`

ONE COMPONENT, ONE ROLE PER APP. Dapr fills `{appID}` into any component's metadata when the sidecar starts
(`pkg/runtime/meta/meta.go:96-97`, 1.18.1 and 1.19.0-rc.4; documented as templated metadata values), so every app's
sidecar logs in to `<release>-<its own app id>`, a role bound to that app's ServiceAccount whose policy reads exactly its
app's secrets (`lance.storeReads`). The store refuses the rest whatever the Configuration says, and a pod that claimed
another app's id would still present its own account's token, which that app's role does not admit. Every other
component keeps naming this one store (`auth.secretStore: lance-secrets`) and every service its one store name.

## `- { name: vaultServiceAccountTokenPath, value: /var/run/secrets/rask/openbao/token }`

The token daprd logs in with, which the pod projects for `openbao.identity.audience` and mounts into daprd alone
(`lance.storeIdentity`): the app container cannot log in past its own sidecar's Configuration. The file is re-read on
every login, so the kubelet's rotation reaches it.

## `- { name: vaultKVUsePrefix, value: "false" }`

Dapr defaults vaultKVPrefix to "dapr", so it reads secret/data/dapr/<key>; our seed writes
secret/data/<key>. Drop the prefix so the path is the natural secret/data/<key> (the kv-v2 nuance).

## `- { name: skipVerify, value: "false" }`

TLS is verified wherever there is TLS: the platform's store is https (`lance.platformStoreAddr` refuses anything
else); the dev store is plain http, where the setting has nothing to verify, and 1.19 logs every `true` as unsafe.

## `{{- range (splitList "," (include "lance.secretScopes" .) | compact) }}`

ONE derivation, in `lance.secretScopes`, because this list has other readers:
`lance.appTokenEnv` decides whether a workload takes its app-API token from this store or
through the environment, and it decides by membership HERE. Restating the computation would
let the two agree at review and diverge later — a pod pointed at a store its app-id cannot
reach gets ERR_SECRET_STORES_NOT_CONFIGURED and fails closed at boot.

## `{{- if .Values.dapr.enabled }}`

The catalog commit reconcile cron binding (from its own rationale below).

### `{{- if .Values.dapr.enabled }}`

LH-497: the commit is the outbox. A write is announced after its commit, and a crash between the commit and the staging of its event loses the announcement; this cron has the catalog read each table's version log after its mark and announce every committed version lineage's feed lacks, under the run id derived from the commit and signed for the commit's author. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string. Every catalog replica ticks, and an announcement two replicas make at once lands one feed row, since both carry the commit's run id.

## `{{- if and .Values.dapr.enabled .Values.catalog.controlEmit .Values.catalog.controlOutbox.enabled }}`

The catalog control relay cron binding (from its own rationale below).

### `{{- if and .Values.dapr.enabled .Values.catalog.controlEmit .Values.catalog.controlOu...`

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

Mirrors the maintenance crons: schedule as Component config so the
cadence is a values edit rather than an app redeploy, scoped to the one consuming app-id, gated on
dapr.enabled (no control plane, no Component CRD, and the release would fail to apply).

## `{{- if .Values.dapr.enabled }}`

The catalog erasure release cron binding (from its own rationale below).

### `{{- if .Values.dapr.enabled }}`

ADR 0138: releases an erasure's pinning tags and branches when due. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string.

## `{{- if .Values.dapr.enabled }}`

The catalog manifest reclaim cron binding (from its own rationale below).

### `{{- if .Values.dapr.enabled }}`

ADR 0185 (LH-251): removes each root's `__manifest` versions older than `catalog.manifestReclaim.retainSeconds`, which no `dir` commit reclaims. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string. Every catalog replica ticks, and two reclaims of one root remove the same versions.

## `{{- if .Values.dapr.enabled }}`

The catalog promotion expiry cron binding (from its own rationale below).

### `{{- if .Values.dapr.enabled }}`

ADR 0156 C1 (LH-466): expires a promotion hold nobody answered before its project's `review_hours` passed, leaving `published` where it was. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string. Every catalog replica ticks, and each hold expires once by an ETag-guarded write.

## `{{- if and .Values.dapr.enabled .Values.services.lineage.relay.enabled }}`

The lineage relay cron binding (from its own rationale below).

### `apiVersion: dapr.io/v1alpha1`

Dapr cron INPUT binding — the sidecar POSTs to /<bindingName> on `schedule` to run lineage's relay tick:
drain the lineage outbox, prune the feed, forward the feed to the configured graph backend. Config lives
here, not in app code (no scheduler thread). Scoped to the lineage app; the route mounts only when
LINEAGE_RELAY_BINDING_NAME matches this name. Gated on dapr.enabled too: without the Dapr control
plane the Component CRD doesn't exist and the release would fail to apply.
