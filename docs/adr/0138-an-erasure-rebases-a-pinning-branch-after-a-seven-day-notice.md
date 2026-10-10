# 0138. An erasure releases a pinning branch or tag after a seven-day notice (2026-10-10)

Source: owner decision D4, 2026-10-10, taken in a grilling session over LH-178 (#116); the tag clause was ruled the same
day after the lance_docs check. Mechanism measured on pylance 12.0.0 and recorded in LH-178's *How*.

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
  post-erasure head (`create_branch(name, reference=<clean head>)`, `guide.md:4031-4032`), and a pinning tag is deleted.
  These are the format's only ways to make the pinned version reclaimable. The owner may copy work out before the
  deadline; a branch loses its fork point and a tag its name.
- **The grace period is 7 days by default**, set as a catalog chart value. The erasure report and the notification
  both show the deadline. GDPR expects erasure without undue delay and within a month, and a week leaves margin.
- **No override.** There is no door that cancels or extends the release: an erasure that can be blocked is not an
  erasure. Deletion protection does not guard tag or branch deletes (ADR 0150), so a protected table is released too.
- **Scope.** This covers the inherited fork point and tagged versions. A branch's own history is LH-263, and copying
  inherited fragments into the branch stays bounded by the 2026-09-26 ruling (64 MiB of source per branch per erasure).

## Consequences

- The erased bytes leave storage only after three things, in order: the grace period ends and the pin is released; the
  fragments holding the subject are rewritten by compaction, since a delete is a deletion file over an unchanged data
  file until "deletions can be materialized by rewriting data files" (`lance_docs/file_format.md:2984`); and cleanup's
  `older_than` window passes (7 days by default, `guide.md:3810`). The erasure report states that bound, not only the
  grace period.
- `pinned_by` must stop over-reporting a branch whose own head is already a residual head, because the notice now
  goes to a person rather than only into a report.
- The pending release must survive a restart, so it is durable state with a due time, not an in-memory timer.
- XC-092's erasure end state (criterion 2) can now be specified, so XC-090 is no longer blocked on D4.
- Unconfirmed: how Lance behaves when the pinning branch has child branches (`parent_branch` set to it,
  `lancemultibasebranchingblobv2.md:480-487`). lance_docs does not say; LH-178 measures it before relying on the
  delete-and-recreate step for such a branch.
