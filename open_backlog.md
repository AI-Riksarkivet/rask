# open_backlog — what is left, and why

**Working plan.** Delete this file when everything below has landed or been ruled out by the owner.
It lives at the repo root, not in `docs/`, because `docs/` asserts SETTLED and these are not.

Pinned 2026-08-24. Each item says what it needs, not just what it is.

---

## CLOSED SINCE THIS FILE WAS PINNED

### ingest #4 — what a mover reads — **DONE** (`ff71aedb`, `568b8fa9`)

The owner answered the decision this file recorded, and the answer made the item smaller than either
option it offered. Neither per-lane subTopics nor unifying the cascade heads was the fix: the head
recognised exactly one hard-coded dataset and published nothing for anything else, so the guard this
item wanted retired was never even reached.

What changed is where a mover's INPUT comes from — the lane record instead of its env. `stage_run`
was already a parameterised Dapr Workflow worker; the daemon was four lines computing its input
before scheduling it. `medallion-cascade.md` §10 is untouched: both cascade heads still fire.

Proven live from the browser on a table that existed in no configuration anywhere.

---

### batch B9 — the dispatch ceiling — **DONE** (`c93183c1`)

The owner supplied the scale this item was waiting on — "over 10 million images, 50,000 hours of
video" (2026-08-24) — and measuring against it found a live DEFECT, not a missing knob. A real
`ChunkSpec.model_dump()` serialises to 317 B, so at `CHUNK_SIZE=1000` the ceiling was **9,923,000
units**: a 10M-image corpus reached 3.02 MiB against the 3 MiB dispatch budget and would have been
REFUSED at the enumeration seam. Note the direction — `enumerate_chunks` returns ONE result holding
every chunk, so a SMALLER chunk size means MORE descriptors and a LOWER ceiling.

`CHUNK_SIZE` 1000 → 10000, chosen by the suite's own 5x-headroom rule rather than by preference.

Still open, and untouched by this: `services/flows` caps `NodeResult.payload_text` at 256 KiB and
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

- **The promotion review had never worked in-cluster** — live fix + `b2f3af6e`. `lance-statestore`
  is scoped, and the DEPLOYED Component was missing `medallion-producer`. The chart has been right
  since `a71c12a5` and helm's own stored manifest carries the app-id; the cluster object did not,
  because helm only patches fields that changed BETWEEN releases, so an out-of-band edit to an
  otherwise-stable field is never corrected. Every held promotion answered INTERNAL and then
  NIL-DEREFERENCED daprd (dapr 1.18.1), taking the cascade head's whole sidecar down — 3 crashes in
  33 minutes. Live object reconciled and verified: the review now schedules and the sidecar has
  restart count 0. `probe_actor_state_store` was added at all six runtime-hosting lifespans so the
  next occurrence says so instead of logging "Workflow engine started" and dying later.
  **NOT YET DEPLOYED** — the probe is committed and green in the suite; it starts reporting when the
  images are rebuilt and rolled.

- **B14 — one `transform_batch`** — `5a8dd3b7`. The derivers moved to `service_kit.lakehouse.media`
  behind a `service-kit[media]` extra; both drivers import ONE implementation. The drift-pin test
  became an identity test plus a "the local names are gone" test.
- **B4** — `f41bedea`, `549c348c`. Missing fields land with their first consumer, deliberately.
- **B11**, **B15** — done / closed as ruled.

---

## CARRIED FROM `open_batch_process.md` (that file is retired; these rulings are not)

Every item in that plan doc is DONE or ruled, so the file was deleted. Two rulings outlive it and are
kept here because a future reader needs the REASON, not just the verdict:

- **B7 — resolve once, carry the value. DEFERRED, and the audit overstated it.** `submit_stage`
  re-calls `resolve_lane_async`, but `submit_stage` is an ACTIVITY: its result is recorded in history
  and replayed, so this is not a determinism break. It costs clarity and one extra resolution per
  submit, not correctness. Apply the invariant the next time that signature changes; it does not
  justify touching the submit path on its own.
- **B8 — vocabulary-validated `TransformSpec` fields. DEFERRED.** The record exists and validates;
  what is missing is `actor.resources`, `batch_bytes`, `enabled` and an `exclude_unset` merge. Every
  one is a knob for a workload that would declare it, and the estate ships no declared lane using
  them. **Adding config nothing reads is the dead-config defect this plane has been bitten by twice**
  — the orphan-scan lever with no path from values, and a state-store scope naming an app-id that did
  not exist. The fields land with their first consumer.

Done there and needing nothing further: B4 (`f41bedea`, `549c348c`), B9 (`c93183c1`), B11,
B14 (`5a8dd3b7`), B15 (closed as ruled).

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
