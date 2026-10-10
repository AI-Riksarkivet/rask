# 0120. One compute-lineage layer, `lineage-kit` (R21, 2026-07-27)

Source: `docs/architecture/lance-ns-merge.md:456` (at `44b354f3`) (owner ruling R21, decided and landed 2026-07-27).

## Context

Ray work in the estate (medallion stage runners, the ingest head, workload pipelines, Ray Lance jobs and
online Ray Serve deployments) would each otherwise invent its own OpenLineage emission shape, and per-actor
lineage would not be traceable in AGE.

## Decision

One compute-lineage layer makes ALL Ray work emit OpenLineage consistently: a shared library wrapping
`openlineage-python` with Pydantic schemas for events and facets, giving Ray Data stages and Ray actors an
inheritable/decoratable emission seam (job run → stage → actor as parent/child runs). It is
`packages/lineage-kit`, a SIBLING of the Ray dataset library rather than part of it, because that library's
pylance/ray/lancedb stack would poison a sealed runner's lock, while openlineage-python's transitive set is
light. The spec stays 2-0-2 byte-parity with the governed emitter, pinned by test, and context carries into a
subprocess via env and via constructor argument.

## Consequences

- `packages/lineage-kit` ships `stage.py` and `actor.py` (`LineageActorMixin`,
  `packages/lineage-kit/src/lineage_kit/actor.py:33`), plus `consume.py`, the JSONB document of
  [0111](0111-gold-carries-its-lineage-as-a-jsonb-column-r26-2026-07-28.md), and `signing.py`, the Ed25519
  seam for lineage events.
- Per-stage and per-actor child runs inside the Ray jobs were not yet emitted at the R27 audit
  ([0122](0122-the-ray-plane-gets-a-standing-audit-r27-2026-07-28.md), item 6); no caller of
  `LineageActorMixin` outside `lineage-kit` was found in `runners/`, `scripts/` or `services/`.
