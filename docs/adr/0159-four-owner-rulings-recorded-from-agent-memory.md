# 0159. Four owner rulings of 2026-09-25/26 that no ADR recorded: branch writes, the principal key, catalog drop events, an unplannable compaction (2026-10-12)

**Status:** accepted. Source: owner rulings given 2026-09-25 and 2026-09-26 through the multi-question tool, until now
held only in an agent's memory. Recorded here so a session on another machine has them. Machine identity (D1) is
already ADR 0083 and ADR 0156 C5; Kueue being someone else's (D11) is ADR 0153.

## Decision

- **D3, branch writes.** A table writer (`can_write_data`) writes every branch of that table; creating or deleting a
  branch, writing tags and restoring stay owner-tier. Reason: in `lance_docs/ns_catalog/spec.yaml` a branch is a
  parameter of a table operation, not an object with its own creator, so it carries no authorization of its own.
- **D5, the principal key.** A human principal's FGA key is `<idp-id>~<claim>`, percent-encoded for FGA, never the
  identity provider's raw `sub`. A service principal's key is `kubernetes~system:serviceaccount:<ns>:<sa>` (ADR 0156
  C5). Built by LH-063 #125.
- **Catalog drop events at the lineage gate.** A drop-lifecycle `DatasetEvent` whose signature verifies as the
  catalog's own service identity is admitted without re-deriving its author's grants: the catalog enforced `can_drop`
  at commit and revokes the table's tuples right after, so a re-check would refuse every drop. Every other event keeps
  the full check.
- **An unplannable compaction.** When the compaction plan door answers 404 (spec codes 1 and 4), the dataset is parked
  for that tick and counted per table, and an alert fires only when the same table stays unplannable across ticks.
  This is a default: the implementing row checks it against `lance_docs` and its callers first.

## Consequences

- The decisions travel with the repository; agent memory keeps only working habits.
