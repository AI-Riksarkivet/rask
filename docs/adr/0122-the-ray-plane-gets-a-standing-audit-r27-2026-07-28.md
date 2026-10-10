# 0122. The Ray plane gets a standing audit (R27, 2026-07-28)

Source: `docs/architecture/lance-ns-merge.md:462` (at `44b354f3`) (owner ruling R27, 2026-07-28) and its execution record at `:471-556`.

## Context

Ray and lance-ray calls compile whether or not they use the API correctly; a null-dropping blob read, a stale
pin or a missing baked job script is invisible until a cascade runs on data that exposes it.

## Decision

Every Ray / lance-ray usage is reviewed against the lance-ray API surface and the Lance data-evolution, blob
and JSON docs: `read_lance`/`write_lance` options (blob handling, `with_metadata`, storage/base-store params),
the distributed alternative where one exists, `add_columns`/`add_columns_from`/`merge_columns_from` for
backfill instead of dataset rewrites, distributed index building, Ray Pool reuse, and the stage/actor seam
carrying lineage per R21. Findings are fixed or recorded, never assumed correct because a call compiles. Each
verdict names a measurement against the INSTALLED libraries and the versions the Ray image pins.

## Consequences

**Headline finding (2026-07-28, `lance_ray 0.5.0`, `pylance 9.0.0`).** The lance Ray plane had not moved onto
the one cluster ([0109](0109-one-ray-cluster-on-the-latest-release-r3-2026-07-24.md)), and its old pins broke
blob v2: at `pylance 8.0.0` a blob-v2 column written by 9.0.0 with one null payload is unreadable row-aligned
(`blob_handling="all_binary"` raises `ArrowInvalid`, and the descriptor's `is_valid()` returns all-`True`).
Positional pairing of a null-dropping read was fixed at every site by one aligned scan,
`service_kit.lakehouse.blobs.read_aligned_table`
(`packages/service-kit/src/service_kit/lakehouse/blobs.py:147`). The audit's image guard pinned every settings
submit-entrypoint as baked into the Ray image; today `tests/unit/test_the_ray_image_can_import_the_jobs_it_bakes.py`
covers the baked jobs and `tests/unit/test_ray_job_images.py` holds only a chart-declaration parse test (what
the former checks was not read).

**Recorded, not fixed, with current state:**

1. **Distributed compaction.** `lance_ray.compact_files` distributes what the sweep does in-process. Still
   open: `services/maintenance` (successor of `services/compaction`) runs `compact_files()` in-process
   (`services/maintenance/src/maintenance/service.py:7`) and no `scripts/ray_compact_job.py` exists.
2. **Distributing the tabular cascade head.** Blocked on cluster verification, not API.
3. **Ray Pool reuse (`init_global_pool`).** No action until a driver runs repeated distributed searches.
4. **`add_columns_from` / `merge_columns_from`.** Available at the bumped pins; no site needs a cross-dataset
   column merge.
5. **Bronze blob placement.** Wanted an owner ruling and a values knob. The ingest service now pins
   `dedicated_size_threshold` on its blob field (`services/ingest/src/ingest/runtime.py:170-174`); the
   medallion head still writes a bare `blob_field("payload")`
   (`services/medallion/src/medallion/services/ingest.py:58`).
6. **Per-stage / per-actor lineage inside Ray jobs (R21).** See
   [0120](0120-one-compute-lineage-layer-lineage-kit-r21-2026-07-27.md).

The P5 fold itself stays open: `deploy/ray-lance-demo.yaml` and `.docker/ray-lance.dockerfile` still exist.
The audit's `tests/unit/test_blob_null_alignment.py` guard is no longer in the tree.
