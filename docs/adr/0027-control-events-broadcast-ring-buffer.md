# 0027. control-events — broadcast + ring buffer

**Decision.** The control-plane change-event feed (shipped + live-proven 2026-07-23,
`scripts/verify_control_events.sh`) rides a **dedicated** Dapr pub/sub component
(`catalog-control-pubsub`, topic `catalog.control.v1`) that the catalog subscribes to **without a
`queueGroupName`** — with JetStream, no deliver group means **every** catalog replica receives **every**
event (broadcast, not competing-consumer) — and with `deliverPolicy: new` on an **ephemeral** consumer:
a restarting replica does not replay retained history into its buffer, it starts fresh at the stream head.
Each replica appends events into a bounded, in-memory, drop-oldest ring buffer
(`services/catalog/core/control_buffer.py`) with a monotonic cursor and `event_id` dedupe, served by
`GET /v1/events?since=<cursor>`.

**Rationale.** The catalog has no NATS client and must not grow one (the `lineage_emit.py` no-broker-
client principle); a per-connection JetStream ephemeral consumer was rejected in the 2026-07-22 review
because Dapr subscriptions are app-level/startup-registered. The no-queueGroup broadcast is the
multi-replica-correct fan-out with zero new dependencies. `deliverPolicy=new` + ephemeral is correct
here (where it would be a bug for the cascade stage runners) because events are **refresh hints**, not the
durable record — the audit trail is — so replaying history into a fresh buffer would only re-announce
stale changes; a client bridging a restart just sees `reset` and re-reads authoritative state.
