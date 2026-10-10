# 0029. control-events — estate-admin scope

**Decision.** `GET /v1/events` is gated by a real **catalog-side** FGA check of `can_observe_events` on
the fixed root object (`settings.fga_root_object` = `warehouse:lance_catalog`), an owner-tier
**platform** privilege — a mere project admin gets 403, and the client treats 403 as terminal. A
*meaningful* poll (events delivered or a reset) is audited (`event_stream_opened`); empty ticks are not,
so a 5s-polling console does not flood the audit trail.

**Rationale.** The feed is **estate-wide** — the buffer holds every project's governance changes
(broadcast subscription, no per-tenant partition) — so authorization scope must equal data scope. The
first draft's per-project `can_administer` param let any project admin read the whole estate (the #12
review fix, 2026-07-23); and the `/audit` "admin bar" precedent lived only in the BFF, so this feature
had to add the catalog-side gate itself. Honest limitation, accepted: live refresh is admin-only — the
non-admin whose *own* access just changed does not get a live refresh; the benefit is for an admin
observing the estate.
