# 0136. Bulk labeling is a mode of the task, and columns carry recipes (2026-08-09)

Source: `open_bulk_active.md` §5 (repo root; the working spec of 2026-08-09, an analysis of `huggingface/aisheets`
at `cadf5cd`), lines 134-238. The owner rulings it records are dated 2026-08-09.

## Context

The ask was "labeling in excel for bulk labeling, combined with AI endpoints, filters, and embeddings". aisheets gets
that loop right (one sentence becomes a living column that starts filling at once, type inferred, references implicit,
editing a cell is validating it) and the infrastructure wrong; rask already has the substrate it lacks (Lance/Arrow
rows, the producers registry, the guided-generation contract, the jobs seam, OpenFGA).

## Decision

- **Bulk labeling is a special case of labeling, done in bulk** (owner, 2026-08-09). The same labeling task, ontology
  and "what should be done"; only the modality changes, a table over all the session's items instead of a canvas over
  one. Bulk is a **tab of the labeling task** (`/tasks/[id]` → Labeling | Bulk | Task settings | Publish), a mode of
  the task rather than a destination; the `/bulk` route survives for deep links only.
- **Claiming is not bulk's job, and bulk never blocks or collides with normal labeling** (owner clarification, same
  day). The grid works the task's item set as data and takes no claim. The two write planes coexist through the save
  wire's optimistic concurrency: every bulk write states its `base_version`, so colliding with a canvas session is a
  409 and a re-fetch, never a lost edit. No queue chrome in the grid.
- **The grid is a view over the same table the canvas edits, never a second store.** Columns speak the ontology: a
  tag column is constrained to declared classes, an attribute column to its type, a transcription column is free
  text, all derived from `LabelOntology`; filling a cell writes an annotation row or metadata patch through the
  existing save wire.
- **A producer column carries a RECIPE**, and its declaration is derived from the action, not demanded before it: one
  textarea creates the column, auto-named, born `free` and tightened later (`free → enum` retro-validates existing
  cells). A recipe pins its producer by NAME, never URL, from the same Serve-native registry the canvas assist uses
  ([0137](0137-model-endpoints-are-ray-serve-discovered-by-the-labeling.md)).
- **Embedding selection has exactly three modes** (owner): similarity (anchor a row, take its neighbourhood),
  clustering, and lasso on a 2D projection. All three produce one thing, a filtered working set every set-level action
  operates on.

## Consequences

- The annotator zone ships the grid and the recipe model: `frontend/microfrontends/annotator/src/lib/bulk/`
  (`BulkGrid.svelte`, `recipe.ts`), with the `bulk` and `tasks` routes beside it.
- Similarity landed first (2026-08-09) over the estate's one similarity seam; clustering, dedup views and
  multi-anchor exemplar sets were open when the spec was written. Their current state was not re-checked here.
- A preview (at most 5 rows) runs synchronously through the assist plane; a full run is one job per column execution
  on the jobs seam, each cell landing as a `status='prediction'` row with provenance, and accepting in the grid is the
  same status flip the review queue does.
