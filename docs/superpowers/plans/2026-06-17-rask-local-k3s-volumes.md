# rask local k3s + generalize beyond IIIF — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `rask` ingest any image volume (an S3 prefix of images) instead of only Riksarkivet IIIF batches, and run HTR through a single `htrflow` endpoint — so the app can later run self-contained on a low-resource local k3s cluster.

**Architecture:** A "volume" is an S3 prefix under the input bucket. A new `register_volume` service indexes that prefix into the existing `batches` table (`page_count` from the listing, `manifest_status=OK`, one chunk). A global `RASK_SOURCE_MODE=s3` switch makes `build_entrypoint` emit the runner's existing `--input/--prefix` form (`S3Source`) instead of the IIIF `--batch/--cache-bucket/--iiif-url` form. With those two seams, the existing orchestrator → submit → htrflow → ALTO pipeline runs unchanged, IIIF-free.

**Tech Stack:** Python 3.13, uv workspace, FastAPI, SQLModel + SQLAlchemy async (SQLite dev / Postgres prod), Ray Data + Ray Serve, `storage` package (S3Source over boto3), pytest (importlib mode) + `moto[s3]`, Helm.

## Global Constraints

- **Run from the worktree root** `/home/morgan/rask/.claude/worktrees/local-k3s-volumes` (branch `worktree-local-k3s-volumes`, based on `origin/main`).
- **JS/TS = Bun, Python = uv (3.13).** Never `npm`/`pip`/`poetry`. Type-check via `uvx ty check`; lint/format via `uv run ruff`.
- **Ray/uv gotcha:** any `uv run` that imports `core`/`ray` needs `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` and `--no-sync`. App imports need `RASK_VIEWER_INPUT`/`RASK_VIEWER_OUTPUT` set (Settings validates at import).
- **Never run pytest with `-o addopts=""`** — it drops `--import-mode=importlib` and breaks collection. Quiet runs: append `-p no:cacheprovider --no-header -q`.
- **Ruff line length is 160.** Tests are `ANN`-exempt.
- **Commits:** exact message given, no `Co-Authored-By`, no Claude/AI mention, **do not push**.
- **Baseline before starting:** from the worktree, `uv sync --all-packages`, then
  `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest components/services/core/tests packages/storage/tests -m "not slow" -p no:cacheprovider --no-header -q` is green. Record the pass count.
- **Back-compat invariant:** the IIIF `build_entrypoint` output must stay **byte-identical** — `components/services/core/tests/test_pipelines.py::test_build_entrypoint_is_byte_identical` must keep passing untouched.

---

## Task 1: `RASK_SOURCE_MODE=s3` — submission emits the non-IIIF runner invocation

Add `source_mode` + `input_uri` to `RunnerParams` (defaulting to the IIIF behaviour so existing golden-string tests are byte-identical), wire them from `Settings`, and branch `build_entrypoint`.

**Files:**
- Modify: `packages/service-kit/src/service_kit/config.py` (`RunnerParams`, `Settings.source_mode`, `Settings.runner_params`)
- Modify: `components/services/core/src/core/services/submission.py` (`build_entrypoint`)
- Test: `components/services/core/tests/test_pipelines.py` (add s3-mode cases)

**Interfaces:**
- Produces: `RunnerParams(repo_root, cache_bucket, output, iiif_url, source_mode="iiif", input_uri="")` (two new fields, both defaulted). `Settings.source_mode: Literal["iiif","s3"]`. `build_entrypoint(batch_ids, *, params, spec) -> str` unchanged signature; s3 branch active when `params.source_mode == "s3"` and `spec.entrypoint_kind == "runner"`.

- [ ] **Step 1: Write the failing tests** — append to `components/services/core/tests/test_pipelines.py`:

