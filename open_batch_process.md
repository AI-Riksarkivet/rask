# open_batch_process — what is still outstanding

**Working plan.** Delete this file when the slices below have landed or been ruled out by the owner.
It exists because `docs/` asserts SETTLED, and these items are not: they are deferred, blocked, or
waiting on a measurement. They were briefly filed under `docs/architecture/` on 2026-08-22 and moved
back the same day — putting unfinished work in `docs/` makes it read as decided no matter what the
prose says.

The invariants that ARE settled — the eight landed and pinned, B11's two columns, and the
cross-cutting rules — live in `docs/architecture/batch-processing-invariants.md`. This file is only
the remainder.

---

## Ruled: deferred, with the reason

**B4 — transform identity reaches the resume predicate.** DONE (`f41bedea`, `549c348c`).

`Stage.identity` from the declaration plus the two things a declaration cannot see (the actor class
the composition root bound, the runner env); a `transform_version` column stamped on every row a run
writes; and both resume filters comparing them.

Two shapes, two granularities, and the second is a Lance constraint rather than a shortfall:

* **non-blob tables** — row granularity, exactly as specified: `resume_filter` claims rows that are
  NULL *or* carry a different version, so an edited transform re-derives only its own rows.
* **blob tables** — rebuild granularity. `merge_insert` crashes Lance's blob decoder (§7.1), so the
  only legal write is the all-or-nothing `_rowid` attach and no partial update exists to drive. The
  rebuild stamps the version through that same attach, and the branch consults it: a column built by
  a superseded transform is rebuilt instead of being reported as nothing-to-fill. Before this, a
  populated column was indistinguishable from a correct one.

Findings worth keeping. **Lance rejects `IS DISTINCT FROM`** — the one-word SQL for "a NULL version
counts as stale", which this needed because `!= 'x'` is NULL on a NULL left side and a NULL predicate
DROPS the row. The string-asserting unit test passed; the test that EXECUTES the predicate against a
real dataset is what caught it, so the long NULL-safe form is load-bearing. And **`packages/ratch` had
no tests at all** and sat in no testpath — a workspace member with zero tests passes the enrolment
gate, because that gate checks that test directories which exist are listed.

Unversioned columns are deliberately NOT treated as stale. Pre-B4 data has unknown provenance, and
rebuilding on that basis would re-run every blob stage in the estate the first time this ships.

**B7 — resolve once, carry the value.** Deferred, and the audit's framing overstates it. `submit_stage`
re-calls `resolve_lane_async`, but `submit_stage` is an ACTIVITY: its result is recorded in history and
replayed, so this is not the determinism break `RunLimits` records. What it costs is clarity and one
extra resolution per submit, not correctness. The invariant is worth applying the next time that
signature changes; it does not justify touching the submit path on its own.

**B8 — a declared `TransformSpec` record, vocabulary-validated at admission.** Deferred. The record
exists and validates; what is missing is fields (`actor.resources`, `batch_bytes`, `enabled`) and an
`exclude_unset` merge. Every one of them is a knob for a workload that would declare it, and the
estate ships no declared lane using them. Adding config nothing reads is the dead-config defect this
plane has been bitten by twice — the orphan-scan lever that existed with no path from values, and a
state-store scope naming an app-id that does not exist. The fields land with their first consumer.

**B9 — an oversized activity result becomes a handle.** Deferred, and BLOCKED on a measurement rather
than on a decision. The invariant requires the threshold to be measured, and the plan says so: its
own §5.4 precondition #1 is to measure `enumerate_chunks`' serialized result at advertised scale, and
the 120 MB figure in the text is admitted arithmetic. That measurement needs a live estate at scale,
which is the same blocker as tier provisioning. Shipping a guessed `RASK_WF_INLINE_MAX_BYTES` would
be the thing the invariant explicitly forbids. Note the related leak is bounded but not removed:
`services/flows` caps `NodeResult.payload_text` at 256 KiB and still writes that document into
workflow history as an output and again per dependent.

**B11 — boot-env vs live-spec, two columns, written down.** Done here rather than deferred; the
columns are below.

**B14 — one `transform_batch`, two drivers, one drift pin.** **DONE (`5a8dd3b7`).** The derivers
(`is_image`, `derive_thumbnail`, `derive_embedding`) and the blob-field pair moved to
`service_kit.lakehouse` behind an optional `service-kit[media]` extra, and both drivers now import ONE
implementation. The stated blocker — "the Ray script is baked into the cluster image and cannot import
the service" — was true and beside the point: it can import `service_kit`, which it already did for
`stamp_stage`.

The drift-pin test became an IDENTITY test (the script must reference the shared function objects)
plus a second test asserting the local names are GONE rather than merely unused, because a dormant
copy is a copy. No Lance path was touched: `lr.read_lance` / `lr.write_lance` / `blob_array` are
unchanged, and both documented paths stand.

**B15 — bound dashboard reads.** Closed as ruled. Its first lever, source-bounding, is done
(`MAX_JOBS`/`MAX_TASKS` in `ray_kit/dashboard.py`, with the 81,155-job OOM measurement attached).
The remainder is CONDITIONAL — "if a cache is added: compute owns it, written from a Dapr cron
binding, never an in-process refresh thread" — and no cache exists. It is a rule for a future change,
not outstanding work. The repo-wide A13 gate against in-process polling already enforces the half
that could be enforced today.
