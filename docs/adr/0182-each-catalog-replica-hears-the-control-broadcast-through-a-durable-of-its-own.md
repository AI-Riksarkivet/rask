# 0182. Each catalog replica hears the control broadcast through a durable of its own (2026-10-27)

**Status:** accepted; superseded in its mechanism 2026-11-14 (XC-199 step D4, below: each replica follows the stream
through a consumer its own connection holds, and the mirror and its durables are gone). Source: LH-305 (#323); design chosen by the owner on 2026-10-27 ("Per-pod durable"). Amends ADR 0027,
whose broadcast and ring buffer stand: only the consumer each replica holds changes.

## Context

ADR 0027 gave each catalog replica an ephemeral JetStream consumer on CATALOG_CONTROL (no queue group, `deliverPolicy:
new`). nats-server deletes an ephemeral push consumer once its deliver subject has had no interest for 5 s (v2.14.2
`consumer.go:1462-1467`), and Dapr's `pubsub.jetstream` binds a consumer once at subscribe and never re-creates it
(components-contrib v1.18.0 `jetstream.go:268-279`). A connection that drops and reconnects inside nats.go's reconnect
budget therefore leaves the replica subscribed to a consumer that no longer exists: the `GET /management/v1/events` buffer
and the cross-replica binding-cache eviction go quiet until the pod restarts. The bus watchdog (XC-095) restarts a sidecar
only when its connection closed for good, so it does not see this case. Measured on the nats-auth rig (a real
nats-server v2.14.2 and daprd 1.19.0-rc.4 with the chart's own Component): a 10 s cut of the broadcast sidecar's
connection, with the server up, left nothing delivered afterwards.

## Decision

- **The consumer is a durable named by the pod**, `durableName: catalog-control-{podName}`. daprd fills `{podName}` from
  `POD_NAME`, which the injector sets from `metadata.name` (Dapr 1.18.1 `pkg/runtime/meta/meta.go:88-93`,
  `pkg/injector/patcher/sidecar_container.go:264`). A durable keeps its place across a lapse in interest, so a
  reconnected sidecar delivers again and resumes at its acknowledgement floor.
- **The durables live on a mirror, CATALOG_CONTROL_BROADCAST**, which the stream Job declares with a consumer limit
  `inactive_threshold = catalog.controlConsumerInactiveSeconds` (3600 s) and the same message age. Dapr sets no
  inactivity threshold on a consumer, and a stream's limit applies to every consumer on it (`consumer.go:663-667`), so on
  CATALOG_CONTROL it would also reap notifications' durable. The limit is longer than nats.go's reconnect budget
  (60 tries, 2 s apart), past which the watchdog restarts the sidecar under the same pod name and it binds the same
  durable again. A deleted pod's durable is reaped an hour after its pod went.
- **The component names its stream** (`streamName`), so Dapr binds on the mirror and looks no stream up; the catalog
  still publishes through it onto CATALOG_CONTROL, since a publish names only its subject.
- **The catalog's bus user consumes only on the mirror**: `$JS.API.CONSUMER.INFO|CREATE` and `$JS.ACK` on
  CATALOG_CONTROL_BROADCAST for any consumer name, nothing on CATALOG_CONTROL, and no stream lookup, so it can no longer
  acknowledge notifications' control deliveries.
- **The Job converges the limit from git.** A mirror whose limit or message age differs is edited from the same config.
  nats-server refuses a change to a stream's consumer limits while any consumer exceeds them, `max_ack_pending`
  included (v2.14.2 `stream.go:2420-2446`), so the limits name `max_ack_pending: 1000`, the server's default for a
  consumer that sets none, and a lowered limit first lowers each durable above it. The Job's admin user may update this
  one stream (`$JS.API.STREAM.UPDATE.CATALOG_CONTROL_BROADCAST`) and no other. An edited limit reaches a durable the Job
  did not lower when its sidecar next subscribes, since a re-bind takes the stream's limit (measured).
- **A pod is rolled when its app's pub/sub Components change.** daprd loads Components once, at boot, so each Dapr pod
  carries a digest of the pub/sub Components scoped to its app (`checksum/dapr-pubsub`); without it this change would
  have reached the catalog only through a restart nothing in git performs.

## Consequences

- A restarted replica keeps its pod name only if its pod does: a new pod starts a new durable at the stream's head
  (`deliverPolicy: new`), as before, and the old one is reaped.
- The durable's name carries no `-durable` suffix, so the Job's orphan and drift passes, which key on that suffix and
  walk only LINEAGE, DLQ and CATALOG_CONTROL, never touch it.
- The per-replica cursor and its session affinity (ADR 0027) are unchanged.
- Rejected: deleting the events door and the subscription, which would leave binding staleness to its TTL alone and drop
  the governance feed over HTTP; and a watchdog self-probe, which would put probe traffic on CATALOG_CONTROL and extend a
  workaround meant to be deleted.

## Superseded in its mechanism, 2026-11-14 (XC-199 step D4, #787)

Each catalog replica follows `catalog.control.v1` on CATALOG_CONTROL over its own connection to the bus, by reading the
stream by sequence (nats-py's `get_msg`, `lakehouse.bus.Bus.follow`): it begins after the stream's last message and asks
for the subject's next message after the last one it handed on, every second while there is none, so the bus holds no
consumer for a replica. An ordered consumer was built first and dropped the same day: a message whose publisher gave it
an empty body and a `Status: 100` header made nats-py 2.16.0 re-create the consumer without end (measured on nats-server
2.15.2: 909 consumers in 14 s, and no later message handed on), and its create grant let the catalog's user have the
bus copy the stream onto a subject it may not publish. The mirror CATALOG_CONTROL_BROADCAST, its per-pod durables, `catalog.controlConsumerInactiveSeconds`,
`catalog.controlDlqTopic` and the catalog's pub/sub Component are gone; the stream Job's retire phase removes the
mirror once. The catalog's bus user reads the stream's messages and holds no ack and no consumer grant. This record's premise was a sidecar that binds its consumer once and never
re-creates it; the service's own client reads on from its place on its next connection, so a lapse costs no restart. ADR
0027's broadcast and per-replica buffer stand.
