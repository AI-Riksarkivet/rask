# 0026. CATALOG_CONTROL wildcard masking — accepted at replicas:1 (2026-07-23)

**Decision.** The /streams dead-subscription detector matches expected consumers by Dapr deliver group
(`queueGroupName` = the subscriber app-id), but the catalog's `catalog.control.v1` subscription is
deliberately **group-less** (broadcast: every replica buffers every event), so the BFF keys it as `"*"`
— *any* bound group-less ephemeral on the `CATALOG_CONTROL` stream satisfies the expected catalog entry
(`+server.ts`, the `serviceLabel` / `key` logic). Known nit, accepted: an operator's `nats` CLI
inspection consumer (also group-less, also ephemeral) can **mask a dead catalog broadcast** for as long
as it is attached.

**Rationale.** There is nothing group-shaped to match on — the broadcast semantics *require* the absence
of a deliver group, and Dapr's ephemeral consumer names are generated, so no stable identifier exists
today. The window is small (an inspection consumer detaches when the operator's terminal closes) and the
blast radius at `replicas: 1` is one refresh-hint feed whose durable record is the audit trail anyway.
**Tighten-when-it-bites:** give the catalog's control subscription a *named ephemeral prefix* (Dapr
component `consumerID`/name plumbing) and match on the prefix instead of `"*"` — do this the first time
a masked dead broadcast survives past an operator session.
