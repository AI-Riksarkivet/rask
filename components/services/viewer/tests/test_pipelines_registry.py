"""Phase-1 registry tests: pipeline identity is single-sourced, htrflow telemetry
is correct, the chunk-submit body validates against the registry, and prefetch
jobs can be stopped.

Pure-function tests (registry resolution, derive helpers, the SubmitRequest
validator) need no I/O. The submit/stop tests run against an in-memory async
sqlite with a fake Ray client so the prefetch-stop fix is exercised end-to-end.
"""

import time
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import cast

import httpx
import pytest
import pytest_asyncio
from lancedb.table import AsyncTable
from pydantic import ValidationError
from ray.dashboard.modules.job.common import JobStatus
from ray.job_submission import JobSubmissionClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.core.config import RunnerParams
from viewer.models.batch import Batch
from viewer.models.enums import ManifestStatus, RayStage
from viewer.models.pipelines import DEFAULT_PIPELINE, PIPELINE_SPECS, Slot, spec_for_submission_id
from viewer.schemas.chunk import SubmitRequest
from viewer.services import submission as submission_service
from viewer.services.discover import search as search_service
from viewer.services.orchestrator import derive


# ── Registry: single source of truth + htrflow telemetry fix ───────────────


def test_registry_keys_match_spec_names() -> None:
    for key, spec in PIPELINE_SPECS.items():
        assert spec.name == key


def test_htrflow_is_htr_slot_with_no_actor_stages() -> None:
    """The telemetry bug fix: htrflow shares the HTR lane but carries NO
    actor-per-stage names, so it is never queried against PageLoaderActor/etc."""
    spec = PIPELINE_SPECS["htrflow"]
    assert spec.slot is Slot.HTR
    assert spec.stages == ()


def test_htr_keeps_actor_per_stage_telemetry() -> None:
    assert PIPELINE_SPECS["htr"].slot is Slot.HTR
    assert RayStage.PAGE_LOADER in PIPELINE_SPECS["htr"].stages


def test_prefetch_tracks_rayjob_id_so_it_can_be_stopped() -> None:
    assert PIPELINE_SPECS["prefetch"].slot is Slot.PREFETCH
    assert PIPELINE_SPECS["prefetch"].tracks_rayjob_id is True


@pytest.mark.parametrize(
    ("submission_id", "expected_name"),
    [
        ("prefetch-chunk-001-of-002-20260101T000000", "prefetch"),
        ("htr-chunk-001-of-002-20260101T000000", "htr"),
        ("htrflow-chunk-001-of-002-20260101T000000", "htrflow"),
        ("fake-chunk-001-of-002-20260101T000000", "fake"),
    ],
)
def test_spec_for_submission_id_resolves_prefix(submission_id: str, expected_name: str) -> None:
    spec = spec_for_submission_id(submission_id)
    assert spec is not None
    assert spec.name == expected_name


def test_spec_for_submission_id_unknown_is_none() -> None:
    assert spec_for_submission_id("mystery-chunk-001-of-002-x") is None


# ── derive helpers: slot + per-job stage resolution ────────────────────────


def test_slot_for_defaults_unknown_to_htr() -> None:
    assert derive._slot_for("mystery-chunk-001-of-002-x") is Slot.HTR
    assert derive._slot_for("prefetch-chunk-001-of-002-x") is Slot.PREFETCH
    assert derive._slot_for("htrflow-chunk-001-of-002-x") is Slot.HTR


def test_stages_for_htrflow_is_empty_not_htr_actors() -> None:
    """An htrflow job's telemetry uses htrflow's (empty) stages, NOT the htr
    actor names — the bug this phase fixes."""
    assert derive._stages_for("htrflow-chunk-001-of-002-x") == ()
    assert RayStage.PAGE_LOADER in derive._stages_for("htr-chunk-001-of-002-x")
    assert derive._stages_for("prefetch-chunk-001-of-002-x") == (RayStage.PREFETCH,)


# ── SubmitRequest validation (→ 422 at the HTTP boundary) ──────────────────


def test_submit_request_defaults_to_default_pipeline() -> None:
    assert SubmitRequest().pipeline == DEFAULT_PIPELINE


def test_submit_request_accepts_registered_pipeline() -> None:
    assert SubmitRequest(pipeline="htrflow").pipeline == "htrflow"


def test_submit_request_rejects_unknown_pipeline() -> None:
    with pytest.raises(ValidationError):
        SubmitRequest(pipeline="nope")


# ── prefetch-stop fix: submit tags the rayjob id, stop clears it ───────────


class _FakeRayClient:
    """Records submit_job / stop_job calls; no Ray dependency."""

    def __init__(self) -> None:
        self.stopped: list[str] = []

    def submit_job(self, *, submission_id: str, **_: object) -> str:
        return submission_id

    def stop_job(self, submission_id: str) -> bool:
        self.stopped.append(submission_id)
        return True


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine) as s:
        s.add(
            Batch(
                batch_id="B1",
                manifest_status=ManifestStatus.OK,
                page_count=10,
                chunk_id=1,
                chunk_total=1,
                last_synced_at="2026-01-01T00:00:00+00:00",
            )
        )
        await s.commit()
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_prefetch_can_be_submitted_then_stopped(session: AsyncSession, tmp_path) -> None:
    fake = _FakeRayClient()
    # _FakeRayClient is a structural stand-in for JobSubmissionClient (only
    # submit_job/stop_job are exercised); cast keeps the typed signature honest.
    client = cast(JobSubmissionClient, fake)
    result = await submission_service.submit_chunk(
        session,
        client,
        chunk_id=1,
        params=RunnerParams(repo_root=tmp_path, cache_bucket="cache", output="s3://out", iiif_url="https://iiif"),
        spec=PIPELINE_SPECS["prefetch"],
        env={},
    )
    assert result.pipeline == "prefetch"
    assert result.submission_id.startswith("prefetch-")

    # The prefetch-stop bug was that current_rayjob_id was never tagged for
    # prefetch, so stop_chunk raised "no running job". With tracks_rayjob_id=True
    # the tag exists and stop succeeds.
    stop = await submission_service.stop_chunk(session, client, chunk_id=1)
    assert stop.stopped is True
    assert stop.stopped_submission_id == result.submission_id
    assert fake.stopped == [result.submission_id]


