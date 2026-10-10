# 0031. control-events — fail-open emit contract

**Decision.** Every control-plane mutation endpoint `await`s the emit (`packages/service-kit/src/service_kit/control_emit.py`)
**after** the backend/FGA mutation succeeds — so a change that did not happen is never announced — and
the emitter **swallows every error**: a bus outage degrades to "no live refresh + the audit trail still
records it", never a failed mutation. The **audit trail is the durable compliance record**; the event
stream is only the live-notify layer, and an event is a refresh hint, never authoritative data — on
receipt the UI re-reads state through the normal FGA-governed path, so the feed can never disclose more
than the caller may already read, and a dropped/duplicated/late event only costs a redundant (or
slightly delayed) re-read. Actor is the **verified** OIDC subject, never self-asserted.

**Rationale.** This mirrors the `lineage_emit` fail-open principle: eventing must never be able to fail
a mutation. Splitting durability (audit, GreptimeDB) from liveness (bus, ring buffer) is what makes the
in-memory drop-oldest buffer and best-effort publish acceptable — nothing that matters is *only* in the
stream.
