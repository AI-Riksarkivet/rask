# 0161. The tag gate runs when a version is published, so a hold is the publish door's answer (2026-10-15)

**Status:** accepted. Source: owner, 2026-10-15, choosing between two designs through the multi-question tool while
taking LH-466 (#586). Refines ADR 0156 C1 and ADR 0160.

## Context

ADR 0156 C1 makes `published` the version readers of a governed tier read, and the spec over it (#593) has the tier
judge answer "hold". Two designs fit that: gate every committed write into a tier and publish what passes, or gate a
version when a writer publishes it, as the publish door has done since the owner's D-R1 ruling (2026-08-04: a commit
is not a publication).

## Decision

- The project's quality gate runs when a version is published (`POST /management/v1/table/{id}/publish`), once per
  publication. A pass moves `published`. With review declared on the project's gate, a breach is held: `published`
  stays, and a `can_promote` holder approves it (`published` moves to it, the spec's `UpdateTableTag`) or rejects it
  (main is restored to the published version, the spec's `RestoreTable`). A hold nobody answers within the project's
  `review_hours` expires on the catalog's cron and publishes nothing.
- The tier judge answers allow or refuse before a write. It does not hold.
- Only the publish door and an approval move `published`; the spec's tag doors refuse the catalog's own tags.

## Considered options

- Gate every commit and publish what passes: rejected. Each commit of a multi-commit job would reach `@published` as
  soon as it passed, the gate's scan would run on every commit, and it reverses D-R1.

## Consequences

- A pipeline on a stock Lance client makes one rask call per publication, the publish door.

## Amendment (2026-11-12): a rejection never discards a later commit

Source: owner, 2026-11-12, "Refuse, hold stays", through the multi-question tool while taking LH-576 (#798).

A rejection restores main to the published version as a new commit, and a restore planned on main's head would
discard every commit a writer made after the held version. So while main holds a commit after the held version,
`POST /management/v1/table/{id}/promotion/reject` answers 409 (code 14, `ConcurrentModificationError`) naming main's
head, and the hold stays open: a `can_promote` holder approves it, someone restores the table explicitly
(`RestoreTable`), or a later rejection is made. The rejection's restore commits only as the version right after the
held one, so a commit landing between the door's decision and its restore refuses it the same way and the hold is put
back to open. A rejection asked again after its restore landed finds that restore, or a restore of the published
version at main's head, and records it rather than restoring a second time.
