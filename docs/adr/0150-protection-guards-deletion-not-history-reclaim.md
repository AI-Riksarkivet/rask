# 0150. Protection guards deletion, not history reclaim (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (LH-300 #311, second answer); the tag and branch clause was ruled
the same day after the lance_docs check.

## Context

Lance separates retention from cleanup. A version that must survive is tagged: "Tagged versions are exempt from
cleanup" (`lance_docs/guide.md:3821`, `3989-3992`), and branches keep their referenced files the same way
(`guide.md:4051-4053`). Lance's own advice for a rollback window is to "create a tag … delay cleanup"
(`guide.md:770-777`). Deleting a tag or a branch is therefore what makes its versions reclaimable (`guide.md:3992`,
`4053`): it is history reclaim, not entity deletion.

`version/delete` checks protection (`services/catalog/src/catalog/api/v1/endpoints/versions.py:245-246`), and so do tag
and branch delete (`tags.py:153`, `branches.py:160`). `maintenance/run`
(`services/catalog/src/catalog/api/v1/endpoints/maintenance.py:124-156`) and the scheduled sweep do not read protection
records.

## Decision

- Deletion protection guards dropping a table, namespace or warehouse. It does not guard history reclaim.
- The protection check leaves `version/delete`, tag delete (`tags.py:153`) and branch delete (`branches.py:160`).
  Authorization alone governs those deletes.
- A version that must be retained is retained the Lance way, with a tag.

## Consequences

- Protected tables are compacted and cleaned on the normal cadence, and they do not grow without bound.
- An erasure can release a tag or branch pinning erased data on a protected table (ADR 0138).
