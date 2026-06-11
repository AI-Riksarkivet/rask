# Orchestrator-driven HTR via lightweight HTTP job — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the viewer orchestrator run HTR batches on dev-kuberay by submitting a self-contained HTTP job that reads S3 pages, POSTs them to the deployed `/htr/transcribe` endpoint, and writes ALTO back to S3 — no ray 2.55→3.x runner port.

**Architecture:** Add a new `entrypoint_kind="http"` pipeline spec (`htr_http`). The orchestrator's `submit_chunk` builds a `python htr_chunk_job.py …` entrypoint (instead of `uv run … runner`) for http specs and adds `pip:["boto3"]` to the job `runtime_env`. The job runs on the cluster's `ray-llm` image, reaching the Serve proxy at `localhost:8000`. Prefetch is gated off via a new `RASK_PREFETCH_ENABLED` flag. Progress is tracked by the existing S3 reconcile.

**Tech Stack:** Python 3.13, Pydantic v2 (SQLModel), Ray Job Submission API, boto3, stdlib urllib, pytest. Spec: `docs/superpowers/specs/2026-06-11-orchestrator-http-htr-design.md`.

---

### Task 1: `PipelineSpec` gains `entrypoint_kind` + `pip`; register `htr_http`

**Files:**
- Modify: `components/services/viewer/src/viewer/models/pipelines.py`
- Test: `components/services/viewer/tests/test_pipelines.py`

- [ ] **Step 1: Write the failing test** — append to `test_pipelines.py`:

```python
def test_htr_http_spec_is_http_kind_with_boto3() -> None:
    spec = PIPELINE_SPECS["htr_http"]
    assert spec.entrypoint_kind == "http"
    assert spec.slot is Slot.HTR
    assert spec.pip == ("boto3",)
    assert spec.stages == ()


def test_runner_kind_specs_match_runner_pipelines() -> None:
    runner_kind = {k for k, v in PIPELINE_SPECS.items() if v.entrypoint_kind == "runner"}
    assert runner_kind == _EXPECTED_RUNNER_PIPELINES
    runner_pipelines = pytest.importorskip(
        "runner.pipeline", reason="runner not on the viewer test path"
    ).PIPELINES
    assert runner_kind == set(runner_pipelines)
```

- [ ] **Step 2: Replace the now-obsolete equality test.** Delete `test_registry_keys_equal_runner_pipelines_exactly` (lines ~114-120) — `test_runner_kind_specs_match_runner_pipelines` replaces it (htr_http is viewer-only, not a runner pipeline, so the registry is no longer byte-equal to runner pipelines; only the runner-KIND subset is).

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest components/services/viewer/tests/test_pipelines.py -k "htr_http or runner_kind" -v`
Expected: FAIL — `KeyError: 'htr_http'` / `AttributeError: entrypoint_kind`.

- [ ] **Step 4: Add the fields to `PipelineSpec`.** In `pipelines.py`, ensure `from typing import Literal` is imported (add if missing). Add two fields after `extra_args`:

```python
    extra_args: tuple[tuple[str, str | int], ...] = ()
    # "runner" (default) → `uv run … runner --pipeline <name>`. "http" → a
    # standalone python job (htr_chunk_job.py) that POSTs to the /htr endpoint;
    # http specs are viewer-only and have no matching runner --pipeline.
    entrypoint_kind: Literal["runner", "http"] = "runner"
    # Extra pip packages for the job's runtime_env (http specs need boto3).
    pip: tuple[str, ...] = ()
