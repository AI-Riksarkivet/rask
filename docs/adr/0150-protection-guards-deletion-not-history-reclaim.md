# 0150. Protection guards deletion, not history reclaim (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (LH-300 #311, second answer); the tag and branch clause was ruled
the same day after the lance_docs check.

## Context

Lance separates retention from cleanup. A version that must survive is tagged: "Tagged versions are exempt from
cleanup" (`lance_docs/lance/guide/read_and_write.md:561`, `lance_docs/lance/guide/tags_and_branches.md:61-67`), and branches keep their referenced files the same way
(`tags_and_branches.md:129-133`). Lance's own advice for a rollback window is to "create a tag … delay cleanup"
(`lance_docs/lance/guide/data_evolution.md:170-179`). Deleting a tag or a branch is therefore what makes its versions reclaimable (`tags_and_branches.md:66-67`,
`:133`): it is history reclaim, not entity deletion.

At the decision `version/delete`, tag delete and branch delete checked protection (`fga_deps.require_table_unprotected`,
with `force` turning the lock), while `maintenance/run` and the scheduled sweep read no protection record. LH-300
(e954c32b1, helm rev 48) removed the check and the `force` parameter from the three doors.

## Decision

- Deletion protection guards dropping a table, namespace or warehouse. It does not guard history reclaim.
- The protection check leaves `version/delete`, tag delete and branch delete.
  Authorization alone governs those deletes.
- A version that must be retained is retained the Lance way, with a tag.

## Consequences

- Protected tables are compacted and cleaned on the normal cadence, and they do not grow without bound.
- An erasure can release a tag or branch pinning erased data on a protected table (ADR 0138).
- Pinned by `tests/integration/test_protection_guards_deletion_not_history_reclaim.py`: a protected table serves each of
  the three doors without `force`.
