# 0138. An erasure rebases a pinning branch after a seven-day notice (2026-10-10)

Source: owner decision D4, 2026-10-10, taken in a grilling session over LH-178 (#116). Mechanism measured on pylance
12.0.0 and recorded in LH-178's *How*.

## Context

A Lance branch is a shallow clone under `tree/<name>/`. Its `_refs/branches/<name>.json` records the `parentVersion`
it forked from, and `cleanup_old_versions` keeps every file a branch references (`lance_docs/file_format.md:2719-2763`).
When a subject is erased from main, a branch whose `parentVersion` still holds the subject pins that version:
`checkout_version(N)` keeps serving the erased rows, the erasure report says `complete=False`, and nobody is told
(`services/catalog/src/catalog/services/erasure.py`, `_holders` and the `doomed |= held.branches` accounting).
Lakekeeper has no erasure. Its expiry/purge split (`docs/audits/2026-09-25/lakekeeper-deep-read/governance.md` §1),
which marks something first and reclaims it later, is the model for the grace step.

## Decision

- **Notify first.** When erase() finds a branch pinning a version that holds the subject, it emits a targeted control
  event. The event carries a new `ControlAction` and `NotificationReason` and goes to the requester and the table's
  owners. It names the branch and the deadline.
- **Then rebase, always.** When the grace period ends, the branch is deleted and recreated at main's post-erasure head
  (`create_branch(name, reference=<clean head>)`), and `cleanup_old_versions` reclaims the pinned version. The branch
  loses its fork point. The owner may copy work out before the deadline.
- **The grace period is 7 days by default**, set as a catalog chart value. The erasure report and the notification
  both show the deadline. GDPR expects erasure without undue delay and within a month, and a week leaves margin.
- **No override.** There is no door that cancels or extends the rebase: an erasure that can be blocked is not an
  erasure, and an override would add an authorization path for nothing.
- **Scope.** This covers the branch's inherited fork point only. The branch's own history is LH-263, and copying
  inherited fragments into the branch stays bounded by the 2026-09-26 ruling (64 MiB of source per branch per erasure).

## Consequences

- An erasure always completes, at most one grace period after it is requested.
- `pinned_by` must stop over-reporting a branch whose own head is already a residual head, because the notice now
  goes to a person rather than only into a report.
- The pending rebase must survive a restart, so it is durable state with a due time, not an in-memory timer.
- XC-092's erasure end state (criterion 2) can now be specified, so XC-090 is no longer blocked on D4.
