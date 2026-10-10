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