```python
def test_build_entrypoint_s3_mode_uses_input_prefix() -> None:
    """s3 source_mode emits --input/--prefix (S3Source) and omits --batch/--cache-bucket/--iiif-url."""
    params = RunnerParams(
        repo_root=Path("/repo"),
        cache_bucket="images-batch",
        output="s3://images-batch-alto",
        iiif_url="https://iiifintern-ai.ra.se",
        source_mode="s3",
        input_uri="s3://images-batch",
    )
    out = build_entrypoint(["VOL_A"], params=params, spec=PIPELINE_SPECS["htrflow"])
    assert out == (
        "uv run --project projects/runner runner \\\n"
        "  --input s3://images-batch \\\n"
        "  --output s3://images-batch-alto \\\n"
        "  --prefix VOL_A/ \\\n"
        "  --pipeline htrflow"
    )


def test_build_entrypoint_s3_mode_defaults_off() -> None:
    """RunnerParams without source_mode defaults to iiif — byte-identical path unchanged."""
    params = RunnerParams(repo_root=Path("/r"), cache_bucket="images-batch", output="s3://o", iiif_url="https://i")
    assert params.source_mode == "iiif"
    out = build_entrypoint(["VOL_A"], params=params, spec=PIPELINE_SPECS["htr"])
    assert "--batch VOL_A" in out and "--input" not in out
```

- [ ] **Step 2: Run to verify they fail**

Run: `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest "components/services/core/tests/test_pipelines.py::test_build_entrypoint_s3_mode_uses_input_prefix" -p no:cacheprovider --no-header -q`
Expected: FAIL (`RunnerParams` has no `source_mode`).

- [ ] **Step 3: Add the `RunnerParams` fields** — in `packages/service-kit/src/service_kit/config.py`, add to the top imports `from typing import Literal` (if absent), and add two fields to `class RunnerParams` after `iiif_url`:

```python
    source_mode: Literal["iiif", "s3"] = "iiif"
    input_uri: str = ""
```

- [ ] **Step 4: Add `Settings.source_mode` and thread it through `runner_params`** — in `class Settings`, add near `iiif_url`:

```python
    source_mode: Literal["iiif", "s3"] = Field(default="iiif", alias="RASK_SOURCE_MODE")
```

and in `Settings.runner_params`, set the two new fields:

```python
    def runner_params(self) -> RunnerParams:
        """Bundle the runner's I/O config; centralizes the s3:// output prefix."""
        return RunnerParams(
            repo_root=self.repo_root,
            cache_bucket=self.cache_bucket,
            output=f"s3://{self.output_bucket}",
            iiif_url=self.iiif_url,
            source_mode=self.source_mode,
            input_uri=f"s3://{self.cache_bucket}",
        )
```

- [ ] **Step 5: Branch `build_entrypoint`** — in `components/services/core/src/core/services/submission.py`, at the start of `build_entrypoint` (after the `if spec.entrypoint_kind == "http":` block, before the existing `parts = [` for the runner kind) insert:

```python
    if params.source_mode == "s3":
        # Arbitrary-volume mode: read images straight from the input bucket via
        # S3Source. One volume per chunk (chunk_total=1), so a single --prefix.
        prefix = f"{batch_ids[0]}/"
        parts = [
            "uv run --project projects/runner runner",
            f"--input {params.input_uri}",
            f"--output {params.output}",
            f"--prefix {prefix}",
            f"--pipeline {spec.name}",
            *(f"--{flag} {value}" for flag, value in spec.extra_args),
        ]
        return " \\\n  ".join(parts)
```

- [ ] **Step 6: Run the new tests + the byte-identical guard**

Run: `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest components/services/core/tests/test_pipelines.py -p no:cacheprovider --no-header -q`
Expected: PASS, including `test_build_entrypoint_is_byte_identical` (unchanged).

- [ ] **Step 7: Typecheck + commit**

```bash
uvx ty check packages/service-kit components/services/core
git add packages/service-kit/src/service_kit/config.py components/services/core/src/core/services/submission.py components/services/core/tests/test_pipelines.py
git commit -m "feat(submission): RASK_SOURCE_MODE=s3 emits --input/--prefix runner invocation"
```

---

## Task 2: `register_volume` service — index an S3 prefix into `batches`

