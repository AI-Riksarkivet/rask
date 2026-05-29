"""Phase-1 registry tests: pipeline identity is single-sourced, htrflow telemetry
is correct, the chunk-submit body validates against the registry, and prefetch
jobs can be stopped.

Pure-function tests (registry resolution, derive helpers, the SubmitRequest
validator) need no I/O. The submit/stop tests run against an in-memory async
sqlite with a fake Ray client so the prefetch-stop fix is exercised end-to-end.
"""

from collections.abc import AsyncIterator
from typing import cast

import pytest
import pytest_asyncio
from pydantic import ValidationError
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
