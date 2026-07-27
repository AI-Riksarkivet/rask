"""Phase-1 owner-requested proofs for the PipelineSpec registry.

Three things the prior-phase tests did NOT cover and the owner asked for here:

1. GOLDEN STRING — `build_entrypoint` must stay BYTE-IDENTICAL to what the old
   hardcoded code produced. The expected value is a hand-written literal (NOT
   re-derived from `build_entrypoint`'s own format string), so the assertion
   actually pins the wire format.

2. REGISTRY INVARIANTS — the registry keys must equal the runner's PIPELINES
   dict EXACTLY (ends the silent divergence), specs are frozen, DEFAULT is
   registered.

3. CUSTOM RUNNER end-to-end at the HTTP layer — register a throwaway
   "testrunner" spec, inject a fake Ray client, and drive the real
   POST /submit + POST /stop endpoints: 200 happy path (entrypoint carries
   `--pipeline testrunner` + the seeded batches, submission_id tagged in the
   DB), 422 for an unknown name, no slot cap (two submits into one lane and two
   pipelines on one chunk both 200), and a stop that calls `stop_job` with the
   tagged id and clears the DB markers. Plus the prefetch-stop bug fix
   exercised through the HTTP endpoints.

No real Ray cluster: the fake client implements only the SDK surface the
viewer touches (`list_jobs`/`submit_job`/`stop_job`/`get_job_info`).
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from ray.dashboard.modules.job.common import JobStatus
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import Session, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from core.api.dependencies import get_ray_client
from core.models.batch import Batch
from core.models.enums import HtrStatus, ManifestStatus
from core.models.pipelines import DEFAULT_PIPELINE, PIPELINE_SPECS, PipelineSpec, Slot
from core.repositories import batch as batch_repo
from core.services.submission import build_entrypoint
from service_kit.config import RunnerParams


# ── 1. GOLDEN STRING: byte-identical runner entrypoint ─────────────────────

# The literal the OLD hardcoded builder produced for two batches. Parts joined
# by " \<newline>  ". Reconstructed by hand so this pins the format rather than
# echoing build_entrypoint's own join.
_GOLDEN_PREFIX = (
    "uv run --project runners/htr runner \\\n"
    "  --cache-bucket images-batch \\\n"
    "  --output s3://images-batch-alto \\\n"
    "  --iiif-url https://iiifintern-ai.ra.se \\\n"
    "  --pipeline {pipeline} \\\n"
    "  --batch VOL_A \\\n"
    "  --batch VOL_B"
)


def _build(pipeline: str) -> str:
    return build_entrypoint(
        ["VOL_A", "VOL_B"],
        params=RunnerParams(
            repo_root=Path("/repo"),
            cache_bucket="images-batch",
            output="s3://images-batch-alto",
            iiif_url="https://iiifintern-ai.ra.se",
        ),
        spec=PIPELINE_SPECS[pipeline],
    )


@pytest.mark.parametrize("pipeline", ["htr", "prefetch", "htrflow", "fake"])
def test_build_entrypoint_is_byte_identical(pipeline: str) -> None:
    """Safety invariant: the viewer-built command string is unchanged for every
    registered pipeline. This is what lets the runner stay untouched."""
    assert _build(pipeline) == _GOLDEN_PREFIX.format(pipeline=pipeline)


def test_build_entrypoint_with_no_extra_args_has_no_extra_flags() -> None:
    """extra_args is empty in Phase 1, so nothing is rendered between
    --pipeline and the --batch flags (part of the byte-identical guarantee)."""
    out = _build("htr")
    assert "--pipeline htr \\\n  --batch VOL_A" in out


def test_build_entrypoint_renders_extra_args_before_batches() -> None:
    """L2 forward-proofing: when a spec DOES carry extra_args they render as
    `--{flag} {value}` immediately after --pipeline and before --batch. Uses a
    throwaway spec so the registered specs stay byte-identical."""
    spec = PipelineSpec(name="htr", label="x", slot=Slot.HTR, stages=(), extra_args=(("limit", 5),))
    params = RunnerParams(repo_root=Path("/r"), cache_bucket="cb", output="s3://o", iiif_url="https://i")
    out = build_entrypoint(["VOL_A"], params=params, spec=spec)
    assert "--pipeline htr \\\n  --limit 5 \\\n  --batch VOL_A" in out


def test_build_entrypoint_honours_configurable_runner_cmd() -> None:
    """The in-cluster ray image has no `uv`/source tree, only the installed
    `runner` console script — so RASK_RUNNER_CMD overrides the invocation. The
    command must start with the configured runner_cmd, not the uv-run default."""
    params = RunnerParams(
        repo_root=Path("/r"),
        cache_bucket="images-batch",
        output="s3://images-batch-alto",
        iiif_url="https://i",
        source_mode="s3",
        input_uri="s3://images-batch",
        runner_cmd="runner",
    )
    out = build_entrypoint(["VOL_A"], params=params, spec=PIPELINE_SPECS["htrflow"])
    assert out.startswith("runner \\\n  --input s3://images-batch")
    assert "uv run" not in out


# ── 2. REGISTRY INVARIANTS ─────────────────────────────────────────────────


# The runner's PIPELINES keys, mirrored here. The core package can't import
# `runner` (not one of its dependencies), so when it IS importable we assert
# equality against the live dict; otherwise we pin against this literal. Either
# way a divergence between the two sources is what this test exists to catch.
_EXPECTED_RUNNER_PIPELINES = {"htr", "htrflow", "fake", "prefetch"}


def test_default_pipeline_is_registered() -> None:
    assert DEFAULT_PIPELINE in PIPELINE_SPECS


def _assign(obj: object, field: str, value: object) -> None:
    """Write an attribute dynamically.

    A literal `spec.name = ...` is a *static* error on a frozen pydantic model
    (the field is read-only), which would hide the runtime assertion below
    behind a type-checker suppression. Going through `setattr` keeps the check
    where it belongs: at runtime.
    """
    setattr(obj, field, value)


def test_pipeline_specs_are_frozen() -> None:
    """frozen=True → assignment raises, so the registry can't be mutated at runtime."""
    spec = PIPELINE_SPECS["htr"]
    with pytest.raises(ValidationError):
        _assign(spec, "name", "mutated")