**Files:**
- Create: `components/services/core/src/core/services/registration.py`
- Test: `components/services/core/tests/test_registration.py`

**Interfaces:**
- Produces: `async register_volume(session: AsyncSession, client: S3Client, *, input_bucket: str, volume_id: str) -> Batch`. Lists `input_bucket/<volume_id>/` via `S3Source(...).keys()`; sets `page_count` + `cached_pages` = image count, `manifest_status=OK`, `chunk_total=1`, `chunk_id=max(chunk_id)+1` on insert (preserved on re-register). Raises `service_kit.exceptions.ValidationError` when the prefix has no images. Consumed by Task 3.

- [ ] **Step 1: Write the failing test** — create `components/services/core/tests/test_registration.py`:

```python
"""register_volume — index an S3 prefix into a one-chunk batches row (moto-backed)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from moto import mock_aws
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from core.models.batch import Batch
from core.models.enums import ManifestStatus
from core.services.registration import register_volume
from service_kit.exceptions import ValidationError


@pytest.fixture
def s3_client() -> Iterator[object]:
    import boto3

    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="images-batch")
        for key in ("VOL_A/00001.jpg", "VOL_A/00002.jpg", "VOL_A/notes.txt"):
            c.put_object(Bucket="images-batch", Key=key, Body=b"x")
        yield c


@pytest.fixture
async def session(tmp_path: Path) -> Iterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def test_register_counts_images_only(session: AsyncSession, s3_client: object) -> None:
    batch = await register_volume(session, s3_client, input_bucket="images-batch", volume_id="VOL_A")
    assert batch.batch_id == "VOL_A"
    assert batch.page_count == 2  # notes.txt is not an image
    assert batch.cached_pages == 2
    assert batch.manifest_status == ManifestStatus.OK
    assert batch.chunk_total == 1
    assert batch.chunk_id == 1


async def test_register_empty_prefix_raises(session: AsyncSession, s3_client: object) -> None:
    with pytest.raises(ValidationError):
        await register_volume(session, s3_client, input_bucket="images-batch", volume_id="MISSING")


async def test_register_is_idempotent_keeps_chunk_id(session: AsyncSession, s3_client: object) -> None:
    first = await register_volume(session, s3_client, input_bucket="images-batch", volume_id="VOL_A")
    s3_client.put_object(Bucket="images-batch", Key="VOL_A/00003.jpg", Body=b"x")
    again = await register_volume(session, s3_client, input_bucket="images-batch", volume_id="VOL_A")
    assert again.chunk_id == first.chunk_id  # preserved
    assert again.page_count == 3  # refreshed
```

- [ ] **Step 2: Run to verify it fails**

Run: `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest components/services/core/tests/test_registration.py -p no:cacheprovider --no-header -q`
Expected: FAIL (`No module named 'core.services.registration'`).

- [ ] **Step 3: Write the implementation** — create `components/services/core/src/core/services/registration.py`:

