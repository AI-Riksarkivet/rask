# 0138. An erasure releases a pinning branch or tag after a seven-day notice (2026-10-10)

Source: owner decision D4, 2026-10-10, taken in a grilling session over LH-178 (#116); the tag clause was ruled the same
day after the lance_docs check, and the descendant clause after the measurements. Every behaviour cited as measured was
measured on pylance 12.0.0 with stable row ids and file version 2.2, by the scripts on branch
`prototype/lance-erasure-and-commit-door` (`prototypes/lance-erasure-and-commit-door/`, one-line results in its README).

## Context

In Lance a branch lives inside the table: it is a shallow clone under the dataset root plus a ref recording its fork
point (`lance_docs/lancemultibasebranchingblobv2.md:357-363`). Keeping branches in-table is what lets cleanup be driven
by branch and tag expiry, with no forgotten copy to break an erasure (`lancemultibasebranchingblobv2.md:302-304`). The
fork point holds the parent's files: "Lance ensures that cleanup does not delete files still referenced by any branch.
Delete unused branches to allow their referenced files to be cleaned up" (`lance_docs/guide.md:4051-4053`). A tag holds
a version the same way: "Tagged versions are exempted from the `LanceDataset.cleanup_old_versions()` process", and a
tagged version is released only by deleting the tag (`guide.md:3989-3992`).

So when a subject is erased from main, a branch whose parent version, or a tag whose version, still holds the subject
keeps it readable: `checkout_version(N)` serves the erased rows, the erasure report says `complete=False`, and nobody is
told (`services/catalog/src/catalog/services/erasure.py`, `_holders` and the `doomed |= held.branches` accounting).

Idea taken from: Lakekeeper's two-step expiry (mark first, reclaim later).

## Decision

- **Notify first.** When erase() finds a branch or a tag pinning a version that holds the subject, it emits a targeted
  control event. The event carries a new `ControlAction` and `NotificationReason` and goes to the requester and the
  table's owners. It names the branch or tag and the deadline.
- **Then release, always.** When the grace period ends, a pinning branch is deleted and recreated at main's
  post-erasure head, and a pinning tag is deleted.
- **Descendants go with it.** Lance refuses to delete a branch another branch was forked from (`Branch A is referenced
  by [("B", 2)] versions, can not delete`), and cleaning the child's history does not lift that (`prototypes/lance-erasure-and-commit-door/a1_child_branches.py`).
  So the notice names the pinning branch and every descendant found through their `parent_branch` refs, and at the
  deadline the subtree is deleted leaf-first and recreated top-down: the pinning branch from main's clean head, each
  child from its recreated parent (`prototypes/lance-erasure-and-commit-door/a1b_child_branch_release.py`). The descendants' own commits are lost; their
  owners have the same grace period to copy work out (owner, 2026-10-10). A branch has no update operation, so delete-and-recreate is its only
  release (`lance_docs/ns_catalog/namespace/operations/index.md:85-92`; CreateTableBranch takes `fromBranch` /
  `fromVersion`, `ns_catalog/namespace/operations/models/CreateTableBranchRequest.md:13-15`). The `reference=` keyword
  of `create_branch` is the installed pylance 12.0.0 signature; `guide.md:4031-4032` shows the call positionally. A tag
  could instead be moved to the clean head (UpdateTableTag, `ns_catalog/namespace/operations/models/UpdateTableTagRequest.md:13-14`),
  which keeps its name; the owner chose deletion. The owner may copy work out before the deadline; a branch loses its
  fork point and a tag its name.
- **The grace period is 7 days by default**, set as a catalog chart value. The erasure report and the notification
  both show the deadline. GDPR expects erasure without undue delay and within a month, and a week leaves margin.
- **No override.** There is no door that cancels or extends the release: an erasure that can be blocked is not an
  erasure. Deletion protection does not guard tag or branch deletes (ADR 0150), so a protected table is released too.
- **Scope.** This covers the inherited fork point and tagged versions. A branch's own history is LH-263, and copying
  inherited fragments into the branch stays bounded by the 2026-09-26 ruling (64 MiB of source per branch per erasure).

## Consequences

- The erased bytes leave storage only after all of these, and the erasure report states that bound, not only the
  grace period:
  - the grace period ends and every pin (branch subtree, tag) is released;
  - compaction rewrites the fragments holding the subject, since a delete is a deletion file over an unchanged data
    file until "deletions can be materialized by rewriting data files" (`lance_docs/file_format.md:2984`). Compaction
    also rewrites managed blob-v2 sidecars (inline, packed and dedicated), so their old `.blob` files go at the next
    cleanup (`prototypes/lance-erasure-and-commit-door/a4_blob_v2.py`, `a4b_blob_dedicated.py`);
  - every index over an erased column is rebuilt (`create_*_index(..., replace=True)`) or dropped, because compaction
    does not rewrite index files on a stable-row-id table: after compaction and cleanup the BTree and FTS files keep the
    subject, and `optimize_indices()` commits nothing (`prototypes/lance-erasure-and-commit-door/a2_index_files.py`). Index files are immutable
    (`file_format.md:1091-1093`) and only an index built after the delete leaves the row out (`1103-1106`,
    `2984-2985`). A rebuild works without compaction, and cleanup alone removes a dropped index's files. A fragment reuse
    index never arises here: `compact_files(defer_index_remap=True)` is refused under stable row ids;
  - a later version exists and cleanup's `older_than` window passes. The delete predicate, which names the subject, is
    written to the delete version's `.txn` and to its manifest, so it leaves only when that version is superseded and
    cleaned (`file_format.md:4783-4789`; the manifest copy is measured, `prototypes/lance-erasure-and-commit-door/a3_txn_files.py`). rask's window is
    `maintenance.olderThanDays` (`chart/values.yaml:1800`, default 7), which a table's policy overrides with
    `retention_days` (`services/maintenance/src/maintenance/services/sweep.py:554-555`); pylance's own default is 14 days
    (`lance_sdk.md:925-931`). Cleanup on main never touches a branch's own `tree/<branch>/` history (LH-263).
- Until cleanup runs, restoring a retained pre-erasure version brings the rows back, even after compaction
  (`file_format.md:5358-5373`; `prototypes/lance-erasure-and-commit-door/a5_restore.py`), so restore stays a high-privilege door.
- A tag can sit on a non-main branch (its ref at the dataset root carries `branch`), so the pin census reads the tag's
  branch as well as its version (`file_format.md:2794-2822`; `prototypes/lance-erasure-and-commit-door/a6_tag_on_branch.py`). Cleanup on that branch raises
  when `error_if_tagged_old_versions=True`: the sweep passes False (`services/catalog/src/catalog/services/maintenance.py:394`)
  and the targeted path passes True (`:454`), so the erasure's own cleanup must pass False or release the tag first.
- Holders outside the branch and tag model: external-URI blobs are never reclaimed, inside a registered base or outside
  one, which contradicts the digest's claim that cleanup collects them (`lancemultibasebranchingblobv2.md:794-805`;
  `prototypes/lance-erasure-and-commit-door/a4_blob_v2.py`), so an erasure over an external blob column must delete the referenced object itself or report it
  as a residual; and a shallow clone shares its source's files and becomes unreadable once the source cleans up
  (`lance_sdk.md:3680-3689`; `prototypes/lance-erasure-and-commit-door/a7_shallow_clone.py`), so the erasure must account for every clone of the table.
- `pinned_by` must stop over-reporting a branch whose own head is already a residual head, because the notice now
  goes to a person rather than only into a report.
- The pending release must survive a restart, so it is durable state with a due time, not an in-memory timer.
- XC-092's erasure end state (criterion 2) can now be specified, so XC-090 is no longer blocked on D4.
