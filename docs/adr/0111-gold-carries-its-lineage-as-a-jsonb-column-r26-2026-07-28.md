# 0111. Gold carries its lineage as a JSONB column (R26, 2026-07-28)

Source: `docs/architecture/lance-ns-merge.md:461` (at `44b354f3`) (owner ruling R26, 2026-07-28; executes R25's (b)).

## Context

The consume layer ([0110](0110-serialization-is-a-projection-from-gold-served-by-its-own.md)) hands an
external user a Lance dataset. Provenance held only in the AGE graph does not travel with that dataset.

## Decision

Every governed tier row carries a `lineage` column of Lance's `pa.json_()` type (stored JSONB), queryable in
place with `json_extract` / `json_get_*` and indexable with a JSON scalar index. The document is a PROJECTION
of the emitted OpenLineage `RunEvent` (`packages/lineage-kit/src/lineage_kit/consume.py`: `LineageDoc`, `LineageEdge`,
`DatasetRef`), so there is one provenance shape. It is stamped in the SAME Lance commit as the data, an
`IndexConfig(index_type="json", target_index_type="btree", path="run_id")` index is built over it, and the
`derived_from` chain is inherited from the upstream dataset's own cell, so gold reaches bronze with no graph
query.

## Consequences

- Live-proven on kind at the ruling: gold v3, 8 rows, `json_get_string` / `json_get` / `json_array_contains` /
  `json_array_length` filters all returned, and the chain matched AGE's `READ`/`WROTE` attribution for the same
  two run ids.
- The shared stage write carries the document on both engines: the work order's `lineage_json` is written as
  the `lineage` JSONB column in the same commit as the data
  (`services/medallion/src/medallion/services/stage_submit.py:135-137`; see
  [0106](0106-the-stage-write-is-one-module-both-engines-land-through-cp.md)).
- `GOLD_CONTRACT_COLUMNS` and `schemas/htr.py`, which the ruling's execution named, were deleted 2026-08-17;
  `lineage` is part of the opaque tier row, not of a workload contract.
