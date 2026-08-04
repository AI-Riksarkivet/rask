# syntax=docker/dockerfile:1.11
# A thin CPU Ray image for the medallion compute seam: official Ray + the lance-ray Data integration.
# Not rask's GPU ray.dockerfile (that's nvidia/cuda + PyTorch for HTR). This is the minimal real Ray runtime
# for a distributed Lance read→transform→write job submitted via `ray job submit` (see make ray-demo).
# Build context = repo root (RA/rask convention):
#   docker build $(BUILD_ARGS) -f .docker/ray-lance.dockerfile -t ray-lance:dev .
FROM rayproject/ray:2.56.1-py312-cpu@sha256:56a97670c40913b7169c2b6e63be2e40c6724fcbb8fb85ce2a4040a4b5238492

# Fully pinned for reproducibility — this trio is version-sensitive (lance_ray's write_lance / index paths
# target specific pylance signatures; see docs/RAY.md). --no-cache-dir keeps the layer lean; the base
# already runs as the non-root `ray` user (UID 1000).
#
# THE PINS MUST MATCH THE FLEET (R27 audit, 2026-07-28). This image reads and writes the SAME blob-v2
# datasets the medallion services write with the root workspace's pylance, so a version split here is a
# correctness bug, not a currency preference. Measured at the previous pins (pylance 8.0.0), against a
# blob-v2 page dataset written by pylance 9.0.0 with one null payload:
#   * ds.scanner(..., blob_handling="all_binary")  -> ArrowInvalid "there were more fields in the schema
#     than provided column indices" on EVERY projection shape — the whole row-aligned blob read path is
#     unusable, so there is no cardinality-preserving way to read the column at all;
#   * the blob DESCRIPTOR's validity is wrong — is_valid() reports [True]*5 for 3 present payloads, so the
#     descriptor-mask fallback silently mis-detects presence too (it is correct at 9.0.0);
#   * lance_ray.create_scalar_index still raises "BTREE distributed indexing uses
#     create_index_uncommitted(..., index_type=…, fragment_ids=…)" even though 8.0.0 HAS both parameters —
#     so ray_lance_job's recorded reason for the native fallback was imprecise, but its conclusion held.
# At these pins all three are clean (verified: 5 rows in / 5 rows out with the nulls None, on every
# projection, plus a blob_array re-wrap round-trip). lance-ray 0.5.0 also brings write_lance's
# `enable_stable_row_ids`, `add_columns_from` / `merge_columns_from` and the reusable Ray Pool, none of
# which existed at 0.4.2.
# pillow: the media stage job derives an inline thumbnail + embedding from image blobs (the SAME Pillow
# primitives as services/medallion/services/media.py, drift-pinned by tests/unit/test_ray_stage_job.py).
# Needed since 2026-07-13, when the Ray stage job gained the blob path (Phase-3 media-on-Ray parity):
# lance_ray's write strips blob-v2 typing (verified live — read_lance turns a blob column into plain
# large_binary), so the job round-trips blobs via pylance and derives here rather than falling back
# in-process. Drop this + the round-trip when lance-ray gains inline-blob-preserving read/write.
RUN pip install --no-cache-dir "lance-ray==0.5.0" "pylance==9.0.0" "pyarrow==25.0.0" "pillow==11.3.0"

# OTel SDK + OTLP/HTTP exporter so the train job (ray_train_job.py) can export its run metrics to
# GreptimeDB (#18 experiment tracking → Perses). Pinned to the services' opentelemetry version for parity.
RUN pip install --no-cache-dir "opentelemetry-sdk==1.43.0" "opentelemetry-exporter-otlp-proto-http==1.43.0"

# Bake the jobs so `ray job submit -- python /home/ray/jobs/<job>.py` needs no working-dir upload.
# EVERY default entrypoint in medallion.core.config must appear here — a job the config names but the
# image does not carry fails at submit with "no such file", which is how the IIIF head's Ray branch was
# dead on arrival (R27 audit, 2026-07-28: an entrypoint setting pointed at a file this COPY
# omitted). Pinned by tests/unit/test_ray_job_images.py.
#   ray_lance_job.py        — the standalone write/index/evolve/compact demo (make ray-demo)
#   ray_stage_job.py        — the per-stage cascade transform a mover submits (MEDALLION_RAY_ENABLED)
#   ray_train_job.py        — the TRAINING job the trainer consumer submits (#115b, docs/RAY-TRAIN.md D2–D4)
COPY scripts/ray_lance_job.py scripts/ray_stage_job.py scripts/ray_train_job.py /home/ray/jobs/
# The DUMMY lane (A11): a sealed runner whose transform is trivial but whose mechanics are real —
# CDF delta read, merge_insert on the stable id, catalog-registered commit. Baked like any other job
# so the end-to-end lane is provable with no GPU and no model download.
COPY scripts/ray_dummy_job.py /home/ray/jobs/

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.title="ray-lance" \
      org.opencontainers.image.description="CPU Ray + lance-ray for the medallion distributed-compute demo" \
      org.opencontainers.image.source="https://github.com/Borg93/lance-ns" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"
