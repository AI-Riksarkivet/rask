# 0176. A table's history is fenced between reclaims and ref writes (2026-10-24)

**Status:** accepted. Source: LH-308 (#329), admitted by the owner on 2026-10-20, whose admission names the fix taken
here: "a lease or conditional record on the control root that tags/create, tags/update, branches/create and every
reclaim path take".

## Context

A tag pins a version against Lance's cleanup, and a branch keeps the files of the version it was cut from
(`lance_docs/lance/guide/tags_and_branches.md:63-64` and `:131-133`). Lance reads both lists once, when `cleanup_old_versions` starts, and deletes
against that listing (`rust/lance/src/dataset/cleanup.rs:472-511` at v13.0.0). Nothing in Lance orders a ref written
while a cleanup runs with that cleanup, and no Lance primitive offers a guard.

Measured on pylance 13.0.0 on a local store, with a tag or branch created on version 2 a few milliseconds into
`cleanup_old_versions(older_than=0)`: in 60 races each, the tag named a deleted version in 20 and the branch could not be
read in 9. On pylance 12.0.0 the LH-206 review measured the same through the doors: `version/delete` lost 31 of 60
(tags) and 94 of 120 (branches), and `maintenance/run` 59 of 60.

The estate's reclaims are the catalog's `maintenance/run`, `version/delete` and erasure verification, and maintenance's
expiry pass. Its ref writes are the catalog's `tags/create`, `tags/update` and `branches/create` doors, publication's tag
move, which goes through the same functions, and the erasure release's branch re-creation. Writers are vended no put
under `_refs/` (`vending.build_session_policy`), so every ref a client writes passes through one of those doors.

## Decision

- **Each takes the table's history fence before it acts, and the two kinds never overlap.** The fence is one record
  per table on the control root, `_history_fences/`, keyed by the table's uuid (ADR 0173 §8). It lists its live holders,
  each a reclaim or a ref write (`service_kit.lakehouse.history_fences.TableHistory`). Holders of one kind share it,
  since the race is a ref written during a reclaim, never two of either kind. A take is a create-if-absent or a replace
  under the ETag it read, the store's conditional write that location claims use.
- **A door the fence refuses answers 409, code 14** (ConcurrentModification), and writes or deletes nothing. The caller
  retries.
- **Maintenance's pass holds it only while it deletes**: versions past the policy's threshold, or files in the table's
  data directories (ADR 0175). A pass the fence refuses is `skipped="history_fenced"`, stamps no cadence, and reclaims
  on the next tick. A pass that deletes nothing holds nothing, so ref writes never wait on it.
- **The erasure release holds it before it claims a release**, so a tick a reclaim refuses leaves the release
  unclaimed for a later one.
- **A hold lapses after 15 minutes** (`HOLD`), so a holder that crashed fences its table for at most that long. A
  release finding its own hold lapsed logs `history_fence_outlived` at ERROR.
- **The record is the table's.** It ends with the table's other uuid-keyed records (`object_records`), and its prefix is
  a control prefix, which no drop, purge or discovery walk treats as a table's bytes.

## Consequences

- A tag or branch written while its table is reclaimed answers 409 for as long as the reclaim runs, and a reclaim door
  answers 409 during a ref write. Maintenance passes over a table whose refs are being written are skipped for that tick.
- A reclaim still running when its hold lapses is unguarded from then on. No reclaim the estate runs on one table is
  known to take that long, and the log line says when one does.
- The maintain tier, whose vend reaches the whole table prefix `_refs/` included (ADR 0156 C4), could still run Lance's
  cleanup or write a ref without the fence. Maintenance itself takes it; a maintain-tier job that runs its own cleanup
  is outside this decision.
- The model registry's bless tag takes no fence: nothing reclaims a model table, since the sweep names datasets only
  through `__manifest` (LH-520).
