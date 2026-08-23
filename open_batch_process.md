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

**B4 — transform identity reaches the resume predicate.** BUILT for non-blob tables (`f41bedea`);
the blob-table half remains and is named below.

All three parts landed: `Stage.identity` from the declaration plus the two things a declaration cannot
see (the actor class the composition root bound, the runner env); a nullable `transform_version`
column stamped on every row a run writes; and `resume_filter` comparing them, replacing the bare
`<output> IS NULL` at both sites in `ratch/core/driver.py`.

Two findings worth keeping. **Lance rejects `IS DISTINCT FROM`** — the one-word SQL for "a NULL
version counts as stale", which this needed because `!= 'x'` is NULL on a NULL left side and a NULL
predicate DROPS the row, silently skipping the rows a first identity-aware run must claim. The
planner answers "not supported SQL in lance"; the string-asserting unit test passed and the test that
EXECUTES the predicate against a real dataset is what caught it. And `packages/ratch` had no tests at
all and sat in no testpath — a workspace member with zero tests passes the enrolment gate, because
that gate checks that test directories which exist are listed.

**What remains is blob tables, and it is not a variation of this work.** B4's capability is a
SELECTIVE re-run; a blob-bearing table's only legal write is the all-or-nothing `_rowid` rebuild,
because `merge_insert` crashes Lance's blob decoder (invariant §7.1, `driver.py:134`/`:221`, refusal
at `:242`). The predicate stays `IS NULL` there, stated at the site. Delivering B4 on those tables
means changing §7.1 — a partial-update path that does not go through `merge_insert` — which is a
Lance write-path question, not a resume-predicate one. Whoever takes it starts there, not here.

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

**B14 — one `transform_batch`, two drivers, one drift pin.** Deferred, and it is the largest genuine
debt on this list. The medallion ships two independent implementations of the bronze→silver transform
(`medallion/services/compute.py` and `scripts/ray_stage_job.py`) whose tabular paths nothing compares,
so they can drift silently and the drift shows up as a Ray-lane result that differs from the
in-process one. Unifying them is a real refactor across an image boundary — the Ray script is baked
into `ray-cluster.dockerfile` and cannot import the service. Recorded as debt with its cost, because
the honest fix is a shared module both can import, not a test that compares two behaviours after the
fact.

**B15 — bound dashboard reads.** Closed as ruled. Its first lever, source-bounding, is done
(`MAX_JOBS`/`MAX_TASKS` in `ray_kit/dashboard.py`, with the 81,155-job OOM measurement attached).
The remainder is CONDITIONAL — "if a cache is added: compute owns it, written from a Dapr cron
binding, never an in-process refresh thread" — and no cache exists. It is a rule for a future change,
not outstanding work. The repo-wide A13 gate against in-process polling already enforces the half
that could be enforced today.
