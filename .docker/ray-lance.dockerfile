# syntax=docker/dockerfile:1.11
# A thin CPU Ray image for the medallion compute seam: official Ray + the lance-ray Data integration.
# Not rask's GPU ray.dockerfile (that's nvidia/cuda + PyTorch for HTR). This is the minimal real Ray runtime
# for a distributed Lance read→transform→write job submitted via `ray job submit` (see make ray-demo).
# Build context = repo root (RA/rask convention):
#   docker build $(BUILD_ARGS) -f .docker/ray-lance.dockerfile -t ray-lance:dev .
FROM rayproject/ray:2.56.0-py312-cpu@sha256:2951c07de396a8b746f9c678b52c6e2282e614e00f80e6846a9ccd12945ae6b0

# Fully pinned for reproducibility — this trio is version-sensitive (lance_ray's write_lance / index paths
# target specific pylance signatures; see docs/RAY.md). --no-cache-dir keeps the layer lean; the base
# already runs as the non-root `ray` user (UID 1000).
# pillow: the media stage job derives an inline thumbnail + embedding from image blobs (the SAME Pillow
# primitives as services/medallion/services/media.py, drift-pinned by tests/unit/test_ray_stage_job.py).
# Needed since 2026-07-13, when the Ray stage job gained the blob path (Phase-3 media-on-Ray parity):
# lance-ray 0.4.2 strips blob-v2 typing on read (verified live — read_lance turns a blob column into plain
# large_binary), so the job round-trips blobs via pylance and derives here rather than falling back
# in-process. Drop this + the round-trip when lance-ray gains inline-blob-preserving read/write.
RUN pip install --no-cache-dir "lance-ray==0.4.2" "pylance==8.0.0" "pyarrow==19.0.1" "pillow==11.3.0"

# OTel SDK + OTLP/HTTP exporter so the train job (ray_train_job.py) can export its run metrics to
# GreptimeDB (#18 experiment tracking → Perses). Pinned to the services' opentelemetry version for parity.
RUN pip install --no-cache-dir "opentelemetry-sdk==1.43.0" "opentelemetry-exporter-otlp-proto-http==1.43.0"

# Bake the jobs so `ray job submit -- python /home/ray/jobs/<job>.py` needs no working-dir upload:
#   ray_lance_job.py  — the standalone write/index/evolve/compact demo (make ray-demo)
#   ray_stage_job.py  — the per-stage cascade transform a mover submits (MEDALLION_RAY_ENABLED)
#   ray_train_job.py  — the TRAINING job the trainer consumer submits (#115b, docs/RAY-TRAIN.md D2–D4)
COPY scripts/ray_lance_job.py scripts/ray_stage_job.py scripts/ray_train_job.py /home/ray/jobs/

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.title="ray-lance" \
      org.opencontainers.image.description="CPU Ray + lance-ray for the medallion distributed-compute demo" \
      org.opencontainers.image.source="https://github.com/Borg93/lance-ns" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"
