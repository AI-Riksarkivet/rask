# open_htr_governance — #88: the archive's product enters the lakehouse

The working plan for the active goal's PIECE 2. Delete when the work lands (residue → OPEN-WORK.md).

## The defect, restated from verified ground truth

The HTR pipeline ends at `AltoWriterActor` (`runners/htr/src/htr/actors/io.py:105`) doing
`self.sink.write(key, data)` — raw ALTO XML to plain S3 keys. No Lance table, no catalog
registration, no lineage, no FGA. Meanwhile every rail built this week — protection (#73),
trash/undrop (#75), tiered vending, page-route authz (#90) — governs tables. The transcriptions,
which are what a restricted fond is restricted ABOUT, are outside the boundary.

Three facts checked at HEAD that shape the design:

1. **The cascade registers NOTHING.** No `register_table` call exists anywhere in
   `medallion/services/` — silver/gold datasets are governed by path convention only. So "a gold
   table FGA-gated for reads" requires the writer to REGISTER its output; writing the Lance file is
   not enough.
2. **`medallion/schemas/htr.py` is imported by nothing in production** (its own docstring says so);
   `schemas/tier.py` (2026-07-29) reframed it: tiers carry `{id, payload, stage, lineage,
   source_rowid}`, and `GOLD_CONTRACT_COLUMNS` is HTR's own payload declaration. The goal requires
   production code to import it — the HTR lane's writer is the natural importer.
3. **The ALTO now carries model identity** (#89): one `<Processing>` block per model with
   `model=<repo>@<revision>`. That is the provenance CHANNEL: the consumer reads what the run
   actually loaded, never what a config requested. The commit sha is NOT yet in the ALTO — step 1
   adds it, completing the "from the run" story.

## Ruling: a medallion HTR lane, catalog-registered, fed by Serve

The transcribe compute stays where it is — the deployed Ray Serve `/htrflow` (warm weights,
already pinned to `MODEL_REVISION`). The GOVERNANCE moves: a new medallion lane consumes bronze
page rows, calls `/htrflow` over HTTP per page, parses the returned ALTO into the contract
columns, writes `gold$htr` as a governed tier row (tier columns + HTR payload), **registers the
table in the catalog** (which seeds FGA ownership — reads become gated for free, #90 already
gates the byte paths), and emits lineage whose run facet carries the model identities and sha
**parsed out of the ALTO**.

Why not the alternatives:
- *The runner writes Lance directly* — the runner is sealed (no medallion import, so it cannot
  import the contract), and a second ungoverned writer is the disease, not the cure.
- *A full P7b re-cut (layout/lines/transcribe as separate stage-job movers)* — the right end
  state, but it needs the GPU cluster to verify and re-cuts three actor stages; this lane is the
  P7b **gold half** done properly, and the bronze→silver geometry half can follow it into the
  same seam without redesign.

## Steps

1. **`runners/htr`: the sha joins the ALTO.** `RASK_GIT_SHA` env → `serialize._METADATA`
   (`softwareVersion` becomes `0.1.0+<sha>` or a dedicated Processing step). The dagger build
   stamps it. Small; mirrors #89's revision work. — *runner suite + mutation.*
2. **`medallion/services/htr_parse.py`**: ALTO → `{page_key, page_width, page_height,
   region_polygons, line_polygons, reading_order, text, confidences}` + `models=[repo@revision…]`
   + `commit_sha`, from the `<Processing>` blocks. **Imports `GOLD_CONTRACT_COLUMNS` and asserts
   its output covers the payload columns** — the contract becomes production-load-bearing here.
3. **`medallion/services/htr_transcribe.py`**: page blob → ALTO via `MEDALLION_HTRFLOW_URL`
   (default the Serve route). Tested with respx (the prescribed seam); the real path is the
   deployed Serve.
4. **The lane**: a `movers:` entry (`pages-to-gold-htr`, operation `transcribe_pages`) whose
   compute path uses steps 2+3 instead of `derive_artifacts`; writes tier columns + payload;
   stamps the lineage JSONB in the same commit (R26, existing shape).
5. **Registration**: after the first successful write, `register_table` via the catalog REST with
   the mover's identity — the register door seeds ownership tuples; unregistered-until-written, so
   a failed lane never registers an empty table. This lane DROPS nothing, so #96's
   cascade-trash rule is satisfied vacuously — stated, not assumed.
6. **Lineage model facet**: `build_run_event` grows an optional `model` run facet (models list +
   commit sha) — kwargs-based, additive, byte-identical when absent.
7. **Verification**: unit + integration per step, each mutation-checked; the lane end-to-end
   in-process against a REAL local Serve `/htrflow` with `RASK_SERVE_GPU_FRAC=0` (CPU-schedulable,
   one page — slow is fine); browser proof = the registered `gold$htr` in the lakehouse UI with
   its lineage and grants. If CPU Serve cannot come up on this host, that bound is DECLARED and
   the e2e gate is env-gated like `test_maintenance_e2e.py` — never silently skipped.

## Bounds stated up front

- Bronze→silver geometry movers (the other P7b half) are OUT — same seam, later work.
- The raw ALTO S3 sink stays (export format, #P7c's exporter reads gold later) — this adds the
  governed home, it does not delete the export.
