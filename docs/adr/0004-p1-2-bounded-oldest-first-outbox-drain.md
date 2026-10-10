# 0004. P1.2 — bounded, oldest-first outbox drain

**Decision.** The reconcile drain caps how many staged events it processes per tick, **oldest-first**,
carrying the remainder to the next tick (`outbox_drain_limit`).

**Rationale.** The drain previously `list()`ed the entire outbox prefix into memory under the single-flight
lock, so a backlog could OOM or stall the tick. Bounding it keeps each tick's memory and work finite while
still guaranteeing every survivor is eventually drained (a unit test drains N > cap across two ticks).