```python
"""Register an already-uploaded S3 prefix as a processable volume.

A "volume" is an S3 prefix under the input bucket. Registration indexes it into
the `batches` table so the existing orchestrator -> submit -> htrflow path picks
it up with no IIIF manifest. Indexing only: getting images into the bucket is a
separate concern. One volume = one chunk (chunk_total=1).
"""

from anyio import to_thread
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from core.models.batch import Batch
from core.models.enums import HtrStatus, ManifestStatus
from service_kit.exceptions import ValidationError
from storage import S3Client, S3Source


async def register_volume(session: AsyncSession, client: S3Client, *, input_bucket: str, volume_id: str) -> Batch:
    """Index `input_bucket/<volume_id>/` into a one-chunk batches row.

    `page_count`/`cached_pages` = number of images under the prefix (the images
    are already in the bucket, so they count as cached). Idempotent: re-registering
    refreshes the counts and keeps the existing `chunk_id`.
    """
    prefix = volume_id.rstrip("/") + "/"
    src = S3Source(bucket=input_bucket, prefix=prefix, client=client)
    keys = await to_thread.run_sync(lambda: list(src.keys()))
    if not keys:
        raise ValidationError(f"no images found under {input_bucket}/{prefix}")
    page_count = len(keys)

    batch = await session.get(Batch, volume_id)
    if batch is None:
        next_chunk = (await session.exec(select(func.coalesce(func.max(Batch.chunk_id), 0)))).one()
        batch = Batch(batch_id=volume_id, chunk_id=int(next_chunk) + 1, chunk_total=1, htr_status=HtrStatus.CACHED)
    batch.page_count = page_count
    batch.cached_pages = page_count
    batch.manifest_status = ManifestStatus.OK
    session.add(batch)
    await session.commit()
    await session.refresh(batch)
    return batch
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest components/services/core/tests/test_registration.py -p no:cacheprovider --no-header -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Add the test dir is already covered** — confirm `components/services/core/tests` is in `pyproject.toml` `testpaths` (it is). No change needed.

- [ ] **Step 6: Typecheck + commit**

```bash
uvx ty check components/services/core
git add components/services/core/src/core/services/registration.py components/services/core/tests/test_registration.py
git commit -m "feat(core): register_volume indexes an S3 prefix into a one-chunk batches row"
```

---

## Task 3: `POST /api/v1/batches/{batch_id}/register` endpoint + thin script

The gateway routes `/api/v1/volumes/*` to the read-only `volumes_api`, so registration (a state change) lives under `/batches/*` (→ core_api).

**Files:**
- Modify: `components/services/core/src/core/api/v1/endpoints/batches.py`
- Create: `components/scripts/register_volume.py`
- Test: `components/services/core/tests/test_register_endpoint.py`

**Interfaces:**
- Consumes: `register_volume` (Task 2), `S3Dep`/`SessionDep`/`SettingsDep` from `core.api.dependencies`.
- Produces: `POST {api_prefix}/batches/{batch_id}/register -> BatchPublic` (201).

- [ ] **Step 1: Write the failing test** — create `components/services/core/tests/test_register_endpoint.py`:

```python
"""POST /batches/{id}/register wiring — moto S3 + sqlite, via the live router."""

from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://images-batch")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://images-batch-alto")
    monkeypatch.setenv("RASK_CACHE_BUCKET", "images-batch")
    monkeypatch.setenv("RASK_BATCHES_DB", str(tmp_path / "b.db"))
    monkeypatch.setenv("HCP_ENDPOINT", "http://localhost:9000")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("RAY_DASHBOARD_URL", "http://127.0.0.1:1")
    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="images-batch")
        c.put_object(Bucket="images-batch", Key="VOL_A/00001.jpg", Body=b"x")
        c.put_object(Bucket="images-batch", Key="VOL_A/00002.jpg", Body=b"x")
        from core.main import create_app

        with TestClient(create_app()) as tc:
            yield tc


def test_register_endpoint_creates_batch(client: TestClient) -> None:
    resp = client.post("/api/v1/batches/VOL_A/register")
    assert resp.status_code == 201
    body = resp.json()
    assert body["batch_id"] == "VOL_A"
    assert body["page_count"] == 2
    assert body["manifest_status"] == "ok"
    # now visible via the normal read path
    assert client.get("/api/v1/batches/VOL_A").status_code == 200
```

- [ ] **Step 2: Run to verify it fails**

Run: `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest components/services/core/tests/test_register_endpoint.py -p no:cacheprovider --no-header -q`
Expected: FAIL (404 — route not defined).

- [ ] **Step 3: Add the endpoint** — in `components/services/core/src/core/api/v1/endpoints/batches.py`: add `status` to the fastapi import (`from fastapi import APIRouter, status`), import the service (`from core.services import registration`), and add the route (place it before the `/{batch_id}` GET so the literal path is unambiguous — FastAPI matches in declaration order, and `/{batch_id}/register` is distinct, but keep POST grouped with the other writes near `/sync`):

```python
@router.post("/{batch_id}/register", status_code=status.HTTP_201_CREATED)
async def register_batch(batch_id: str, session: SessionDep, settings: SettingsDep, s3: S3Dep) -> BatchPublic:
    """Index an already-uploaded input-bucket prefix `{batch_id}/` as a one-chunk volume."""
    batch = await registration.register_volume(session, s3, input_bucket=settings.cache_bucket, volume_id=batch_id)
    return BatchPublic.model_validate(batch)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest components/services/core/tests/test_register_endpoint.py -p no:cacheprovider --no-header -q`
Expected: PASS.

- [ ] **Step 5: Add the thin script** — create `components/scripts/register_volume.py`:

```python
"""Register an uploaded volume by POSTing to core-api.

Usage:
    uv run python components/scripts/register_volume.py VOL_A [VOL_B ...] \
        --base-url http://localhost:8888

Images must already be in the input bucket under `<volume_id>/`. This only
indexes them into the batches table; it does not upload.
"""

import argparse
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="Register uploaded S3 volumes into rask.")
    parser.add_argument("volume_ids", nargs="+", help="Volume ids = input-bucket prefixes (e.g. VOL_A).")
    parser.add_argument("--base-url", default="http://localhost:8888", help="Gateway/core-api base URL.")
    parser.add_argument("--api-prefix", default="/api/v1")
    args = parser.parse_args()

    rc = 0
    with httpx.Client(base_url=args.base_url, timeout=120.0) as client:
        for vol in args.volume_ids:
            resp = client.post(f"{args.api_prefix}/batches/{vol}/register")
            if resp.status_code == 201:
                body = resp.json()
                print(f"registered {vol}: page_count={body['page_count']} chunk_id={body['chunk_id']}")
            else:
                print(f"FAILED {vol}: {resp.status_code} {resp.text}", file=sys.stderr)
                rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Typecheck + commit**

```bash
uvx ty check components/services/core
git add components/services/core/src/core/api/v1/endpoints/batches.py components/scripts/register_volume.py components/services/core/tests/test_register_endpoint.py
git commit -m "feat(core-api): POST /batches/{id}/register + register_volume client script"
```

---

## Task 4: Single-endpoint config + chart/dockerfile correctness fixes

No runtime code — config + Helm correctness so `helm template`/`helm lint` render against the current `core` fleet and an S3-source, single-`htrflow` deployment.

**Files:**
- Modify: `chart/values.yaml`
- Modify: `chart/templates/configmap.yaml` (only if env keys are enumerated there — inspect first)
- Modify: `Makefile` (add a `serve-up-htrflow` convenience target)

- [ ] **Step 1: Inspect the configmap template** — `cat chart/templates/configmap.yaml` to see whether it renders `.Values.config` as a map (then no per-key edit needed) or enumerates keys. Note which.

- [ ] **Step 2: Fix `chart/values.yaml`** — apply these edits:
  - Under `config:` add `RASK_VIEWER_INPUT: "s3://images-batch"`, `RASK_VIEWER_OUTPUT: "s3://images-batch-alto"`, `RASK_SOURCE_MODE: "s3"`, `RASK_HTR_PIPELINE: "htrflow"`, `RASK_PREFETCH_PIPELINE: "none"`.
  - Change the hardcoded `RASK_IIIF_URL: "https://iiifintern-ai.ra.se"` to `RASK_IIIF_URL: ""` (no longer required in s3 mode; keep the key so IIIF deploys can still set it).
  - Fix `migrations.command`: replace `components/services/viewer` with `components/services/core`.
  - Change `ingress.className: "nginx"` to `ingress.className: ""` (k3s default Traefik) and add a comment that nginx must be installed separately if desired.

- [ ] **Step 3: Add a Makefile convenience target** — append under the serve section of `Makefile`:

```make
# Single CPU/1-GPU htrflow endpoint for the low-resource / local-k3s shape.
serve-up-htrflow:
	RASK_SERVE_REPLICAS=1 RASK_SERVE_GPU_FRAC=$(RASK_SERVE_GPU_FRAC) \
	  RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python components/scripts/deploy_serve.py up --app htrflow
```

- [ ] **Step 4: Render the chart to verify it's valid**

Run: `helm lint chart/ && helm template rask chart/ >/dev/null && echo OK`
Expected: `OK` (lint passes; template renders with no errors). If `helm` is absent, note it and skip — this task's deliverable is then verified in Phase 2.

- [ ] **Step 5: Commit**

```bash
git add chart/values.yaml Makefile
git commit -m "chore(chart): S3-source single-htrflow config; fix stale viewer migration path + ingress class"
```

---

## Task 5: End-to-end verification (no IIIF) — single htrflow endpoint over an arbitrary volume

REQUIRED SUB-SKILL when claiming completion: superpowers:verification-before-completion. This task makes no code change and no commit — it proves the seams work end to end and records evidence.

**Files:** none (runtime check).

- [ ] **Step 1: Bring up Ray + a single htrflow endpoint**

```bash
make ray-up
make serve-up-htrflow            # RASK_SERVE_REPLICAS=1
uv run --no-sync python components/scripts/deploy_serve.py status --app htrflow
```
Expected: htrflow app `RUNNING` with 1 replica. (If the GPU stack isn't ready, htrflow runs CPU-only per its YAML — slower but valid.)

- [ ] **Step 2: Run the runner directly over a local folder of sample images (proves the S3Source/htrflow path with zero IIIF)**

```bash
ls packages/htr/tests/**/*.jpg 2>/dev/null | head    # find sample images, or supply your own dir
mkdir -p /tmp/vol_smoke && cp <a few .jpg> /tmp/vol_smoke/
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync --project projects/runner runner \
  --input /tmp/vol_smoke --output /tmp/vol_out --pipeline htrflow
