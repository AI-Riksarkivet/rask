"""Response models for the system endpoints (``/api/health``, ``/api/columns``,
``/api/documents``)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class VllmPing(BaseModel):
    """Best-effort reachability of one vLLM server (embed or rerank)."""

    ok: bool
    url: str
    error: str | None = None


class DbFacts(BaseModel):
    """Row counts + table list behind the status badge."""

    path: str
    tables: list[str]
    chunks: int
    documents: int


class HealthResponse(BaseModel):
    """Status-badge payload: both vLLM pings, plus DB facts WHEN a dataset resolves.

    ``db`` is optional because ``/api/health`` is the media plane's liveness probe and must answer 200
    on a deployment with no corpus loaded (the chart's default ``media.corpus.mode=emptyDir``). It used
    to be required, which forced the endpoint to resolve a dataset before it could answer at all — so
    an empty corpus turned the probe the media + annotator zones poll on every page load into a 404
    (live-proof 2026-07-28, defect 4). ``db_error`` carries WHY, so the badge can say "no dataset"
    instead of "backend unreachable".
    """

    db: DbFacts | None = None
    db_error: str | None = None
    embed: VllmPing
    rerank: VllmPing


class ColumnKind(StrEnum):
    """How the UI renders a filterable ``chunks`` column (drives the input type).

    A ``StrEnum`` (not a free string) so the set of filter kinds is closed and the
    frontend can switch on it exhaustively; the member serializes to its literal.
    """

    number = "number"
    boolean = "boolean"
    time = "time"
    text = "text"


class FilterColumn(BaseModel):
    """A scalar ``chunks`` column the UI may put in a WHERE filter (name + kind)."""

    name: str
    type: ColumnKind


class DocumentSummary(BaseModel):
    """One row of the documents gallery (archival metadata + duration)."""

    doc_id: str
    audio_path: str
    duration: float | None = None
    referenskod: str | None = None
    namn: str | None = None
    bildid: str | None = None
    extraid: str | None = None


class DocumentsResponse(BaseModel):
    """A page of the documents gallery."""

    total: int
    page: int
    docs: list[DocumentSummary] = Field(default_factory=list)
