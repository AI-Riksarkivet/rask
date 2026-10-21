"""Typed response schemas for the lineage API (Pydantic, snake_case)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EventRecord(BaseModel):
    """One ingested OpenLineage event, Marquez-style (summary + the full event with facets)."""

    seq: int
    event_type: str | None = None
    event_time: str | None = None
    job: str | None = None
    author: str | None = None
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    #: The uuids of the governed tables the event names and of their namespaces (ADR 0173 §10): a table's identity
    #: across a rename, which its name is not.
    table_uuids: list[str] = Field(default_factory=list)
    namespace_uuids: list[str] = Field(default_factory=list)
    #: The governed tables the event named that resolved to no uuid, which a scoped read matches by name.
    unresolved_tables: list[str] = Field(default_factory=list)
    event: dict[str, Any]


class Events(BaseModel):
    """A page of the ingested OpenLineage events: oldest first after ``since``, else newest first.

    ``next_cursor`` after ``?since=``: the ``since`` to pass next, never ``None``; equal to the one sent when
    nothing new is visible. Without ``since`` it pages OLDER as ``?after=`` (keyset over ``seq``), and
    ``None`` means no rows below this page.
    """

    events: list[EventRecord]
    next_cursor: int | None = None
    #: The oldest seq the feed still RETAINS. ``None`` = an empty feed.
    #:
    #: Retention prunes the feed, so a consumer whose cursor has fallen below this number may have lost rows
    #: before reading them; a seq the feed never filled looks the same, so it is a possible loss, not proof.
    #: Lineage cannot make the comparison itself: the consumer's cursor lives in its own service's state store.
    #: So the floor is published and each reader draws its own conclusion.
    oldest_seq: int | None = None


class ProjectedEvent(BaseModel):
    """One feed row as the unfiltered projection serves it: its seq, and the event cut to what targeting reads."""

    seq: int
    event: dict[str, Any]


class FeedProjection(BaseModel):
    """A page of the run feed with no per-dataset filter ([[CTL-021]]).

    The cursors and the floor mean exactly what they mean on :class:`Events`, so a walk over either
    behaves the same; what differs is reach (every warehouse) and width (``ProjectedEvent.event``).
    """

    events: list[ProjectedEvent]
    next_cursor: int | None = None
    oldest_seq: int | None = None


class DerivedTable(BaseModel):
    """A table derived from the source, and how many derivation hops from it: 1 for a table a run wrote from the source.

    ``table`` is the name the table's newest edge was recorded under, and ``uuid`` the uuid the catalog keys it by, or
    ``None`` for a table whose events named none.
    """

    table: str
    uuid: str | None = None
    depth: int


class Derived(BaseModel):
    """Every table derived from ``table`` that the caller may see, nearest first.

    ``complete`` is false when the walk stopped at its depth with tables still to follow, or when a derived table is
    hidden from the caller, so an answer that is not everything says so.
    """

    table: str
    #: The uuid ``table`` resolved to, which the walk started from; ``None`` when the name names no table the caller may
    #: see, and the walk started from the edges recorded under the name.
    uuid: str | None = None
    derived: list[DerivedTable]
    complete: bool


class DlqEvent(BaseModel):
    """One staged-but-not-yet-drained lineage event in the transactional outbox (#83 ops view).

    A file's presence in the outbox means "committed write, lineage not yet confirmed delivered" — the
    at-risk set the relay drains. ``parseable=False`` marks a poison object (the relay would drop it): its
    run_id is the filename, the rest is unknown. A catalog DDL change (a ``DatasetEvent``) names no run, so it
    lists under its staged key too, with its event time and dataset but no type, job or inputs.
    """

    run_id: str
    event_type: str | None = None
    event_time: str | None = None
    job: str | None = None
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    parseable: bool = True


class DlqBacklog(BaseModel):
    """The outbox saturation snapshot + the visible at-risk events (#83).

    ``depth`` / ``oldest_age_seconds`` are the RAW system-health signal (the same numbers the
    ``outbox.depth`` gauge alerts on); ``events`` is the governed subset the caller may see (≤ depth when
    per-dataset filtering drops events for datasets outside the caller's reach), capped at ``limit``.
    """

    depth: int
    oldest_age_seconds: float
    events: list[DlqEvent]
    limit: int


class DlqReplayResponse(BaseModel):
    """The result of re-ingesting one staged event (#83): recorded under its key, then dropped."""

    status: str
    run_id: str
