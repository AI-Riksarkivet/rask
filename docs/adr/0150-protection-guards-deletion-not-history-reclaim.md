# 0150. Protection guards deletion, not history reclaim (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (LH-300 #311, second answer). Reference: Lakekeeper `docs/docs/concepts.md:201-224`.

## Context

`version/delete` checks protection (`services/catalog/src/catalog/api/v1/endpoints/versions.py:245-246`). `maintenance/run`
(`services/catalog/src/catalog/api/v1/endpoints/maintenance.py:124-156`) and the scheduled sweep do not read protection records. In Lakekeeper, protection blocks
entity deletion only, and its commit path, which includes snapshot expiry, has no protection check.

## Decision

- Deletion protection guards dropping a table, namespace or warehouse. It does not guard routine history reclaim.
- The protection check leaves `version/delete`. Whether `tags.py:153` and `branches.py:160` follow is settled in
  LH-300's implementation, against this rule.

## Consequences

- Protected tables are compacted and cleaned on the normal cadence, and they do not grow without bound.
