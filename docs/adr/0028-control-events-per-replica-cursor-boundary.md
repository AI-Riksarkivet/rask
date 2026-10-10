# 0028. control-events — per-replica cursor boundary

**Decision.** The ring buffer **and** its monotonic cursor are **per-replica** (each broadcast subscriber
buffers independently, in process memory). This is correct at the default `services.catalog.replicas: 1`.
Scaling the catalog past one replica requires **session affinity** (a client's polls stick to one
replica) **or a shared buffer** — a NATS KV-backed buffer is the natural candidate when task #20
(NACK/CRD-managed streams) unparks — otherwise a load-balanced poll hits different replicas, sees
inconsistent cursors, and degrades to noisy `reset`s.

**Rationale.** Safe-by-construction degradation: because an event is only a hint and the consumer
(`admin.remote.ts`) dedups by `event_id` and clears on `reset`, a multi-replica catalog degrades
*noisily, never wrongly* — the cost is redundant re-reads, not wrong data. Accepting the boundary keeps
the shipped feature dependency-free (no shared store) at the deployed replica count, with the scaling
path named rather than silently missing.
