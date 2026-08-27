# syntax=docker/dockerfile:1.11
# A thin CPU Ray image for the medallion compute seam: official Ray + the lance-ray Data integration.
# Not rask's GPU ray.dockerfile (that's nvidia/cuda + PyTorch for HTR). This is the minimal real Ray runtime
# for a distributed Lance read→transform→write job submitted via `ray job submit` (see make ray-demo).
# Build context = repo root (RA/rask convention):
#   docker build $(BUILD_ARGS) -f .docker/ray-lance.dockerfile -t ray-lance:dev .
FROM rayproject/ray:2.58.0-py312-cpu@sha256:c3c9573c5c6bfe4127885f79622d6a32064d34cafc7d156ec728aab8657be250

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
# THE FLEET'S OWN STACK, so the baked production jobs can actually run.
#
# `ray_stage_job.py` — the per-stage cascade transform every mover submits — imports
# `service_kit.lakehouse`. This image did not provide it, so the job died on line 65 the moment
# anything submitted it. MEASURED on the k3s estate, driving a real 50k `/produce`:
#
#   ray-silver-e2e-verify-…  FAILED
#   ModuleNotFoundError: No module named 'service_kit'
#
# while the mover logged `medallion_stage_dispatched_to_workflow` and reported a terminal job — a
# dead cascade wearing a dispatched one. Pinned by `test_a_baked_job_gets_every_repo_package_it_imports`.
#
# EXPORTED, NOT SYNCED. `.docker/ray-cluster.dockerfile` builds a `/opt/venv` from the root lock, which
# is right for a `nvidia/cuda` base it owns outright. This base is `rayproject/ray`, which brings its
# OWN interpreter and Ray installation — a second venv beside it would leave the job running under
# whichever python `ray job submit` picked. So the LOCK still decides the versions (`uv export
# --frozen`, same root lock, same resolution as the fleet) and they are installed into Ray's python.
# `--no-emit-workspace` keeps the first-party members out: they are COPYed as sources below, for the
# same reason `dummy_runner` is — no second resolution, and `sys.path[0]` finds them.
#
# BEFORE the explicit pins below, deliberately: if a transitive dep disagrees about pyarrow, the
# pinned line is what must win, and pip applies last-write.
COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:7bff3c3776ec467fc1437960f2c469d8beb30f536a6465a3350c647ccd260ec2 /uv /usr/local/bin/uv
RUN --mount=type=bind,source=uv.lock,target=/tmp/w/uv.lock \
    --mount=type=bind,source=pyproject.toml,target=/tmp/w/pyproject.toml \
    --mount=type=bind,source=packages,target=/tmp/w/packages \
    --mount=type=bind,source=services,target=/tmp/w/services \
    uv export --directory /tmp/w --package service-kit --frozen --no-hashes \
      --no-emit-workspace --no-dev -o /tmp/service-kit-requirements.txt \
 && pip install --no-cache-dir -r /tmp/service-kit-requirements.txt \
 && rm -f /tmp/service-kit-requirements.txt

RUN pip install --no-cache-dir "lance-ray==0.5.0" "pylance==10.0.0" "pyarrow==25.0.0" "pillow==11.3.0"

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
# ...AND ITS PACKAGE, which this image carried the script for but not the code — so the baked job
# died `ModuleNotFoundError: No module named 'dummy_runner'` the moment anything ran it, while the
# `dummy` TransformSpec stayed declared and the door stayed open onto a broken image. Reproduced on
# the deployed head (ray-lance:main-9a7d113f) before this line existed.
#
# `.docker/ray-cluster.dockerfile` has carried the same COPY since A11 and explains the reasoning at
# length; the short version is that `python /home/ray/jobs/ray_dummy_job.py` puts that directory on
# `sys.path[0]`, so `from dummy_runner.job import main` resolves with no PYTHONPATH edit and — the
# part that matters — NO second dependency resolution. `dummy_runner` imports pyarrow and lance and
# nothing else, both already present, so a source copy adds no resolvable dependency and stays
# inside the "a workload's awkward dependencies are ITS problem" ruling.
#
# Baked, never `runtime_env`: Ray documents that as development-only, and a probe that ran
# differently from the lane it probes would be worth nothing.
COPY runners/dummy/src/dummy_runner /home/ray/jobs/dummy_runner
# The two workspace packages the baked PRODUCTION jobs import, beside them for the same reason —
# `service_kit` depends on `storage`, so both must be present or the first import fails on the second.
COPY packages/service-kit/src/service_kit /home/ray/jobs/service_kit
COPY packages/storage/src/storage /home/ray/jobs/storage

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.title="ray-lance" \
      org.opencontainers.image.description="CPU Ray + lance-ray for the medallion distributed-compute demo" \
      org.opencontainers.image.source="https://github.com/Borg93/lance-ns" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"
