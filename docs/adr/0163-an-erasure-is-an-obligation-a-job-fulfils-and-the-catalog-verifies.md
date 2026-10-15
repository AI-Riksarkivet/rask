# 0163. An erasure is an obligation a bring-your-own job fulfils and the catalog verifies (2026-10-15)

**Status:** accepted. Source: owner, 2026-10-15, un-parking LH-493 (#637) right after LH-467, and the same day, through
the multi-question tool, on how an erasure reaches a branch and how an index segment is proved. Amends ADR 0162 §6,
ADR 0156 C6 and ADR 0100, and moves when ADR 0138's notice is sent. §5 is amended by ADR 0164: branch commits are
served, so a job fulfils a branch head through them.

## Context

ADR 0162 made compaction, index builds and index optimize bring-your-own compute committed through `version/create`,
and kept one exception: an erasure still deleted, compacted past `COMPACTION_BOUND` and rebuilt every user index
inside the catalog's own pod (`catalog/services/erasure.py`, `index_specs`). That is the column-sized, table-sized work
the owner ruled off the pods that serve every request.

What the catalog must keep is the proof. An erasure that reports success while a branch, a tag, a deletion vector or an
index segment still holds the subject has not erased them (ADR 0100), and only the catalog reads every ref's history
on bytes.

## Decision

1. **The erasure door records an obligation and commits no data.** `POST /management/v1/table/{id}/erasure` (owner
   rung) plans the predicate on main, so one Lance refuses answers 400 with nothing recorded. It disarms every ref
   (`version/create` refuses a commit whose config arms auto-cleanup, so the job could not commit on an armed ref),
   then records the obligation and answers 202. The obligation lives on the control root
   (`_erasure_obligations/<id>.json`) and holds the table, its location, the SHA-256 of the predicate's exact text,
   `retain_days`, each ref the table has with its head version then, the requester and a deadline (chart
   `catalog.erasure.deadlineDays`, default 30, GDPR Art. 12(3)'s month). The predicate itself is never stored
   ([[LH-281]]).
2. **A bring-your-own job rewrites.** Holding `writer` and `maintainer` (no owner rung), on each ref it deletes the
   subject's rows by row id, so the history records ids and never the identifier ([[LH-281]]); compacts every fragment
   that held them with deletions materialised (Lance's own `Compaction.plan` / `commit`); and drops every user index (a
   stock `drop_index` commits a `CreateIndex` that removes it, measured on pylance 13.0.0). Each commit is judged at
   `version/create` like any other (ADR 0157, ADR 0162). It rebuilds its indexes once the obligation is released.
3. **The catalog verifies, and only its own reading releases the obligation.**
   `POST /management/v1/table/{id}/erasure/{obligation_id}/verify` (maintainer rung) takes the predicate and refuses
   one whose digest is not the obligation's: a predicate nothing matches would verify clean. It then:
   - judges every ref's head on bytes, as surfaces the job owes: rows that still answer (`main`, `branch:<name>`),
     rows behind a deletion vector (`compact:<ref>`), and index segments nothing proves free of the subject
     (`index:<ref>`). Every predicate probe reads the data, never a scalar index. Measured on pylance 13.0.0: with a
     BTREE rebuilt after the subject's row was deleted, an index-assisted scan counts 0 rows behind the deletion
     vector and a data scan counts 1, so a job that deleted and rebuilt but never compacted would otherwise verify
     clean;
   - takes no index segment committed after the obligation was recorded as proof. A `CreateIndex` commit's provenance
     (the version it read, the fragments it covers) is written by whoever commits it, so a job could commit a copied
     or crafted segment that still holds the subject's key as freshly built; provenance is read only from commits up
     to each ref's recorded head (owner, 2026-10-15, choosing this over reading each segment's keys);
   - proves each clean head's index segments before anything is reclaimed, because a reclaim deletes the transactions
     that prove them;
   - drops the tags pinning the subject once every head is clean, reclaims each clean ref deepest first with the
     obligation's `retain_days`, and verifies every retained version of every ref (ADR 0100), unchanged.
   The obligation is released when every head is clean and every residual left is held only by the refs `pinned_by`
   names. Its digest is wiped, and the release of those refs is scheduled under the obligation's id and noticed to the
   obligation's requester and the table's owners (ADR 0138), so a second verification meets the first one's record
   rather than scheduling it twice. Otherwise it stays open with every open surface named.
4. **A missed deadline is visible, and nothing acts on it.** `GET /management/v1/erasures` lists obligations to a
   caller holding `can_maintain` on each one's table, with `overdue` set while one is open past its deadline. The
   catalog never does or retries the job's work.
5. **A branch is erased in place, through branch commits, once the spec doors serve them.** `lance_docs` settles the
   shape: the spec puts `branch` on `CreateTableVersion` and `DescribeTable` (`ns_catalog/spec.yaml`), and a branch is a
   shallow clone inside the table keeping its own version history (`lancemultibasebranchingblobv2.md` § Building
   block 3). Today the catalog refuses both with a branch, so an obligation stays open, naming each branch whose head
   holds the subject, until LH-490 (#632) serves branch commits (owner, 2026-10-15: split, branch commits next).

## Considered options

- **The catalog keeps the rewrite** (ADR 0156 C6's named exception): rejected by the owner's 2026-10-15 ruling that
  compute-heavy table work is bring-your-own.
- **Release a branch holding the subject** (ADR 0138's delete-and-recreate at main's head): not Lance-idiomatic per
  `lance_docs` (a branch keeps a complete version history), and it loses every row a branch wrote, not only the
  subject's.
- **Serve branch commits in this row**: measured on pylance 13.0.0 and lancedb 0.40.0, both stock clients open a
  branch with a main `DescribeTable` and stage under `tree/<branch>/` with the table's own write credential. Adding
  the branch staging globs takes that session policy from 1,932 to about 3,100 bytes at the longest names, and MinIO at
  the estate's pinned build refuses anything over 2,048 (`maxSTSSessionPolicySize`, `cmd/sts-handlers.go`). That
  credential shape is LH-490's own measured problem, taken next.
- **Store the predicate for a catalog cron to verify with**: rejected; the identifier would sit on the control root
  ([[LH-281]]). The digest binds the job to the requester's predicate without storing it.
- **Prove a job's index segment by reading its keys**: `lance_docs/file_format.md` documents the BTree
  (`page_data.lance`: `values`, `ids`) and Bitmap (`bitmap_page_lookup.lance`: `keys`) files, and
  `lance.file.LanceFileReader` reads them (measured on pylance 13.0.0), so the catalog could prove each key is a value
  the version still serves and keep scalar indexes serving through the erasure. Not chosen: a reader per index type,
  index-sized reads on a catalog pod, and every other index type (vector, full text, n-gram, zone map, bloom filter)
  would still need dropping.

## Consequences

- An erasure needs the owner's own compute: the requester records the obligation and hands the predicate to a job
  holding `writer` and `maintainer`. A table no job erases is listed overdue once the deadline passes.
- `index_specs` is deleted; the job rebuilds each index as its owner built it, after the release, and re-tuning one is
  the owner's build (ADR 0162). A table's queries run without its user indexes from the job's drop until that rebuild.
- A residual the requester's `retain_days` window keeps leaves the obligation open; a verification after the window
  releases it.
- Since an obligation settles only with every head clean, a branch can no longer be the one keeping a residual: a head
  standing on the residual's files would hold the subject's bytes itself. A settled obligation's release names tags,
  and ADR 0138's branch release stays reachable only from a record that names branches.
