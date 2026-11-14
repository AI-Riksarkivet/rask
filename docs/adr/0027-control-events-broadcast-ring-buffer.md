# 0027. control-events — broadcast + ring buffer

**Reopened 2026-11-09** by [ADR 0199](0199-nats-is-the-one-bus-and-the-design-review-reopens-eight-decisions.md): the per-replica ring buffer loses to cloud native; the poll is to read the durable stream by sequence, decided in the row that implements it.

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

**Amended 2026-11-14 (#787, XC-199 step D4).** The catalog now holds a NATS client of its own (`lakehouse.bus`) and
follows `catalog.control.v1` on it, so the dedicated Component `catalog-control-pubsub` is gone and the rationale's "the
catalog has no NATS client and must not grow one" no longer holds. The semantics stand: every replica receives every
event by reading the stream by sequence, a starting replica begins after the stream's last message rather than replaying
history into its buffer, and the per-replica ring buffer is unchanged. The read is recorded in
[ADR 0182](0182-each-catalog-replica-hears-the-control-broadcast-through-a-durable-of-its-own.md), superseded in its
mechanism the same day. The reopening by ADR 0199 (the poll reads the stream by sequence) is still open.
