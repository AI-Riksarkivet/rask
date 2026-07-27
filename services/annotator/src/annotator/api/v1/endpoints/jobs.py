"""Batch labeling jobs — enqueue a producer over a read-plane SELECTION (the 3rd mode).

The interactive modes (manual, AI-assist) write locally then merge_insert one version.
Bulk / auto-labeling instead runs a producer over a CHUNK-LEVEL selection (a lasso /
scope / corpus formed in the read plane) as a SILVER DERIVER — async, corpus-scale,
replace-protects-humans. That deriver is lance-ns's engine (lance-ray + the catalog
mover); OUR job is the thin SEAM that submits it and reports status.

Routes to a job runner (``MEDIA_JOBS_URL`` — a lance-ns RayJob submit endpoint) when set,
else a deterministic in-repo MOCK so the submit/poll round-trip is wired + testable
(drop-in for the real submitter, exactly like the assist + catalog transports). No job
runs in-repo — the mock only proves submission + status polling.
"""

import hashlib
import logging
from typing import Literal

import httpx
from common.core.exceptions import ServiceUnavailableError
from common.deps import StateDep
from common.state import AppState
from fastapi import APIRouter
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobScope(BaseModel):
    """The chunk-level target set — mirrors the frontend ChunkSelection."""

    level: Literal["chunks", "scope", "corpus"]
    keys: list[str] = Field(default_factory=list)  # level=chunks: descriptor hitKeys
    where: str | None = None  # level=scope: a SQL WHERE (scope.ts ScopeClause)


class JobRequest(BaseModel):
    """A batch-deriver submission: which producer/op to run over which selection."""

    producer: str  # grounding-dino | insid3 | vlm-judge | embed-propagate | …
    op: Literal["predict", "propagate", "judge"]
    scope: JobScope
    dataset: str | None = None
    prompt: str | None = None
    # PROPAGATE (INSID3 / embed-propagate): the few-shot REFERENCE — annotation ids of the
    # exemplar masks. The deriver reads their masks + DINOv3 features and propagates them
    # over `scope`. Empty for predict/judge. This is the seam that carries INSID3's
    # "1–few exemplars → apply to all from small data" through to the lance-ns deriver.
    exemplars: list[str] = Field(default_factory=list)


class JobResult(BaseModel):
    """Submit/poll response — the runner's view of one job."""

    job_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    backend: Literal["mock", "remote"]


def scope_size(scope: JobScope) -> int:
    """Item count for a chunk selection; -1 for scope/corpus (server-side count)."""
    return len(scope.keys) if scope.level == "chunks" else -1


def job_id_for(body: JobRequest) -> str:
    """Deterministic id from the request — a re-submit of the same op+scope+exemplars is
    idempotent (no wall-clock / randomness, so runs are reproducible). The scope's actual
    CONTENT is part of the identity (sorted keys / the WHERE predicate — a same-size but
    different selection is a different job), as are the exemplars (a different few-shot
    reference is a different propagate)."""
    scope = f"{body.scope.level}:{','.join(sorted(body.scope.keys))}:{body.scope.where or ''}"
    exemplars = ",".join(sorted(body.exemplars))
    basis = f"{body.producer}:{body.op}:{scope}:{body.dataset}:{exemplars}"
    return "job-" + hashlib.sha1(basis.encode(), usedforsecurity=False).hexdigest()[:12]


@router.post("/apply")
def apply(state: StateDep, body: JobRequest) -> JobResult:
    """Submit a batch labeling deriver over a read-plane selection."""
    n = scope_size(body.scope)
    logger.info(
        "job apply %s:%s over %s (%s items, %d exemplars)",
        body.producer,
        body.op,
        body.scope.level,
        n,
        len(body.exemplars),
    )
    url = state.settings.jobs_url
    if url:
        return _remote(state, url, body)
    return JobResult(job_id=job_id_for(body), status="queued", backend="mock")


@router.get("/{job_id}")
def status(state: StateDep, job_id: str) -> JobResult:
    """Poll a submitted job. The mock reports 'queued' deterministically; a real runner
    (``MEDIA_JOBS_URL``) reflects the deriver's live state."""
    url = state.settings.jobs_url
    if url:
        return _remote_status(state, url, job_id)
    return JobResult(job_id=job_id, status="queued", backend="mock")


def _remote(state: AppState, url: str, body: JobRequest) -> JobResult:
    """Proxy the submit to the lance-ns RayJob endpoint (WIRED, not exercised in-repo).

    A failing or misbehaving runner (HTTP error, bad JSON, missing/unknown fields)
    raises :class:`ServiceUnavailableError` — a stable 503, never a raw 500."""
    http = state.http
    if http is None:
        raise ServiceUnavailableError("jobs: HTTP client unavailable")
    try:
        resp = http.post(url, json=body.model_dump(), timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        return JobResult(job_id=str(data["job_id"]), status=data.get("status", "queued"), backend="remote")
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise ServiceUnavailableError("jobs runner error") from e


def _remote_status(state: AppState, url: str, job_id: str) -> JobResult:
    """Poll the runner; same failure contract as :func:`_remote` (503, never raw 500)."""
    http = state.http
    if http is None:
        raise ServiceUnavailableError("jobs: HTTP client unavailable")
    try:
        resp = http.get(f"{url.rstrip('/')}/{job_id}", timeout=15.0)
        resp.raise_for_status()
        return JobResult(job_id=job_id, status=resp.json().get("status", "queued"), backend="remote")
    except (httpx.HTTPError, ValueError) as e:
        raise ServiceUnavailableError("jobs runner error") from e
