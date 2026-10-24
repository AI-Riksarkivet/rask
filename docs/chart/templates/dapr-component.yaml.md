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
Ephemeral (no durableName) + deliverPolicy=new: a booting/redeployed replica must NOT replay 168h of past
governance events into a fresh in-memory buffer — the buffer is a live-refresh hint window, not a log.

## `{{- if and .Values.maintenance.enabled .Values.maintenance.controlEmit }}`

The control topic's other publisher, on its own component and so as its own NATS user: maintenance's #79 purge
announcements. A publisher a component does not scope is refused by its sidecar, silently.

## `{{- if .Values.dapr.resiliency.enabled }}`

Redelivery timing, on the SAME schedule the per-subscriber components below carry and for the same
invariant: the broker's first backOff step must exceed the sidecar's total retry span, or the broker
redelivers while the sidecar is still mid-retry and one publication is handled twice, concurrently.
This component shipped with none of these and rode jetstream's defaults — so the subscriber whose
message is the most expensive in the estate to duplicate was the only one without the guard.

Duplicated rather than shared because the block below renders inside a `range` over $subscribers,
which this component is deliberately not part of (it carries the CONTROL topic, not the lineage
one). .dagger/charts.go asserts the rendered numbers, so drift between the two copies fails the
build rather than going unnoticed.

Changing these on an EXISTING cluster mutates a live durable's config — the §2.11 hazard, in its
benign form: the durable NAME and its filter subject are unchanged, so the sidecar reconfigures
rather than colliding with a consumer already holding the name, and this is exactly the drift the
nats-stream-job reconcile compares (maxDeliver/backOff) and repairs.

## `apiVersion: dapr.io/v1alpha1`

The NOTIFICATION plane's control-topic subscriber (v3 targeting: a governance act that NAMED
someone). Its OWN component rather than a scope added to the
broadcast component — `queueGroupName` lives on the COMPONENT, so a scope added to a broadcast
splits it into a competing-consumer group instead of joining it, and the catalog's broadcast exists
precisely to reach every replica.

`deliverPolicy: new` for the same reason the lineage subscriber carries it: an inbox that replayed
the retained backlog on every pod restart would re-notify a week of grants on every rollout.

## `{{- if .Values.dapr.resiliency.enabled }}`

Same redelivery schedule as its two siblings, and the same invariant: the broker's first backOff
step must exceed the sidecar's total retry span, or the broker redelivers while the sidecar is
still mid-retry and one event is handled twice, concurrently.

## `{{- $subscribers := list (dict "appId" .Values.services.lineage.daprAppId "deliverPolicy" "all") }}`

One component PER SUBSCRIBER app-id (see the lance.subPubsub helper): queueGroupName makes that
app's replicas a competing-consumer group (single delivery per app — without it every replica gets
EVERY message: jetstream only sets a DeliverGroup when queueGroupName is non-empty), and it CANNOT
live on one shared component because lineage and notifications both consume lineage.events.v1 — one
shared queue group would split those messages across the two apps. deliverPolicy differs on
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

## `{{- $subscribers = append $subscribers (dict "appId" "notifications" "deliverPolicy" "new") }}`

The INBOX (open_notifications.md §2). `new` is the strongest case in this list: lineage's `all`
replays the retained stream on its first attach and on a rebuild, which for a graph IS the recovery
story and for an inbox would re-notify a week of history — a badge that lies loudest when it matters. The durable half is the other side of the same coin: a run
that terminated while this app was down must still reach its author, and `new` alone skips it on
reconnect. Unconditional, like the Deployment (services.notifications is frontDoor).

This mints a durable name no stream has seen, so §2.11's migration hazard does not apply here —
that failure is an EXISTING durable name re-requested with a different filter subject ("consumer
name already in use", a silently dead subscription the drift reconcile cannot see because
maxDeliver/backOff still match). `notifications-durable` on LINEAGE is new; the reconcile loop
(nats-stream-job.yaml) adopts it by its `-durable` suffix from the first tick.

## `{{- if and .Values.maintenance.enabled .Values.maintenance.arrivalEnabled }}`

MAINTENANCE's write-event subscription: a write refreshes that table's due record (the tables needing
compaction or reindexing, ADR 0162) between sweep ticks. It schedules no work; the rewrite is the
user's job. Its dead letters go to `maintenance.arrivalDlqTopic`.

`new`, for the notifications reason sharpened: replaying the retained stream on a pod restart would
refresh the due record of every table written in the window, hundreds of manifest opens to reach the
answer the next sweep tick writes anyway. A missed window costs at most one tick; a replayed one costs
the walk this lane exists to avoid.

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

## `- { name: protectedTopics, value: "{{ $.Values.pubsub.topic }}" }`

THE ONE CONSUMER-ONLY IDENTITY on the provenance bus, denied publish to it ([[LH-064]]).
Measured 2026-09-16: `notifications` has NO publish call sites of its own and its sidecar
registers exactly two topics on this component — `lineage.events.v1` and `dlq.notifications`.
Applied HERE ONLY, and that is the lesson from the first attempt: the same three keys rolled
to all eight components broke delivery, because `subscriptionScopes` is not additive — naming
an app in it makes that list its COMPLETE allowlist, so an unlisted-but-unprotected topic is
refused too (the `dlq.notifications` refusal quoted above).
So BOTH topics are listed on the subscribe side, and the publish side grants the dead-letter
(the sidecar parks on it via `deadLetterTopic`, and whether that publish is scope-checked is
not documented — granting it costs nothing and withholding it would be the same guess that
caused the outage) while withholding the provenance topic, which is the whole point.

## `- { name: durableName, value: "{{ .appId }}-durable" }`

One durable per subscriber app-id, for the reasons in the block above.

## `{{- if $.Values.dapr.resiliency.enabled }}`

backOff supersedes ackWait for redelivery timing (NATS consumer semantics): its FIRST value is the
effective ack window, so it must not undercut the slowest handler (one doing real S3 reads+writes,
like a maintenance work unit) — a 1s first step would concurrently redeliver mid-handling even at 1 replica.

## `- { name: ackWait, value: "720s" }`

Resiliency ON: the SIDECAR owns delivery retries (Resiliency CRD, 450s = 7.5 min total: 30+60+
120+240, jitter pinned off) and parks exhausted messages on the DLQ — so broker redelivery
becomes the CRASH-RECOVERY layer only. Its first step must exceed the sidecar's total retry
span, else the broker would redeliver while the sidecar still holds the message mid-retry
(concurrent duplicate handling). 720s > 450s holds with 270s of margin.
(This comment said "~10.5 min" until 2026-07-28 and dapr-resiliency.yaml said "~7.5 min" — two
different numbers for one schedule, neither of which the chart rendered. Both now state 7.5,
and .dagger/charts.go asserts the rendered value so they cannot drift apart again.)

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
