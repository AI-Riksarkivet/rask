"""The annotator's WIRE shapes — what each route publishes, declared.

Thirteen handlers were annotated `-> dict[str, Any]` with no `response_model` between them, so
`/openapi.json` described their answers as "an object" and whatever the actor document happened to
hold is what shipped (docs/DECISIONS.md "The Python estate audit" ANN-07). Two things follow from that, and the second is the
one that bites: the frontend's valibot schemas became the only statement of these shapes, on the far
side of the wire and maintained by hand; and a field added to `Task` or `AnnotationProject` for the
actor's own bookkeeping is published to every client the moment it is stored.

**The envelopes live here; the documents do not.** Where a domain model already describes the
payload — `Task`, `Draft`, `AnnotationProject` — the route publishes THAT model rather than a
parallel copy of it, because a second description of the same document is a second thing to drift.
What this module adds is only the envelopes the routes build around them, which have no home in the
domain layer: they are shapes of the HTTP surface, not of the plane.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from annotator.projects.models import AnnotationProject, Draft, Task


class LegalEvent(BaseModel):
    """One principal-fireable transition, straight from `machines.legal_*_events`.

    `permission` is the relation that gates it, so a UI can EXPLAIN a disabled action. The actual
    gate stays server-side — this is a description, never an authorization.
    """

    event: str
    to: str
    permission: str


class ProjectListing(BaseModel):
    """`GET /projects?tenant=` — the tenant's projects, fanned out from its index actor."""

    projects: list[AnnotationProject]
    total: int


class ProjectDetail(BaseModel):
    """`GET /projects/{id}` — the project plus the edges a principal may fire out of its state."""

    project: AnnotationProject
    legal_events: list[LegalEvent]


class TaskDetail(Task):
    """One task as the queue UI reads it: the document plus its own legal edges.

    Inherits `Task` rather than restating it — the extra key is the only difference, and a
    hand-copied twin of a 25-field document is a drift waiting to happen.
    """

    legal_events: list[LegalEvent] = Field(default_factory=list)


class TaskListing(BaseModel):
    """`GET /projects/{id}/tasks` — the index and the publish precondition from ONE snapshot.

    The last three are present only for `?include=details`, and their ABSENCE is load-bearing: the
    route is declared `response_model_exclude_unset=True` so the plain listing publishes exactly the
    five keys it always did. Emitting them as `null` instead would be a wire change.
    """

    tasks: dict[str, str]
    counts: dict[str, int]
    total: int
    terminal: int
    may_publish: bool
    details: list[TaskDetail] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    next_cursor: str | None = None


class SendReceipt(BaseModel):
    """`POST /projects/{id}/items` — what a send actually created.

    `created` counts the batch's per-task answers, not the send list: an item that was already
    indexed reports as not created, which is what makes a retried send readable.
    """

    sent: int
    created: int
    task_ids: list[str]


class DropReceipt(BaseModel):
    """`DELETE /projects/{id}/tasks/{task_id}` — idempotent, so `removed` may be false."""

    task_id: str
    removed: bool
    total: int


class DraftImport(BaseModel):
    """`POST /tasks/{id}/import` — what the Arrow-IPC import appended, plus the resulting draft."""

    imported: int
    links: int
    draft: Draft
