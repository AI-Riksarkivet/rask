# open_ingest_design — what is still outstanding

**Working plan.** Delete this file when the items below have landed or been ruled out by the owner.
It exists because `docs/` asserts SETTLED, and these are not: they are deferred features, one
refutation that still has no replacement, and two changes blocked on a live estate. They were briefly
filed under `docs/architecture/` on 2026-08-22 and moved back the same day — unfinished work in
`docs/` reads as decided no matter what the prose says.

The settled half — the five decisions as they stand, what landed, the tuple-seeding policy and the
cross-cutting rules — is `docs/architecture/ingest-and-tier-movement.md`.

---

## Ruled: deferred, with the reason

**1b — the `lance-append` source kind, and its three guards.** Deferred as a feature; the guards go
with it, since a guard for a kind that does not exist is unreachable code. What 1b actually wanted is
mostly already available: `POST /v1/table/{id}/register` is the existing catalog door and does the
whole job — parent check, native register, ownership seeding — so "an existing Lance table becomes
governed" is solved. What is missing is the INGEST-run form: reading a bounded row range out of an
ungoverned `.lance` and projecting it into BRONZE_SCHEMA. No workload asks for that today; the
registered path covers the case the estate has. Its three guards land with it if it is ever built —
refuse a source that resolves to a catalog table (naming the medallion mover, because copying between
governed tiers is the cascade's job), refuse a schema that cannot produce BRONZE_SCHEMA at ACCEPT
rather than hanging, and run a bronze conformance check in front of register.

**1c — the cron trigger.** Deferred, and the ordering turned out to matter. Incremental ingest is
REACHABLE today: the anti-join runs on every `POST /v1/ingests`, so what the cron adds is automation,
not capability. Shipping the cron before the ceiling existed would have put an unbounded
O(existing rows) read on a clock; now that `RASK_INGEST_INCREMENTAL_MAX_ROWS` exists it is safe to add
when an operator wants it. The plan's companion note — "state plainly in the code that incremental
ingest is a scheduled poll at the outer boundary and event-driven from bronze inward" — is recorded
here instead, because the statement had no home while the outer boundary did not exist.

**2 — the frontend `merge_insert` proxy.** Deferred. The RULING (below) is that a manual push uses
`merge_insert`, not a raw insert, and the catalog's door already accepts it. What is missing is the
zone-side proxy and a `mergeRows` helper — UI for an operation no surface currently offers.

**3 — FIX 3, the source pin.** REFUTED rather than deferred, and this is the one to read before
re-opening. FIX 3 says to carry the send's dataset as a catalog-qualified id so `source_pin` resolves.
It cannot: `ItemSource.where` is the MEDIA-REGISTRY key, validated by `_refuse_unknown_datasets`
against `state.registry`, whose ids are bare — so qualifying it makes every send refuse at the door.
And the opposite change was already tried: `source_pin`'s docstring records that sending the bare
media name made the catalog authorize `table:transcripts_v2`, an object that does not exist, and FGA
denies before it checks existence, so the ENTIRE publish failed (observed live, 2026-08-03). The
guard FIX 3 wants removed IS the fix from that day. A real fix resolves the pin server-side from
registry id to catalog id, or carries a second field — both depend on whether every corpus has a
catalog node at all, which was never established. Pinned by
`services/annotator/tests/test_item_source_where_is_the_media_registry_key.py`.

**4 — make `table_published` the single cascade trigger.** BLOCKED, not deferred. The mechanism
exists behind `medallion.cascadeViaPublish` and the chart says "it should die once every estate runs
on it", but flipping it is a live-estate change against a cluster whose tiers are not provisioned —
the same blocker as tier provisioning. Retiring the lane-matching guards is the same change and the
same blocker. See `docs/architecture/medallion-cascade.md` for why BOTH cascade heads must fire
meanwhile; that is a ruling, not an interim state.
