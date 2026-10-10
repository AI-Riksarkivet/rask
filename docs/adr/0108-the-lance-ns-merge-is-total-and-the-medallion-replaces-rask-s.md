# 0108. The lance-ns merge is total, and the medallion replaces rask's orchestration (R1, R2, 2026-07-24)

Source: `docs/architecture/lance-ns-merge.md:22-26,439-440` (at `44b354f3`) (owner rulings R1 and R2, accepted 2026-07-24).

## Context

The lance-ns repository carried the Lance lakehouse (catalog, lineage, medallion, the media plane and its
frontend zones); rask carried an S3-sync orchestrator, a batches table and a Ray pipeline. The merge plan
first scoped the copy narrowly and left rask's orchestration alone.

## Decision

- **R1 — total merge.** Everything in lance-ns moves into rask, the media plane included.
- **R2 — compute-plane convergence is in scope** as phase P7, sequenced coexistence-first: P1-P6 land with
  green gates and rask's orchestrator untouched; P7 then replaces the S3-sync orchestration entirely. No
  reconcile loop, no prefetch lane and no batches table survive it. Batch IO is Lance-only; rask's pipeline
  becomes jobs the medallion cascade triggers.

## Consequences

- The orchestrator, the batches table, Alembic and the app database are gone (P7a); the only relational
  stores left are the chart-managed lineage (AGE) and OpenFGA databases (`CLAUDE.md`, Architecture).
- A workload reaches the platform as a sealed runner triggered by the cascade, never as a second
  orchestration plane.