# ── thumb_url carries the api_prefix (regression guard for the missing-/v1 bug) ──


class _FakeLinesTbl:
    """Async LanceDB table stand-in: the query() chain returns the seeded rows."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def query(self) -> "_FakeLinesTbl":
        return self

    def nearest_to_text(self, query: str, columns: str) -> "_FakeLinesTbl":
        return self

    def select(self, cols: list[str]) -> "_FakeLinesTbl":
        return self

    def limit(self, n: int) -> "_FakeLinesTbl":
        return self

    async def to_list(self, timeout: timedelta) -> list[dict[str, object]]:
        return self._rows


@pytest.mark.asyncio
async def test_search_thumb_url_includes_api_prefix() -> None:
    """The bug emitted /api/search/thumb/... (no /v1) so every line thumbnail 404'd
    through the SPA. The URL must be built from settings.api_prefix."""
    tbl = _FakeLinesTbl([{"batch_id": "VOL_A", "text": "hej", "thumb_key": "thumbs/VOL_A/0001.jpg"}])
    resp = await search_service.search_lines(cast(AsyncTable, tbl), "hej", 10, timedelta(seconds=5), api_prefix="/api/v1")
    assert resp.hits[0].thumb_url == "/api/v1/search/thumb/thumbs/VOL_A/0001.jpg"


# ── derive_state eligibility: eligible = ready - in-flight - cooldown ──────


class _FakeJob:
    """Minimal Ray JobDetails stand-in for the list_jobs surface derive reads."""

    def __init__(self, submission_id: str, status: JobStatus, *, start_time: int = 0, end_time: int | None = None) -> None:
        self.submission_id = submission_id
        self.status = status
        self.entrypoint = ""
        self.start_time = start_time
        self.end_time = end_time

    def dict(self) -> dict[str, object]:
        return {
            "submission_id": self.submission_id,
            "status": self.status,
            "entrypoint": self.entrypoint,
            "start_time": self.start_time,
            "end_time": self.end_time,
        }


class _FakeDeriveClient:
    """list_jobs returns configurable jobs; get_job_info → None (skip per-stage telemetry)."""

    def __init__(self, jobs: list[_FakeJob]) -> None:
        self._jobs = jobs

    def list_jobs(self) -> list[_FakeJob]:
        return self._jobs

    def get_job_info(self, submission_id: str) -> None:
        return None


@pytest_asyncio.fixture
async def derive_session() -> AsyncIterator[AsyncSession]:
    """chunk 2 prefetch-pending (cached < page_count); chunk 3 HTR-ready (fully cached, untranscribed)."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine) as s:
        s.add(
            Batch(
                batch_id="B2", manifest_status=ManifestStatus.OK, page_count=40, cached_pages=20,
                transcribed_pages=0, chunk_id=2, chunk_total=1, last_synced_at="2026-01-01T00:00:00+00:00",
            )
        )
        s.add(
            Batch(
                batch_id="B3", manifest_status=ManifestStatus.OK, page_count=10, cached_pages=10,
                transcribed_pages=0, chunk_id=3, chunk_total=1, last_synced_at="2026-01-01T00:00:00+00:00",
            )
        )
        await s.commit()
        yield s
    await engine.dispose()


async def _derive_state(jobs: list[_FakeJob], session: AsyncSession) -> derive.OrchestratorState:
    async with httpx.AsyncClient() as http:
        return await derive.derive_state(
            http=http,
            client=cast(JobSubmissionClient, _FakeDeriveClient(jobs)),
            dashboard_url="http://127.0.0.1:1",
            session=session,
        )


@pytest.mark.asyncio
async def test_derive_eligibility_baseline(derive_session: AsyncSession) -> None:
    """No Ray jobs: chunk 2 is prefetch-eligible, chunk 3 is htr-eligible."""
    state = await _derive_state([], derive_session)
    assert state.ok
    assert state.prefetch is not None and state.prefetch.eligible == [2]
    assert state.htr is not None and state.htr.eligible == [3]


@pytest.mark.asyncio
async def test_derive_eligibility_excludes_in_flight(derive_session: AsyncSession) -> None:
    """A chunk with an active RUNNING job in its lane is excluded from eligible."""
    jobs = [
        _FakeJob("prefetch-chunk-002-of-001-20260101T000000", JobStatus.RUNNING),
        _FakeJob("htr-chunk-003-of-001-20260101T000000", JobStatus.RUNNING),
    ]
    state = await _derive_state(jobs, derive_session)
    assert state.prefetch is not None and state.prefetch.eligible == []
    assert state.htr is not None and state.htr.eligible == []


@pytest.mark.asyncio
async def test_derive_eligibility_excludes_cooldown(derive_session: AsyncSession) -> None:
    """A chunk whose recent job FAILED within the 600s cooldown is excluded, and a Cooldown is recorded."""
    now_ms = int(time.time() * 1000)
    jobs = [_FakeJob("htr-chunk-003-of-001-20260101T000000", JobStatus.FAILED, end_time=now_ms)]
    state = await _derive_state(jobs, derive_session)
    assert state.htr is not None and 3 not in state.htr.eligible
    assert any(c.chunk_id == 3 for c in state.cooldowns)
