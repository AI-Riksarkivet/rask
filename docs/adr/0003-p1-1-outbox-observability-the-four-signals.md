# 0003. P1.1 — outbox observability (the four signals)

**Decision.** The lineage outbox is an external boundary (S3 + pub/sub) and carries the four golden
signals — counters for staged / drained / poison plus gauges for outbox **depth** and **oldest-age** —
exported OTLP-direct to GreptimeDB, with a Perses alert on `depth>0` sustained.

**Rationale.** Without depth/age a leaking outbox is invisible and every durability property is
unobservable. A gauge pinned at 0 is indistinguishable from a *stuck* one, so the alert signal was driven
live (survivors staged → depth rises → relay drains → depth falls) and read back out of GreptimeDB, not
merely asserted to be emitted.