```

- [ ] **Step 5: Register the `htr_http` spec.** Add to `PIPELINE_SPECS` (after `"fake"`):

```python
    "htr_http": PipelineSpec(
        name="htr_http",
        label="HTR (HTTP → /htr endpoint)",
        slot=Slot.HTR,
        # No Ray actors → no per-stage telemetry; progress comes from S3 reconcile.
        stages=(),
        entrypoint_kind="http",
        pip=("boto3",),
        tracks_rayjob_id=True,
    ),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest components/services/viewer/tests/test_pipelines.py -v`
Expected: PASS (all, including the unchanged golden-string and frozen tests).

- [ ] **Step 7: Commit**

```bash
git add components/services/viewer/src/viewer/models/pipelines.py components/services/viewer/tests/test_pipelines.py
git commit -m "feat(pipelines): add http-kind PipelineSpec + htr_http registry entry"
```

---

### Task 2: `build_entrypoint` emits the HTTP job; `submit_chunk` adds `pip`

**Files:**
- Modify: `components/services/viewer/src/viewer/services/submission.py`
- Test: `components/services/viewer/tests/test_pipelines.py`

- [ ] **Step 1: Write the failing test** — append to `test_pipelines.py`:

```python
def test_build_entrypoint_http_kind_runs_the_job_script() -> None:
    spec = PIPELINE_SPECS["htr_http"]
    params = RunnerParams(
        repo_root=Path("/repo"),
        cache_bucket="images-batch",
        output="s3://images-batch-alto",
        iiif_url="https://iiifintern-ai.ra.se",
    )
    out = build_entrypoint(["VOL_A", "VOL_B"], params=params, spec=spec)
    assert out == (
        "python components/scripts/htr_chunk_job.py \\\n"
        "  --cache-bucket images-batch \\\n"
        "  --output s3://images-batch-alto \\\n"
        "  --batch VOL_A \\\n"
        "  --batch VOL_B"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest components/services/viewer/tests/test_pipelines.py::test_build_entrypoint_http_kind_runs_the_job_script -v`
Expected: FAIL (build_entrypoint emits the runner command).

- [ ] **Step 3: Branch `build_entrypoint` on `entrypoint_kind`.** Replace the body of `build_entrypoint` in `submission.py`:

```python
def build_entrypoint(batch_ids: list[str], *, params: RunnerParams, spec: PipelineSpec) -> str:
    """Build the job invocation that processes all batch_ids in one job.

    `runner` specs run the Ray Data pipeline; `http` specs run the standalone
    htr_chunk_job.py, which POSTs pages to the deployed /htr endpoint.
    """
    if spec.entrypoint_kind == "http":
        parts = [
            "python components/scripts/htr_chunk_job.py",
            f"--cache-bucket {params.cache_bucket}",
            f"--output {params.output}",
            *(f"--{flag} {value}" for flag, value in spec.extra_args),
            *(f"--batch {b}" for b in batch_ids),
        ]
        return " \\\n  ".join(parts)
    parts = [
        "uv run --project projects/runner runner",
        f"--cache-bucket {params.cache_bucket}",
        f"--output {params.output}",
        f"--iiif-url {params.iiif_url}",
        f"--pipeline {spec.name}",
        *(f"--{flag} {value}" for flag, value in spec.extra_args),
        *(f"--batch {b}" for b in batch_ids),
    ]
    return " \\\n  ".join(parts)
```

- [ ] **Step 4: Add `pip` to the job `runtime_env`.** In `submit_chunk`'s `_submit`, change the `runtime_env` dict:

```python
            runtime_env={
                "working_dir": str(params.repo_root),
                "env_vars": _passthrough_env(env if env is not None else os.environ),
                **({"pip": list(spec.pip)} if spec.pip else {}),
            },
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest components/services/viewer/tests/test_pipelines.py -v`
Expected: PASS (http test passes; the four runner golden-string tests stay byte-identical).

- [ ] **Step 6: Commit**

```bash
git add components/services/viewer/src/viewer/services/submission.py components/services/viewer/tests/test_pipelines.py
git commit -m "feat(submission): build htr_chunk_job entrypoint + boto3 pip for http specs"
```

---

### Task 3: `RASK_PREFETCH_ENABLED` gate in config + orchestrator loop

**Files:**
- Modify: `components/services/viewer/src/viewer/core/config.py`
- Modify: `components/services/viewer/src/viewer/services/orchestrator/loop.py`
- Test: `components/services/viewer/tests/test_orchestrator_prefetch_gate.py` (create)

- [ ] **Step 1: Write the failing test** — create `test_orchestrator_prefetch_gate.py`. (Matches the real `tick(*, settings, sessionmaker, ray_client, http, s3)` seam: it reconciles iff `s3` is not None, gets `state` from `derive_state`, then submits. We patch `derive_state` and `submit_chunk` — both module-level names in `loop` — and pass `s3=None` to skip reconcile.)

```python
"""The orchestrator must NOT submit prefetch jobs when prefetch is disabled."""
import contextlib
from types import SimpleNamespace

import pytest

from viewer.models.pipelines import PIPELINE_SPECS
from viewer.services.orchestrator import loop as loop_mod


class _Lane:
    def __init__(self, eligible):
        self.eligible = eligible


def _sessionmaker():
    @contextlib.asynccontextmanager
    async def _cm():
        yield object()  # session is unused once derive_state is patched

    return _cm


@pytest.mark.asyncio
async def test_prefetch_skipped_when_disabled(monkeypatch):
    submitted: list[tuple[str, int]] = []

    async def _fake_submit(_session, _client, *, chunk_id, params, spec):  # noqa: ANN001
        submitted.append((spec.slot.value, chunk_id))

    async def _fake_derive(**_kwargs):
        return SimpleNamespace(ok=True, prefetch=_Lane([1, 2]), htr=_Lane([3]))

    monkeypatch.setattr(loop_mod, "submit_chunk", _fake_submit)
    monkeypatch.setattr(loop_mod, "derive_state", _fake_derive)

    settings = SimpleNamespace(
        prefetch_enabled=False,
        prefetch_pipeline="prefetch",
        htr_pipeline="htr_http",
        runner_params=lambda: SimpleNamespace(),
        ray_dashboard_url="http://x",
        cache_bucket="images-batch",
        output_bucket="images-batch-alto",
    )
    await loop_mod.tick(
        settings=settings,
        sessionmaker=_sessionmaker(),
        ray_client=object(),
        http=object(),
        s3=None,
    )

    # htr_http's slot is Slot.HTR (value "htr"); prefetch's is "prefetch".
    assert all(slot != "prefetch" for slot, _ in submitted)
    assert ("htr", 3) in submitted
```

> Before running, confirm `derive_state` is imported into `loop`'s namespace (it's called unqualified in `tick`, so it is) so `monkeypatch.setattr(loop_mod, "derive_state", …)` binds the name `tick` actually calls.

- [ ] **Step 2: Add the config field.** In `config.py`, near `orchestrator_autostart` / `prefetch_pipeline`:

```python
    prefetch_enabled: bool = Field(default=True, alias="RASK_PREFETCH_ENABLED")
```

- [ ] **Step 3: Gate the prefetch submission block.** In `loop.py`, wrap the prefetch loop:

```python
        params = settings.runner_params()
        if settings.prefetch_enabled:
            prefetch_spec = PIPELINE_SPECS[settings.prefetch_pipeline]
            for cid in state.prefetch.eligible:
                log.info(f"orchestrator: submitting {settings.prefetch_pipeline} for chunk {cid}")
                await submit_chunk(session, ray_client, chunk_id=cid, params=params, spec=prefetch_spec)
        htr_spec = PIPELINE_SPECS[settings.htr_pipeline]
        for cid in state.htr.eligible:
            log.info(f"orchestrator: submitting {settings.htr_pipeline} for chunk {cid}")
            await submit_chunk(session, ray_client, chunk_id=cid, params=params, spec=htr_spec)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest components/services/viewer/tests/test_orchestrator_prefetch_gate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add components/services/viewer/src/viewer/core/config.py components/services/viewer/src/viewer/services/orchestrator/loop.py components/services/viewer/tests/test_orchestrator_prefetch_gate.py
git commit -m "feat(orchestrator): RASK_PREFETCH_ENABLED gate on the prefetch slot"
```

---

### Task 4: The `htr_chunk_job.py` cluster job

**Files:**
- Create: `components/scripts/htr_chunk_job.py`
- Test: `components/services/viewer/tests/test_htr_chunk_job.py` (create — pure-logic units only; no live S3/HTTP)

- [ ] **Step 1: Write the failing test** — create `test_htr_chunk_job.py`:

```python
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[3] / "components/scripts/htr_chunk_job.py"
_spec = importlib.util.spec_from_file_location("htr_chunk_job", _PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)  # type: ignore[union-attr]


def test_bucket_strips_s3_scheme():
    assert m.bucket_name("s3://images-batch-alto") == "images-batch-alto"
    assert m.bucket_name("images-batch-alto") == "images-batch-alto"


def test_out_key_maps_jpg_to_xml_under_batch():
    assert m.out_key("008558342/008558342_00003.jpg") == "008558342/008558342_00003.xml"


def test_is_jpg():
    assert m.is_jpg("a/b.jpg") and m.is_jpg("a/b.JPG")
    assert not m.is_jpg("a/b.xml") and not m.is_jpg("a/")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest components/services/viewer/tests/test_htr_chunk_job.py -v`
Expected: FAIL (file does not exist).

- [ ] **Step 3: Write the job.** Create `components/scripts/htr_chunk_job.py`:

```python
"""Transcribe a chunk's pages via the cluster /htr endpoint, write ALTO to S3.

Submitted by the viewer orchestrator as a Ray job (the entrypoint runs on the
head node, where the Serve proxy is reachable at localhost:8000). Self-contained
— NO rask imports; needs only boto3 (installed via the job runtime_env) + stdlib.

S3 creds come from the env handed to the job: AWS_ACCESS_KEY_ID/SECRET (derived
from HCP_USERNAME/PASSWORD by the viewer and passed through), HCP_ENDPOINT,
HCP_INSECURE. For each --batch: list <cache-bucket>/<batch>/ *.jpg; pages whose
ALTO already exists in <output>/<batch>/ are skipped (resumable); otherwise GET
the image, POST it to /htr/transcribe, and write the returned ALTO XML.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import os
import sys
import urllib.request
from urllib.parse import urlparse

import boto3
from botocore.config import Config

log = logging.getLogger("htr_chunk_job")


def bucket_name(uri: str) -> str:
    return urlparse(uri).netloc if uri.startswith("s3://") else uri


def is_jpg(key: str) -> bool:
    return key.lower().endswith(".jpg")


def out_key(jpg_key: str) -> str:
    """`<batch>/<stem>.jpg` → `<batch>/<stem>.xml` (output is keyed identically)."""
    batch, _, fname = jpg_key.partition("/")
    stem = fname.rsplit(".", 1)[0]
    return f"{batch}/{stem}.xml"


def s3_client() -> object:
    kwargs: dict = {
        "endpoint_url": os.environ.get("HCP_ENDPOINT"),
        "config": Config(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "adaptive"},
        ),
    }
    if os.environ.get("HCP_INSECURE", "").lower() in ("1", "true", "yes"):
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        kwargs["verify"] = False
    return boto3.client("s3", **kwargs)


def list_keys(s3: object, bucket: str, prefix: str, suffix_ok) -> list[str]:
    out: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out.extend(o["Key"] for o in page.get("Contents", []) if suffix_ok(o["Key"]))
    return out


def transcribe(endpoint: str, img: bytes) -> str:
    req = urllib.request.Request(
        endpoint, data=img, headers={"Content-Type": "application/octet-stream"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return resp.read().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-bucket", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--endpoint", default="http://localhost:8000/htr/transcribe")
    ap.add_argument("--batch", action="append", required=True)
    ap.add_argument("--concurrency", type=int, default=8)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    s3 = s3_client()
    out_bucket = bucket_name(a.output)

    # Plan the work: every jpg page across the requested batches, minus pages
    # whose ALTO already exists (resumable). One output listing per batch.
    todo: list[str] = []
    for batch in a.batch:
        done = {k for k in list_keys(s3, out_bucket, f"{batch}/", lambda k: k.lower().endswith(".xml"))}
        pages = list_keys(s3, a.cache_bucket, f"{batch}/", is_jpg)
        todo.extend(k for k in pages if out_key(k) not in done)
        log.info("batch %s: %d pages, %d already done", batch, len(pages), len(pages) - sum(out_key(k) not in done for k in pages))
    log.info("htr_chunk_job: %d pages to transcribe (concurrency=%d)", len(todo), a.concurrency)

    def work(key: str) -> None:
        img = s3.get_object(Bucket=a.cache_bucket, Key=key)["Body"].read()
        alto = transcribe(a.endpoint, img)
        s3.put_object(Bucket=out_bucket, Key=out_key(key), Body=alto.encode("utf-8"), ContentType="application/xml")

    ok = fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = {ex.submit(work, k): k for k in todo}
        for f in concurrent.futures.as_completed(futs):
            try:
                f.result()
                ok += 1
            except Exception as exc:  # noqa: BLE001 — one bad page must not abort the chunk
                fail += 1
                log.warning("page %s failed: %s", futs[f], str(exc)[:200])
    log.info("htr_chunk_job done: ok=%d fail=%d", ok, fail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest components/services/viewer/tests/test_htr_chunk_job.py -v`
Expected: PASS.

- [ ] **Step 5: Lint/typecheck the new file**

Run: `uv run ruff check components/scripts/htr_chunk_job.py && uvx ty check components/scripts/htr_chunk_job.py`
Expected: clean (boto3 has no stubs — `object` return on `s3_client` keeps `ty` quiet; if `ty` flags a `.get_paginator`/`.get_object` attribute on `object`, annotate those locals as `Any` via `from typing import Any` rather than importing boto3 stubs).

- [ ] **Step 6: Commit**

```bash
git add components/scripts/htr_chunk_job.py components/services/viewer/tests/test_htr_chunk_job.py
git commit -m "feat(scripts): htr_chunk_job — S3 pages -> /htr -> ALTO (orchestrator job)"
```

---

### Task 5: Full suite + typecheck gate

**Files:** none (verification only)

- [ ] **Step 1: Run the viewer test suite**

Run: `uv run pytest components/services/viewer/tests/ -q`
Expected: PASS (no regressions; new tests included).

- [ ] **Step 2: Format + lint + typecheck the changed Python**

Run: `make fmt && make lint && make typecheck`
Expected: clean. (Pre-existing unrelated failures, if any, must be untouched by this change — note them but do not fix here.)

- [ ] **Step 3: Commit any formatter-only changes**

```bash
git add -A && git commit -m "chore: fmt/lint for orchestrator http-htr" || echo "nothing to commit"
```

---

### Task 6: Operational enablement + live validation (after S3 write grant)

**Files:**
- Modify (local, gitignored): `/home/morgan/rask/.env`

- [ ] **Step 1: Switch the orchestrator to the HTTP HTR pipeline + disable prefetch.** Add to `.env`:

```
RASK_HTR_PIPELINE=htr_http
RASK_PREFETCH_ENABLED=0
```

- [ ] **Step 2: Restart the viewer backend** so it reloads `.env` (config is read at startup):

```bash
pkill -f "uvicorn viewer.main:app"; sleep 2; make viewer > /tmp/rask-viewer-backend.log 2>&1 &
```

- [ ] **Step 3: Confirm the S3 write grant landed** (ser_devai_rw can write images-batch-alto). If not, STOP — the orchestrator will submit jobs that fail on `put_object`.

- [ ] **Step 4: Start the orchestrator and watch one chunk.**

```bash
curl -s -X POST http://localhost:8888/api/orchestrator/start
# poll: a htr_http-* job appears, then ALTO lands and htr_status flips
curl -s "https://dev-kuberay.ra.se/api/jobs/" | python3 -c "import sys,json;[print(j.get('submission_id'),j.get('status')) for j in json.load(sys.stdin) if 'htr_http' in (j.get('submission_id') or '')]"
```

Expected: a `htr_http-chunk-…` job runs; after it finishes, `images-batch-alto/<batch>/*.xml` exist and the next orchestrator sync sets those batches' `htr_status` toward `done`. Stop with `curl -s -X POST http://localhost:8888/api/orchestrator/stop` once validated.

---

## Notes for the implementer
- boto3 clients are thread-safe for concurrent calls; sharing one `s3` across the thread pool is intentional.
- The job reaches the Serve proxy at `localhost:8000` because the Ray job entrypoint runs on the head node (verified earlier with `htr_test.py`). Do not hardcode a cluster IP.
- `working_dir = repo_root` uploads the rask repo (~12 MB sans venv) so the cluster job can `python components/scripts/htr_chunk_job.py`. Ray excludes per `.gitignore`; if the upload is rejected as too large, add a runtime_env `excludes` for `.venv`/`node_modules` — but verify first, don't assume.