```
Expected: `Done — ok=<N>`; `/tmp/vol_out/` contains one `.xml` (ALTO) per input image. **Paste the command output and an `ls /tmp/vol_out` listing as evidence.**

- [ ] **Step 3: Run the focused suite + the full not-slow suite + typecheck/lint**

```bash
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest \
  components/services/core/tests packages/storage/tests -m "not slow" -p no:cacheprovider --no-header -q
uvx ty check packages/service-kit components/services/core
uv run ruff check components/services/core packages/service-kit components/scripts/register_volume.py
```
Expected: all green; pass count = baseline + the new tests from Tasks 1–3. **Paste the summary line.**

- [ ] **Step 4: Tear down**

```bash
make serve-down || true
make ray-down            # NOTE: host-wide. Only run if no other Ray work is using the local cluster.
```

- [ ] **Step 5: Done** — no commit. Summarize evidence (ALTO produced, suite green) before claiming Phase 1 complete.

---

## Phase 1 done criteria

- `RASK_SOURCE_MODE=s3` makes `build_entrypoint` emit `--input/--prefix`; the IIIF golden-string test is still byte-identical.
- `register_volume` + `POST /batches/{id}/register` index an S3 prefix into a one-chunk `batches` row (idempotent; empty-prefix → 4xx).
- A single `htrflow` endpoint transcribes a local/S3 image folder to ALTO with no IIIF.
- `make check`-equivalent (ruff + ty) clean; full not-slow suite green; chart renders.

---

## Phase 2 — Local k3s deploy (authored as a separate plan)

Phase 2 (in-cluster MinIO + Postgres + Ray head + GPU `htrflow`, per-service Dockerfiles, `make k3s-install`/`k3s-up`) is **deliberately deferred to its own plan**, written after Phase 1 merges and after the **GB10/aarch64 GPU-image spike** resolves the base image — its task content (exact Dockerfile bases, k8s template values, GPU runtime wiring) materially depends on both, and writing it now would be guesswork. Scope and templates are fully enumerated in the design spec §Phase 2 (`docs/superpowers/specs/2026-06-17-rask-local-k3s-volumes-design.md`). When ready, generate it with `superpowers:writing-plans` into `docs/superpowers/plans/2026-06-17-rask-local-k3s-deploy.md`.
