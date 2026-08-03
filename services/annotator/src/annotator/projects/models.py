"""The annotation-project domain: four documents, two state machines, one publish record.

Server-side counterpart of `frontend/packages/labeling`'s `LabelOp` model. Spec:
`OPEN-WORK.md#design--annotation-projects` §4 (entities) and §5 (state machines).

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
from typing import Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


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


TemplateKind = Literal[
    "bbox-detection",
    "segmentation",
    "classification",
    "text-span",
    "transcription",
    "doc-qa",
    "reading-order",
]
AttrType = Literal["free", "int", "enum"]


class OutputAttr(BaseModel):
    """One typed attribute the template requires on every submitted shape.

    Typed here, validated at SUBMIT in the task actor — the same never-trust-the-client posture as
    `review_required`: a client that could skip the template's output block would publish items
    that do not carry what the task promised downstream."""

    #: `extra="forbid"` because every enforcement-bearing field here DEFAULTS PERMISSIVE: a typo in
    #: `required` (or, on the template, in `enforce`) would otherwise be dropped silently and the
    #: create still answer 201, leaving a project that advertises a contract it does not apply.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    type: AttrType = "free"
    #: `enum` only — the closed set of legal values.
    choices: list[str] = Field(default_factory=list)
    required: bool = False


class TaskTemplate(BaseModel):
    """The labeling task's SHAPE, declaratively (the Label-Studio-config equivalent, v1).

    Deliberately flat — no nesting, no conditionals: `kind` names the task for humans and presets,
    `tools` closes the set of shape types a submit may contain, `required_labels` are the labels
    every completed item must carry at least once, `attributes` the typed fields each shape must
    answer. The default template is exactly today's unconstrained behavior, so existing projects
    are untouched."""

    model_config = ConfigDict(extra="forbid")

    kind: TemplateKind = "bbox-detection"
    modality: MediaKind = "image"
    tools: list[ShapeType] = Field(default_factory=lambda: [cast(ShapeType, "bbox")])
    required_labels: list[str] = Field(default_factory=list)
    attributes: list[OutputAttr] = Field(default_factory=list)
    #: Unconstrained escape hatch: True (the default-model case) skips tool/label enforcement so a
    #: template-less project behaves exactly as before templates existed.
    enforce: bool = False
    #: An enforced template refuses an EMPTY submission by default. Every other rule below is a
    #: per-shape or per-label test, so zero shapes satisfies all of them vacuously — claim, submit,
    #: done, without drawing anything. A blank page is a real archival outcome, so it stays
    #: expressible; it just has to be declared rather than fallen into.
    allow_empty: bool = False


def validate_against_template(template: TaskTemplate, shapes: list[Shape]) -> str | None:
    """The template's output contract, as ONE pure function: the first violation, or None.

    Pure and shared so the actor's refusal and any test speak the same words. Violations NAME the
    rule and the offender — a 409 nobody can act on is not enforcement."""
    if not template.enforce:
        return None
    if not shapes and not template.allow_empty:
        return f"template {template.kind} is enforced — a submission must carry at least one shape (set allow_empty to accept blank items)"
    allowed = set(template.tools)
    for shape in shapes:
        if shape.shape_type not in allowed:
            return f"template {template.kind} allows tools {sorted(allowed)} — shape {shape.shape_id} is {shape.shape_type}"
    present = {s.label for s in shapes if s.label}
    missing = [label for label in template.required_labels if label not in present]
    if missing:
        return f"template {template.kind} requires labels {missing} — none of the submitted shapes carry them"
    for attr in template.attributes:
        for shape in shapes:
            value = shape.attributes.get(attr.name)
            if value is None or value == "":
                # `required` governs PRESENCE ONLY. The type rules below apply to whatever is
                # actually there — an optional `enum` still declares a closed set, and an optional
                # `int` is still an int. Skipping the whole attribute here (the original shape of
                # this loop) let 'GARBAGE' sit in a column whose facet advertises `choices`.
                if attr.required:
                    return f"template {template.kind} requires attribute {attr.name!r} on every shape — shape {shape.shape_id} lacks it"
                continue
            if attr.type == "int":
                try:
                    int(value)
                except ValueError:
                    return f"attribute {attr.name!r} must be an integer — shape {shape.shape_id} carries {value!r}"
            elif attr.type == "enum" and value not in attr.choices:
                return f"attribute {attr.name!r} must be one of {attr.choices} — shape {shape.shape_id} carries {value!r}"
    return None


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
    #: The source dataset's version AT SEND TIME — §4.5's reproducibility capture, informational
    #: for lineage only, never load-bearing for correctness. When every published item shares one
    #: dataset at one captured version, the publish pins it (`publish.source_pin`) and the CREATE
    #: RunEvent gains its READ edge; anything mixed or uncaptured pins nothing — a fabricated pin
    #: is a lie (§7.2).
    dataset_version: int | None = None


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


class Adjudication(BaseModel):
    """Consensus v1's manager merge — a PICK, never a blend.

    The manager names ONE accepted replica of a group as canonical; every replica's rows still
    publish, and the facet carries this pick with its attribution. Synthesizing a merged shape set
    would put words in annotators' mouths — a downstream consumer that wants the canonical rows
    filters by the picked `task_id`."""

    task_id: str
    by: str
    at: datetime


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
    #: Captured from the PROJECT at send time. It decides whether `submit` lands in `in_review` or
    #: straight in `accepted`, so it must never be a request field: an annotator who could pass it
    #: would self-accept and walk straight past review — the one guarantee this whole plane exists
    #: to provide.
    review_required: bool = True
    #: Also captured from the project at send time (§4.1 `lease_seconds`). The claim path reads THIS
    #: when the request names no duration — before the capture existed the project setting was
    #: stored and never read, and every claim ran on the client's default.
    lease_seconds: int = 1800
    #: Captured from the project at send (like `review_required`/`lease_seconds`): the template the
    #: SUBMIT is validated against, in the actor, for any caller.
    template: TaskTemplate = Field(default_factory=TaskTemplate)
    #: Consensus v1: the replica GROUP this item belongs to (the group id shared by its siblings),
    #: or None for an ordinary item. Sibling ids are deterministic (`{group}-r{k}`), which is what
    #: lets the one-replica-per-annotator guard find them without an index.
    replica_of: str | None = None
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
    #: Annotator-facing labeling instructions (how to label), distinct from `description` (what and
    #: why). Rendered on the detail page and the canvas handoff — the LS-parity "instructions page".
    instructions: str = ""
    state: ProjectState = ProjectState.DRAFT
    label_schema: LabelSchema = Field(default_factory=LabelSchema)
    review_required: bool = True
    lease_seconds: int = 1800
    #: Consensus v1: how many INDEPENDENT annotators label each sent item. N>1 makes `send` seed N
    #: replica items per source item (`Task.replica_of` groups them); one annotator may hold at
    #: most one replica of a group. The publish emits every accepted replica's rows and reports
    #: agreement COUNTS in the run facet — merging is a manager step, deliberately not built yet.
    consensus_n: int = Field(default=1, ge=1, le=5)
    #: The labeling task's declarative SHAPE (v1 template). Captured onto every item at send —
    #: the enforcement reads the ITEM's copy, so mid-flight template edits cannot retroactively
    #: invalidate work already in review.
    template: TaskTemplate = Field(default_factory=TaskTemplate)
    skip_policy: SkipPolicy = "requeue_for_others"
    #: Consensus v1's merge step: replica group id → the manager's canonical pick. Pre-publish
    #: metadata (re-pickable while the project is adjudicable); the publish validates every pick
    #: still names an ACCEPTED replica and stamps the map into the run facet.
    adjudications: dict[str, Adjudication] = Field(default_factory=dict)
    counts: dict[TaskState, int] = Field(default_factory=dict)
    lead_time_seconds_total: float = 0.0
    created_at: datetime | None = None
    created_by: str | None = None
    updated_at: datetime | None = None
    published: PublishRecord | None = None
    publish_error: str | None = None
    #: The idempotency token for the publish saga, minted at the `publish` transition and REUSED by
    #: every retry. It exists so the saga's non-idempotent step — creating the table — becomes
    #: idempotent by key: the table id is derived from it, so a retry after a crash either finds the
    #: table it already created or creates it, but never makes a second one. `PublishRecord.publish_id`
    #: is the same value, copied on success; this field is what a RETRY reads, and it must therefore
    #: outlive a failure rather than being cleared with `publish_error`.
    pending_publish_id: str | None = None
    #: The publish INSTANT, minted with the token and reused by every retry — for the same reason.
    #: `published_at` is written into every published row and into `PublishRecord`, so stamping it
    #: per ATTEMPT would make a retried publish produce different rows than the attempt that crashed,
    #: and the "a replay is byte-identical" property the whole idempotency argument rests on would be
    #: false in exactly the case it is supposed to cover.
    pending_publish_at: datetime | None = None
    #: The TARGET namespace, pinned with the token. The endpoint authorized the publish against this
    #: namespace (§6.2 door 2), and the table id derives from it — so a crash-recovered saga reads it
    #: here rather than guessing, and a retry naming a different namespace is refused (a different
    #: namespace means a different table id, i.e. a second table for one logical publish).
    pending_target_namespace: str | None = None
    #: Who is driving the publish — re-stated on every `publish` fire (a retry may be driven by
    #: someone else, and `PublishRecord.published_by` should name the person whose action produced
    #: the publish that succeeded). Rows are unaffected: no published column carries this value, so
    #: updating it per retry does not break the byte-identical-replay property.
    pending_publish_by: str | None = None
    #: The saga's current step while PUBLISHING — A4's "surface where a publish is, not just a
    #: spinner". Written by `note_progress`, cleared when a publish starts (retry) or succeeds; on
    #: `publish_failed` it is KEPT: it names the step that was running when the attempt died.
    publish_progress: str | None = None

    @property
    def fga_object(self) -> str:
        """The FGA object id this project authorizes against."""
        return f"annotation_project:{self.project_id}"

    @property
    def fga_parent(self) -> str:
        """The create-on-parent door: the tenant, checked before the child exists."""
        return f"project:{self.tenant}"