def test_slot_submission_id_prefix_matches_value() -> None:
    assert Slot.PREFETCH.submission_id_prefix == "prefetch-"
    assert Slot.HTR.submission_id_prefix == "htr-"


# ── DB still works: orchestrator-consumed repo queries didn't regress ──────


@pytest_asyncio.fixture
async def repo_session() -> AsyncIterator[AsyncSession]:
    """In-memory async sqlite seeded with two chunks at different progress."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine) as s:
        # chunk 1: fully cached + transcribed (done) — not prefetch-pending.
        s.add(
            Batch(
                batch_id="DONE",
                htr_status=HtrStatus.DONE,
                manifest_status=ManifestStatus.OK,
                page_count=10,
                cached_pages=10,
                transcribed_pages=10,
                chunk_id=1,
                chunk_total=2,
                last_synced_at="2026-01-01T00:00:00+00:00",
            )
        )
        # chunk 2: partially cached, not transcribed — prefetch-pending.
        s.add(
            Batch(
                batch_id="PARTIAL",
                htr_status=HtrStatus.PARTIAL,
                manifest_status=ManifestStatus.OK,
                page_count=40,
                cached_pages=20,
                transcribed_pages=0,
                chunk_id=2,
                chunk_total=2,
                last_synced_at="2026-01-01T00:00:00+00:00",
            )
        )
        await s.commit()
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_repo_queries_used_by_orchestrator_still_work(repo_session: AsyncSession) -> None:
    """The schema-touching query layer the registry refactor sits on top of:
    prefetch-pending detection + per-chunk progress must behave as before."""
    pending = await batch_repo.prefetch_pending_chunk_ids(repo_session)
    assert pending == [2]  # only the partially-cached chunk needs prefetch

    progress = {p.chunk_id: p for p in await batch_repo.chunks_with_progress(repo_session)}
    assert progress[1].expected_pages == 10
    assert progress[1].cached_pages == 10
    assert progress[1].transcribed_pages == 10
    assert progress[2].expected_pages == 40
    assert progress[2].cached_pages == 20
    assert progress[2].transcribed_pages == 0

    summary = {c.chunk_id: c for c in await batch_repo.chunks_summary(repo_session)}
    assert summary[1].done_batches == 1
    assert summary[2].done_batches == 0


# ── 3. CUSTOM RUNNER end-to-end through the real HTTP endpoints ────────────


class _FakeJobDetails:
    """Minimal stand-in for Ray's `JobDetails` (a Pydantic-V1 model). Only the
    fields `ray_dashboard.list_jobs` reads need to round-trip through `.dict()`."""

    def __init__(self, submission_id: str, status: JobStatus, entrypoint: str) -> None:
        self.submission_id = submission_id
        self.status = status
        self.entrypoint = entrypoint
        self.job_id = "drv-0"

    def dict(self) -> dict[str, object]:  # matches Ray's Pydantic-V1 JobDetails.dict()
        return {
            "submission_id": self.submission_id,
            "status": self.status,
            "entrypoint": self.entrypoint,
            "start_time": 0,
            "end_time": None,
        }


class _FakeRayClient:
    """Records submit/stop and reports a configurable job list. Stands in for
    JobSubmissionClient across derive_state (orchestrator loop) and submission."""

    def __init__(self) -> None:
        self.jobs: list[_FakeJobDetails] = []
        self.submitted: list[dict[str, object]] = []
        self.stopped: list[str] = []

    # --- submission surface -------------------------------------------------
    def submit_job(self, *, entrypoint: str, submission_id: str, **kwargs: object) -> str:
        self.submitted.append({"entrypoint": entrypoint, "submission_id": submission_id, **kwargs})
        return submission_id

    def stop_job(self, submission_id: str) -> bool:
        self.stopped.append(submission_id)
        return True

    # --- derive_state surface ----------------------------------------------
    def list_jobs(self) -> list[_FakeJobDetails]:
        return self.jobs

    def get_job_info(self, submission_id: str) -> _FakeJobDetails | None:
        for j in self.jobs:
            if j.submission_id == submission_id:
                return j
        return None


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """One CACHED batch in chunk 1 — eligible for HTR submission."""
    db = tmp_path / "batches.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(
            Batch(
                batch_id="VOL_A",
                htr_status=HtrStatus.CACHED,
                manifest_status=ManifestStatus.OK,
                page_count=30,
                cached_pages=30,
                transcribed_pages=0,
                chunk_id=1,
                chunk_total=1,
                last_synced_at="2026-01-01T00:00:00+00:00",
            )
        )
        s.commit()
    engine.dispose()
    return db


@pytest.fixture
def fake_ray() -> _FakeRayClient:
    return _FakeRayClient()


@pytest.fixture
def app(seeded_db: Path, fake_ray: _FakeRayClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    monkeypatch.setenv("RASK_VIEWER_INPUT", str(tmp_path / "in"))
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("RASK_BATCHES_DB", str(seeded_db))
    monkeypatch.setenv("RAY_DASHBOARD_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("HCP_ENDPOINT", raising=False)
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()

    # Register a throwaway custom runner so the test proves the registry — not a
    # Literal/StrEnum — is what makes a pipeline selectable end-to-end. monkeypatch
    # restores PIPELINE_SPECS after the test.
    monkeypatch.setitem(
        PIPELINE_SPECS,
        "testrunner",
        PipelineSpec(name="testrunner", label="Test runner", slot=Slot.HTR, stages=()),
    )

    from core.main import create_app

    application = create_app()
    # Inject the fake Ray client for every request (overrides app.state wiring).
    application.dependency_overrides[get_ray_client] = lambda: fake_ray
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _tagged_rayjob_id(db: Path, chunk_id: int) -> str | None:
    engine = create_engine(f"sqlite:///{db}")
    try:
        with Session(engine) as s:
            row = s.exec(select(Batch.current_rayjob_id).where(Batch.chunk_id == chunk_id).limit(1)).first()
            return row
    finally:
        engine.dispose()


def test_custom_runner_submit_200_tags_db_and_passes_entrypoint(client: TestClient, fake_ray: _FakeRayClient, seeded_db: Path) -> None:
    """3a: selecting an unregistered-in-Literal-but-registered-in-PIPELINE_SPECS
    pipeline submits successfully, the entrypoint carries `--pipeline testrunner`
    and the seeded batch, and the submission_id is tagged on the chunk's rows."""
    resp = client.post("/api/v1/chunks/1/submit", json={"pipeline": "testrunner"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pipeline"] == "testrunner"
    assert body["batches"] == ["VOL_A"]
    assert body["submission_id"].startswith("testrunner-chunk-001-of-001-")

    assert len(fake_ray.submitted) == 1
    entrypoint = cast(str, fake_ray.submitted[0]["entrypoint"])
    assert "--pipeline testrunner" in entrypoint
    assert "--batch VOL_A" in entrypoint
    assert fake_ray.submitted[0]["submission_id"] == body["submission_id"]

    # tracks_rayjob_id=True → the chunk's rows now carry the submission_id.
    assert _tagged_rayjob_id(seeded_db, 1) == body["submission_id"]


def test_custom_runner_unknown_pipeline_422(client: TestClient) -> None:
    """3b: a name that's in neither the registry nor the body's validator → 422."""
    resp = client.post("/api/v1/chunks/1/submit", json={"pipeline": "nope"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == 422
    assert any(e["field"].endswith("pipeline") for e in body["errors"])


def test_concurrent_submits_same_lane_allowed(client: TestClient, fake_ray: _FakeRayClient) -> None:
    """3c: no slot cap — two submits into the same (HTR) lane BOTH succeed.
    Concurrency is delegated to Ray/Kueue; the old slot guard would 409 the
    second. (The orchestrator's per-chunk in-flight guard is what prevents the
    auto-loop from re-submitting a running chunk — see derive_state.)"""
    r1 = client.post("/api/v1/chunks/1/submit", json={"pipeline": "testrunner"})
    r2 = client.post("/api/v1/chunks/1/submit", json={"pipeline": "testrunner"})
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert len(fake_ray.submitted) == 2


def test_two_pipelines_same_chunk_both_submit(client: TestClient, fake_ray: _FakeRayClient) -> None:
    """3c': the owner's scenario — htr AND htrflow on the SAME chunk both submit
    (no 409), producing two Ray jobs with distinct pipeline-name prefixes."""
    r1 = client.post("/api/v1/chunks/1/submit", json={"pipeline": "htr"})
    r2 = client.post("/api/v1/chunks/1/submit", json={"pipeline": "htrflow"})
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert len(fake_ray.submitted) == 2
    prefixes = {cast(str, s["submission_id"]).split("-chunk-")[0] for s in fake_ray.submitted}
    assert prefixes == {"htr", "htrflow"}


def test_custom_runner_stop_200_clears_markers(client: TestClient, fake_ray: _FakeRayClient, seeded_db: Path) -> None:
    """3d: submit then stop — stop_job is called with the tagged submission_id
    and the DB markers are cleared."""
    submit = client.post("/api/v1/chunks/1/submit", json={"pipeline": "testrunner"})
    assert submit.status_code == 200
    sid = submit.json()["submission_id"]

    stop = client.post("/api/v1/chunks/1/stop")
    assert stop.status_code == 200, stop.text
    body = stop.json()
    assert body["stopped"] is True
    assert body["stopped_submission_id"] == sid
    assert fake_ray.stopped == [sid]
    assert _tagged_rayjob_id(seeded_db, 1) is None


def test_prefetch_can_be_stopped_via_http(client: TestClient, fake_ray: _FakeRayClient, seeded_db: Path) -> None:
    """The prefetch-stop bug fix, exercised through the real endpoints: submitting
    a prefetch chunk tags current_rayjob_id (tracks_rayjob_id=True), so the stop
    endpoint finds a running job instead of raising 'no running job'."""
    submit = client.post("/api/v1/chunks/1/submit", json={"pipeline": "prefetch"})
    assert submit.status_code == 200, submit.text
    sid = submit.json()["submission_id"]
    assert sid.startswith("prefetch-")
    assert _tagged_rayjob_id(seeded_db, 1) == sid

    stop = client.post("/api/v1/chunks/1/stop")
    assert stop.status_code == 200, stop.text
    assert stop.json()["stopped_submission_id"] == sid
    assert fake_ray.stopped == [sid]


# ── audit-fix regression guards: error paths return RFC 9457, not 500/404-by-luck ──


def test_submit_unknown_chunk_returns_404(client: TestClient, fake_ray: _FakeRayClient) -> None:
    """A chunk with no manifest-ok batches is a client condition → 404 (NotFoundError),
    not a generic 500. Guards the submission.py ValueError-as-500 fix; nothing is submitted."""
    resp = client.post("/api/v1/chunks/999/submit", json={"pipeline": "testrunner"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["status"] == 404
    assert fake_ray.submitted == []


def test_stop_chunk_with_no_running_job_returns_404(client: TestClient) -> None:
    """Stopping a chunk that was never submitted (no current_rayjob_id) → 404, not a 500.
    Guards the stop_chunk ValueError-as-500 fix."""
    resp = client.post("/api/v1/chunks/1/stop")
    assert resp.status_code == 404, resp.text
    assert resp.json()["status"] == 404


def test_non_positive_chunk_id_rejected_at_boundary(client: TestClient, fake_ray: _FakeRayClient) -> None:
    """chunk_id is 1-based; Path(ge=1) rejects 0/negative with 422 at the boundary,
    before the service runs (which would otherwise surface as a 500)."""
    submit = client.post("/api/v1/chunks/0/submit", json={"pipeline": "testrunner"})
    stop = client.post("/api/v1/chunks/0/stop")
    assert submit.status_code == 422, submit.text
    assert stop.status_code == 422, stop.text
    assert fake_ray.submitted == []


def test_htr_http_spec_is_http_kind_with_boto3() -> None:
    spec = PIPELINE_SPECS["htr_http"]
    assert spec.entrypoint_kind == "http"
    assert spec.slot is Slot.HTR
    assert spec.pip == ("boto3",)
    assert spec.stages == ()


def test_runner_kind_specs_match_runner_pipelines() -> None:
    runner_kind = {k for k, v in PIPELINE_SPECS.items() if v.entrypoint_kind == "runner"}
    assert runner_kind == _EXPECTED_RUNNER_PIPELINES
    runner_pipelines = pytest.importorskip("runner.pipeline", reason="runner not on the core test path").PIPELINES
    assert runner_kind == set(runner_pipelines)


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
        "python scripts/htr_chunk_job.py \\\n  --cache-bucket images-batch \\\n  --output s3://images-batch-alto \\\n  --batch VOL_A \\\n  --batch VOL_B"
    )


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
        "uv run --project runners/htr runner \\\n"
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
