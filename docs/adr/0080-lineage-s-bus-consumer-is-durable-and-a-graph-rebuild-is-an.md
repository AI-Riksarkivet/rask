# 0080. Lineage's bus consumer is durable, and a graph rebuild is an explicit step (LH-303, owner 2026-09-26)

Lineage consumed `lineage.events.v1` through an EPHEMERAL JetStream consumer with `deliverPolicy: all`,
on purpose: every pod restart replayed the retained stream into the idempotent ingest, and that replay
was the stated way to rebuild the graph. LH-199's live outage proof showed what else it meant. NATS
scaled to 0 and back deleted the ephemeral consumer and Dapr 1.18.1's sidecar never re-created it, so
lineage recorded nothing from the bus from 19:49:25Z until its pod was restarted at 19:53:39Z, while the
three durable consumers on the same stream resumed. Nothing alerted.

Every subscriber is now durable + queue group, lineage included (`lineage-durable`, still
`deliverPolicy: all`, so its first attach replays the stream once). A rebuild is an operator step:
`nats consumer rm LINEAGE lineage-durable`, then `kubectl rollout restart deploy/rask-lineage`
(docs/RESILIENCE.md). Pinned by `tests/unit/test_every_bus_subscriber_survives_a_bus_restart.py`. The
catalog's own control-event broadcast consumer stays ephemeral (one per replica, no queue group), so a
NATS restart removes it too; that is parked as LH-305, since a shared durable cannot serve a broadcast.

**Amended 2026-11-14 (#787, XC-199 step D4).** Lineage's consumer stays durable and shared, and it is now a pull
consumer, `lineage-ingest`, which the stream Job declares (delivered by start sequence) and each lineage replica pulls on
its own connection to the bus; no sidecar subscribes for it, because a sidecar's subscription could not be rebound after
a configuration change and closed for good after a bus outage of about two minutes. The stream Job declares a new
`lineage-ingest` to begin after the ack floor of `lineage-durable` while that consumer exists, and its retire phase
removes `lineage-durable`, so the rebuild step above names a consumer that no longer exists: once `lineage-durable` is
gone, a `lineage-ingest` that does not exist when the stream Job runs is declared at the stream's first message, and
that is the consumer a rebuild now removes. The test named above is deleted with
the Components it walked.
