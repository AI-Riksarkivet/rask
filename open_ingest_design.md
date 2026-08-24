# open_ingest_design — what is still outstanding

**Working plan.** Delete this file when the items below have landed or been ruled out by the owner.
It exists because `docs/` asserts SETTLED, and these are not: they are deferred features, one
refutation that still has no replacement, and two changes blocked on a live estate. They were briefly
filed under `docs/architecture/` on 2026-08-22 and moved back the same day — unfinished work in
`docs/` reads as decided no matter what the prose says.

The settled half — the five decisions as they stand, what landed, the tuple-seeding policy and the
cross-cutting rules — is `docs/architecture/ingest-and-tier-movement.md`.

---

## Landed (2026-08-23)

**1b — the `lance-append` source kind (`8e2da00a`).** Built with all three guards. The unit grain
changed during the build: the plan said "a bounded row range", and `lance_sdk.md` refuses it —
offsets "are not stable" across versions, so an offset-keyed unit would re-land a table's whole tail
after any early delete, which is the duplication the anti-join exists to prevent. `file_format.md`
gives the stable grain (fragment ids are reserved and monotonic), so the unit is a FRAGMENT and the
payload is its rows as Arrow IPC.

**1c — the incremental cron trigger (`e629e2cc`).** Landed after `RASK_INGEST_INCREMENTAL_MAX_ROWS`
existed, which was the ordering the plan called for: a fresh idempotency key per tick, sharing
`dispatch_run` with the route so the poll and the manual call cannot drift.

**2 — the frontend `merge_insert` proxy (`7ea1b7d8`).** The zone-side proxy plus a `mergeRows` helper
on `requestBytes`, which also dropped the BFF-JSON residual ceiling 13 -> 12.

---

## Ruled: deferred, with the reason

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

**4 — make `table_published` the single cascade trigger.** Still open, and its STATED blocker is
gone: it read "a cluster whose tiers are not provisioned", and the tiers were provisioned 2026-08-23
(`acme-bronze` / `acme-silver$features` / `acme-gold$catalog`, seed `converged=7 created=25 failed=0`).
`medallion.cascadeViaPublish` is also already `true` in the deployed values, so the mechanism is not
merely available, it is ON in this estate.

What actually remains is the part the old wording hid behind the provisioning excuse: retiring the
lane-matching guards is a change to every estate, and it must not be made until ONE cascade has been
driven end to end through the publish head and observed reaching gold.

**THAT PROOF NOW EXISTS (2026-08-23).** A cascade was driven end to end through the publish head on an
isolated tenant (`gateprobe`) and observed reaching gold: the Ray stage job SUCCEEDED, the workflow
re-published with `ray_job_done`, the mover logged `medallion_stage_moved`, the `published` tag
advanced to version 7, and `gateprobe-gold` received `catalog`. The stated blocker — reading
`APP_API_TOKEN`, since `POST /produce` answers 403 without it — was not solved by extracting the
credential but by calling from INSIDE the mover pod, where it is already in the environment; the
secret never left the cluster, which is the only form of this that respects the secret rule.

So this item is no longer blocked on a proof. What is left is the change itself — and reading it
against the medallion's own record (2026-08-24) turns up a CONFLICT this item never acknowledged.

**A STANDING RULING CONTRADICTS THE HEADLINE.** `docs/architecture/medallion-cascade.md` §10 —
"DECIDED — the two cascade heads are distinct events, and both must fire" — rules that
`/bronze-arrival` and `/publication-arrival` describe different work: different datasets, and a
version RANGE the ingest head has no concept of. Unifying them "would collide two legitimate cascades
onto one `instance_id`, and Dapr would answer the second as a duplicate — silently dropping one of two
pieces of work that must both happen." So `table_published` CANNOT become the single trigger without
overturning that ruling, and this item may not simply proceed.

**BUT THE GUARD AND THE HEADLINE ARE SEPARABLE, which neither document noticed.** The lane-matching
guard exists for a narrower reason than the trigger question: two ingest lanes (`bronze$events`,
`bronze$pages`) share the `medallion.bronze` topic, so every mover subscribed to it sees both and must
filter. `subTopic` is ALREADY per-mover config in the chart (`medallion.bronze` / `medallion.silver` /
`medallion.media`), so a third option exists that §10 does not foreclose:

> Give each LANE its own subTopic. An arrival then reaches only the mover that wants it, the guard
> becomes unnecessary, and BOTH cascade heads keep firing exactly as §10 requires.

That is still a change to every estate — every mover's `subTopic` and every publisher's topic move
together, or triggers go nowhere — but it delivers this item's actual goal without touching the
ruling.

**OWNER DECISION NEEDED, and this item is blocked on it, not on effort:** per-lane subTopics, or
overturn §10 and unify the heads?

Retiring the lane-matching guards. See `docs/architecture/medallion-cascade.md`
for why BOTH cascade heads must fire meanwhile; that is a ruling, not an interim state.
