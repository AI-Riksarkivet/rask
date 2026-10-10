# 0110. Serialization is a projection from gold, served by its own service (R4, R25, 2026-07-24)

Source: `docs/architecture/lance-ns-merge.md:26,442,460` (at `44b354f3`) (owner rulings R4, accepted 2026-07-24, and R25, 2026-07-28).

## Context

rask produced a consumer format (ALTO XML) inside its pipeline. In a lakehouse that makes a file format a tier
output and puts format code inside the stage runners.

## Decision

- **R4.** Compute ends at gold Lance. Consumer formats (ALTO 4.4 first) are PROJECTIONS served from gold by a
  separate `exporter` microservice, never produced inside the lakehouse or the stage runners, and never stored.
- **R25 — the consume layer is the goal of the south side.** An external user gets (a) the Lance datasets
  through the governed catalog, (b) lineage alongside them as JSONB
  ([0111](0111-gold-carries-its-lineage-as-a-jsonb-column-r26-2026-07-28.md)), and (c) serialization on
  demand through the exporter. A query engine joins this layer later.

## Consequences

- The exporter is not built: there is no `services/exporter` (`services/` holds annotator, catalog, compute,
  controlplane, flows, gateway, ingest, lineage, maintenance, medallion, notifications, search, viewer).
- The ruling still binds what may not happen: no stage runner writes a consumer format, and no tier carries a
  format-shaped schema (`CLAUDE.md`, "The orchestrator is gone").
- R4 named "the gold schema contract (P7b)" as load-bearing. That per-workload contract
  (`medallion/schemas/htr.py`) was deleted 2026-08-17; governed rows are `{id, payload, stage, lineage,
  source_rowid}` with an opaque payload, and a workload's output shape belongs to its sealed runner.
