"""The annotation-project domain: four documents, two state machines, one publish record.

Server-side counterpart of `frontend/packages/labeling`'s `LabelOp` model. Spec:
`docs/DESIGN-annotation-projects.md` §4 (entities) and §5 (state machines).

Deliberately store-free and corpus-free. This module imports nothing from the lakehouse plane — no
`lancekit`, no registry, no dataset handle — because an annotation project's state (tasks, claims,
drafts, reviews) is the annotator's own and never enters the governed plane until a publish. The
import guard in `tests/unit/test_annotation_projects_machine.py` makes that mechanical rather than a
promise, so slices S1/S3 are buildable before any actor store exists.

Ids are `uuid4().hex`, not UUID7: the deployed interpreter is CPython 3.13 (`uuid.uuid7` is absent)
and ordering comes from `created_at` plus the index the project actor maintains, so a ULID dependency
would buy nothing.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def new_id() -> str:
    """A fresh document id (uuid4 hex, per §4)."""
    return uuid4().hex


class ProjectState(StrEnum):
    """§5.1. `publish_failed` is a distinct resting state so a retry is a legal edge, not a reset."""

    DRAFT = "draft"
    LABELING = "labeling"
    FROZEN = "frozen"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"
    ARCHIVED = "archived"


class TaskState(StrEnum):
    """§5.2 — one axis, six states. A task is in exactly one of these, always."""

    UNASSIGNED = "unassigned"
    CLAIMED = "claimed"
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    ACCEPTED = "accepted"
    SKIPPED = "skipped"


#: Publishing requires EVERY task terminal (§5.1). A task in `in_review` blocks it — the owner's
#: "nothing lands before that", enforced rather than described.
TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset({TaskState.ACCEPTED, TaskState.SKIPPED})

SkipPolicy = Literal["requeue_for_others", "requeue_for_me", "terminal"]
ShapeType = Literal["bbox", "polygon", "mask", "segment", "tag", "text"]
ReviewAction = Literal["accepted", "fix_and_accept", "request_changes"]
DraftOrigin = Literal["human", "model", "propagated"]
MediaKind = Literal["image", "audio", "video"]


class LabelClass(BaseModel):
    """One class in a project's taxonomy."""

    name: str
    colour: str | None = None
    shape_types: list[ShapeType] = Field(default_factory=list)


class LabelSchema(BaseModel):
    """The taxonomy for a project (§4.1). A managed taxonomy plugs in here."""

    classes: list[LabelClass] = Field(default_factory=list)
    attributes: list[str] = Field(default_factory=list)


class MediaRef(BaseModel):
    """Resolved at send time — the shape the annotator zone's `MediaUnit` already consumes."""

    kind: MediaKind
    image_url: str | None = None
    media_url: str | None = None
    width: int | None = None
    height: int | None = None


class ItemSource(BaseModel):
    """The send capture (§4.5): what selection produced this task."""

    kind: Literal["chunks", "scope", "corpus"]
    keys: list[str] = Field(default_factory=list)
    where: str | None = None


class Transition(BaseModel):
    """Append-only audit row. The FSM is insert-only: a transition is never edited or removed."""

    at: datetime
    by: str
    event: str
    from_state: str = Field(alias="from")
    to_state: str = Field(alias="to")

    #: `from` is a Python keyword, so the field is `from_state` and the WIRE name is the alias.
    #: `serialize_by_alias` makes the wire shape the default in both directions — without it a caller
    #: that forgets `by_alias=True` silently emits `from_state`/`to_state`, and the audit trail ends
    #: up with two shapes depending on which code path wrote the row.
    model_config = {"populate_by_name": True, "serialize_by_alias": True}


class ReviewNote(BaseModel):
    """Append-only reviewer feedback (§4.2)."""

    by: str
    at: datetime
    action: ReviewAction
    message: str = ""
    shape_ids: list[str] = Field(default_factory=list)


class Shape(BaseModel):
    """One annotation. Carries its own provenance so the payload stays mode-blind."""

    shape_id: str = Field(default_factory=new_id)
    shape_type: ShapeType
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    rotation: float | None = None
    polygon: list[float] = Field(default_factory=list)
    t_start: float | None = None
    t_end: float | None = None
    mask: str | None = None
    label: str | None = None
    text: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    group: str | None = None
    difficult: bool = False
    source: str | None = None
    model_version: str | None = None
    confidence: float | None = None


class Draft(BaseModel):
    """ONE document per (task, author) holding the whole shape set as a single list (§4.3).

    Not a row per shape: that is the write-amplification fix made structural. A save is one keyed
    write guarded by `revision`, so two tabs of the same annotator cannot silently lose each other's
    work — the mismatch is a 409, the same contract the Arrow transport spells `X-Annotations-Version`.
    """

    task_id: str
    project_id: str
    author: str
    shapes: list[Shape] = Field(default_factory=list)
    revision: int = 0
    updated_at: datetime | None = None
    origin: DraftOrigin = "human"


class PublishRecord(BaseModel):
    """Set ONCE, only by the publish workflow (§4.1)."""

    table_id: str
    namespace: str
    version: int
    tag: str | None = None
    publish_id: str
    published_at: datetime
    published_by: str


class Task(BaseModel):
    """§4.2. `lease_expires_at is None` while CLAIMED means manager-pinned — it never expires."""

    task_id: str = Field(default_factory=new_id)
    project_id: str
    state: TaskState = TaskState.UNASSIGNED
    assignee: str | None = None
    lease_expires_at: datetime | None = None
    source: ItemSource
    media: MediaRef
    submitted_by: str | None = None
    submitted_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_action: ReviewAction | None = None
    review_notes: list[ReviewNote] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    lead_time_seconds: float = 0.0
    skipped_reason: str | None = None


class AnnotationProject(BaseModel):
    """§4.1. `tenant` is the authz parent — the FGA object is `annotation_project:<project_id>`,
    and creating one is checked on `project:<tenant>` (`can_create_annotation_project`)."""

    project_id: str = Field(default_factory=new_id)
    tenant: str
    slug: str
    title: str = ""
    description: str = ""
    state: ProjectState = ProjectState.DRAFT
    label_schema: LabelSchema = Field(default_factory=LabelSchema)
    review_required: bool = True
    lease_seconds: int = 1800
    skip_policy: SkipPolicy = "requeue_for_others"
    counts: dict[TaskState, int] = Field(default_factory=dict)
    lead_time_seconds_total: float = 0.0
    created_at: datetime | None = None
    created_by: str | None = None
    updated_at: datetime | None = None
    published: PublishRecord | None = None
    publish_error: str | None = None

    @property
    def fga_object(self) -> str:
        """The FGA object id this project authorizes against."""
        return f"annotation_project:{self.project_id}"

    @property
    def fga_parent(self) -> str:
        """The create-on-parent door: the tenant, checked before the child exists."""
        return f"project:{self.tenant}"
