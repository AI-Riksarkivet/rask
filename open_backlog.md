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
