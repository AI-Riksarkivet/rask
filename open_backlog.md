# open_backlog — what is left, and why

**Working plan.** Delete this file when everything below has landed or been ruled out by the owner.
It lives at the repo root, not in `docs/`, because `docs/` asserts SETTLED and these are not.

Pinned 2026-08-24. Each item says what it needs, not just what it is.

---

## BLOCKED ON AN OWNER DECISION

### ingest #4 — `table_published` as the single cascade trigger

**A standing ruling contradicts this item, and it must be resolved before any code moves.**

`docs/architecture/medallion-cascade.md` §10 — *"DECIDED — the two cascade heads are distinct events,
and both must fire"* — rules that `/bronze-arrival` and `/publication-arrival` describe different work:
different datasets, and a version RANGE the ingest head has no concept of. It states that unifying
them *"would collide two legitimate cascades onto one `instance_id`, and Dapr would answer the second
as a duplicate — silently dropping one of two pieces of work that must both happen."*

**What ingest #4 actually wants is narrower than the ruling forbids.** The lane-matching guard exists
because two ingest lanes (`bronze$events`, `bronze$pages`) share the `medallion.bronze` topic, so every
mover subscribed to it sees both and must filter. `subTopic` is ALREADY per-tier config
(`medallion.bronze` / `medallion.silver` / `medallion.media`), so a third option exists that §10 does
not foreclose:

> **Give each LANE its own subTopic.** The guard becomes unnecessary because an arrival only reaches
> the mover that wants it. Both cascade heads keep firing. §10 is untouched.

That is still a change to EVERY estate — every mover's `subTopic` and every publisher's topic move
together, or triggers go nowhere.

**Decision needed:** per-lane topics (respects §10), or overturn §10 and unify the heads?

**Blocks:** item 5 (the whole loop from the UI). A UI-declared lane stops at bronze today because the
guard DROPs its arrival as another lane's.

---

## NEEDS A MEASUREMENT, NOT A DECISION

### batch B9 — `RASK_WF_INLINE_MAX_BYTES`

Measure `enumerate_chunks`' serialized result at advertised scale, then set the threshold. The 120 MB
figure in the plan text is admitted arithmetic. **Shipping a guessed value is what the invariant
explicitly forbids**, so this waits on a live estate at scale rather than on effort.

Related leak, bounded but not removed: `services/flows` caps `NodeResult.payload_text` at 256 KiB and
still writes that document into workflow history as an output, and again per dependent.

---

## NEEDS A QUESTION ANSWERED FIRST

### ingest #3 — the source pin

REFUTED with no replacement. `ItemSource.where` is the MEDIA-REGISTRY key, validated against
`state.registry` whose ids are bare — so qualifying it makes every send refuse at the door. The
opposite was already tried and broke worse: sending the bare media name made the catalog authorize
`table:transcripts_v2`, an object that does not exist, and FGA denies before it checks existence, so
the ENTIRE publish failed (observed live 2026-08-03).

A real fix resolves the pin server-side from registry id to catalog id, or carries a second field.
**Both depend on whether every corpus has a catalog node at all, which was never established.**
Establish that first; the fix follows from the answer.

---

## LANDED (kept here until the parent items close)

- **B14 — one `transform_batch`** — `5a8dd3b7`. The derivers moved to `service_kit.lakehouse.media`
  behind a `service-kit[media]` extra; both drivers import ONE implementation. The drift-pin test
  became an identity test plus a "the local names are gone" test.
- **B4** — `f41bedea`, `549c348c`. Missing fields land with their first consumer, deliberately.
- **B11**, **B15** — done / closed as ruled.

---

## SMALLER, UNBLOCKED

- **`RASK_INGEST_LANCE_ROOT` is empty**, so the `lance-append` source kind is advertised in the live
  registry and cannot be used. Either configure the root or stop advertising the kind.
- **`scripts/ray_lance_job.py` is not baked** into `.docker/ray-cluster.dockerfile`. A lane naming it
  dies `exit 2` with nothing pointing at the image.
- **The gate resolver is wired on the in-process path only.** The Ray lane is submit-and-ack, so its
  gate runs later off the catalog's publish; it still reads the chart's band, not the declared one.
- **Two run-like nav labels.** `Ingest ▸ Runs` (ingest runs) and `Workloads ▸ Jobs` (Ray jobs) are
  near-synonymous in a sidebar. Rename to "Ingest runs" / "Batch jobs".
- **A lane cannot show its own health.** `/compute/lanes` shows what a lane DECLARES and nothing about
  what it did — a lane failing every run looks identical to a healthy one.
- **The lane→runs link is unfiltered.** It points at `/compute/jobs`, not that lane's jobs.
  `rask.lane` is on the metadata now, so the filter is available.
- **`is_blob_field` is defined twice** — `service_kit/lakehouse/blobs.py` and
  `service_kit/lancekit/blobs.py`. The same class of duplication B14 just removed.
- **compute and studio ship no `e2e/` harness at all.**
