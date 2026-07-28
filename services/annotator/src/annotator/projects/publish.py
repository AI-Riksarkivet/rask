"""Publish → the lakehouse: which annotations land, in what shape, and whose names travel with them.

The publish DECISION, implementing `docs/DESIGN-annotation-projects.md` §7. Deliberately pure — no
Dapr, no Lance, no catalog client — so the rules that would be expensive to get wrong in a cluster
are testable exhaustively here. **The annotator never writes Lance directly** (§7.1); a workflow
takes this plan and posts it through the catalog, which is what seeds FGA ownership and emits the
`CREATE` RunEvent.

Three rules carry the weight:

1. **Only an `accepted` task contributes SHAPES.** `skipped` is terminal, so it satisfies the publish
   precondition and the project actor correctly allows the publish — but a skipped task can still
   hold a draft, because the annotator drew shapes and only then decided the item did not belong.
   Terminal-therefore-publishable is the trap.
2. **A skipped task still contributes ONE SENTINEL ROW** (`shape_type="none"`,
   `task_outcome="skipped"`, §7.1). Dropping it entirely would be the opposite error: the project's
   *decisions* would be incomplete on the record and a consumer could not build an exclusion set.
   "No shapes" and "no row" are different claims, and only one of them is true.
3. **Attribution comes from the task, never from the payload.** `submitted_by` / `reviewed_by` are
   written by the actor from the OIDC-verified subject. The draft's `author` records who last SAVED
   — under `fix_and_accept` that is the reviewer — so the two are carried separately rather than
   collapsed into one "who did this", which would credit the wrong person in either direction.

Provenance is written in both directions, and they cannot disagree because both are projected from
one plan in one pass: the ROWS carry `annotated_by` / `reviewed_by` so a consumer answers "who
annotated this row" with no graph query, and ONE `annotationProject` run facet carries the project
totals so the graph answers "what produced this dataset".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from lineage_kit.schemas import custom_facet
from pydantic import BaseModel, Field

from annotator.projects.models import AnnotationProject, Draft, Shape, Task, TaskState


#: The only state that contributes SHAPES. Every other terminal state contributes a sentinel row.
_SHAPES_FROM: Final[frozenset[TaskState]] = frozenset({TaskState.ACCEPTED})

#: The sentinel `shape_type` for a task that produced no shapes (§7.1). A real value in the column's
#: domain, not a null — a consumer filters `shape_type != 'none'` for shapes and reads `task_outcome`
#: for coverage, and neither query has to reason about nullability.
NO_SHAPE: Final[str] = "none"

#: The publish run facet (§7.2). Named to avoid the catalog's `_RESERVED_RUN_FACETS`
#: (`lance`, `author`, `errorMessage`, `progress`, `parent`) — a collision there is rejected at emit.
PROJECT_FACET: Final[str] = "annotationProject"

#: Column order of `PUBLISHED_LABELS_SCHEMA` (§7.1). Named here so a row dict and the Arrow schema the
#: workflow builds cannot drift: the test asserts every row carries exactly these keys.
PUBLISHED_COLUMNS: Final[tuple[str, ...]] = (
    "project_id",
    "project_slug",
    "publish_id",
    "task_id",
    "task_outcome",
    "item_source_kind",
    "item_dataset",
    "item_key_path",
    "annotation_id",
    "shape_type",
    "x",
    "y",
    "width",
    "height",
    "rotation",
    "polygon",
    "t_start",
    "t_end",
    "mask",
    "label",
    "text",
    "attributes",
    "group",
    "difficult",
    "source",
    "model_version",
    "confidence",
    "annotated_by",
    "annotated_at",
    "reviewed_by",
    "reviewed_at",
    "review_action",
    "lead_time_seconds",
    "published_at",
)


class PublishRefusal(ValueError):
    """A publish that must not proceed. Raised rather than returning a partial plan — a publish that
    silently drops rows is worse than one that stops and says why."""


class Attribution(BaseModel):
    """Who produced one task's outcome, taken entirely from server-written fields."""

    task_id: str
    outcome: str
    annotated_by: str
    annotated_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_action: str | None = None
    #: Who last SAVED the shapes. Usually the annotator; under `fix_and_accept` it is the reviewer,
    #: which is exactly the case that makes collapsing these two fields a lie.
    drafted_by: str = ""
    shape_count: int = 0


class PublishPlan(BaseModel):
    """Everything the publish workflow needs, decided in one pass so the rows it writes and the
    lineage it emits cannot drift apart."""

    project_id: str
    publish_id: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    attributions: list[Attribution] = Field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return sum(1 for a in self.attributions if a.outcome == "accepted")

    @property
    def skipped_count(self) -> int:
        return sum(1 for a in self.attributions if a.outcome == "skipped")

    @property
    def shape_count(self) -> int:
        """Rows that are real shapes. Sentinels are decisions, not annotations, and counting them as
        labels would overstate every published dataset by its skip rate."""
        return sum(1 for r in self.rows if r["shape_type"] != NO_SHAPE)


def _attribution(task: Task, draft: Draft | None, outcome: str) -> Attribution:
    """Project one terminal task into an attribution row.

    `submitted_by` is required for an ACCEPTED task: accepted with nobody recorded as having
    submitted it means the audit trail is broken, and anonymous provenance in the lakehouse is worse
    than no publish — once written it is indistinguishable from the real thing. A SKIPPED task may
    legitimately have no submitter (skipped straight from a claim), so it is credited to the actor
    that skipped it, or to nobody.
    """
    if outcome == "accepted" and not task.submitted_by:
        raise PublishRefusal(f"task {task.task_id} is accepted but records no submitter — refusing to publish anonymous work")
    return Attribution(
        task_id=task.task_id,
        outcome=outcome,
        annotated_by=task.submitted_by or "",
        annotated_at=task.submitted_at,
        reviewed_by=task.reviewed_by,
        reviewed_at=task.reviewed_at,
        review_action=task.review_action,
        drafted_by=draft.author if draft else "",
        shape_count=len(draft.shapes) if draft else 0,
    )


def _row(
    project: AnnotationProject,
    task: Task,
    attribution: Attribution,
    shape: Shape | None,
    *,
    publish_id: str,
    published_at: datetime,
) -> dict[str, Any]:
    """One row of `PUBLISHED_LABELS_SCHEMA`. `shape=None` builds the sentinel."""
    source = task.source
    return {
        "project_id": project.project_id,
        "project_slug": project.slug,
        "publish_id": publish_id,
        "task_id": task.task_id,
        "task_outcome": attribution.outcome,
        "item_source_kind": source.kind,
        "item_dataset": source.where or "",
        "item_key_path": source.keys[0] if source.keys else "",
        "annotation_id": shape.shape_id if shape else "",
        "shape_type": shape.shape_type if shape else NO_SHAPE,
        "x": shape.x if shape else None,
        "y": shape.y if shape else None,
        "width": shape.width if shape else None,
        "height": shape.height if shape else None,
        "rotation": shape.rotation if shape else None,
        "polygon": list(shape.polygon) if shape else [],
        "t_start": shape.t_start if shape else None,
        "t_end": shape.t_end if shape else None,
        "mask": shape.mask if shape else None,
        "label": shape.label if shape else None,
        "text": shape.text if shape else None,
        "attributes": _json_attributes(shape),
        "group": shape.group if shape else None,
        "difficult": bool(shape.difficult) if shape else False,
        "source": (shape.source or "human") if shape else "",
        "model_version": shape.model_version if shape else None,
        "confidence": shape.confidence if shape else None,
        # Server-stamped provenance. Read off the TASK, so a client payload cannot reach it.
        # `reviewed_by` is '' rather than null when review was waived (§7.1) — the column says "no
        # reviewer", which is a fact, instead of "unknown", which is not what happened.
        "annotated_by": attribution.annotated_by,
        "annotated_at": attribution.annotated_at,
        "reviewed_by": attribution.reviewed_by or "",
        "reviewed_at": attribution.reviewed_at,
        "review_action": attribution.review_action or NO_SHAPE,
        "lead_time_seconds": task.lead_time_seconds,
        "published_at": published_at,
    }


def _json_attributes(shape: Shape | None) -> str:
    """`attributes` is a JSON string column (§7.1). Sorted keys so a replayed publish produces byte-
    identical rows — an activity that replays after a crash must not rewrite the dataset differently."""
    import json  # noqa: PLC0415 - only needed on this path

    return json.dumps(dict(sorted(shape.attributes.items())), separators=(",", ":")) if shape else "{}"


def build_plan(
    project: AnnotationProject,
    pairs: list[tuple[Task, Draft | None]],
    *,
    publish_id: str,
    published_at: datetime,
) -> PublishPlan:
    """Decide the whole publish in one pass.

    `pairs` is every task in the project with its draft (or `None`). A non-terminal task is a
    programming error at this point — the project actor's precondition already refused the publish —
    so it is refused loudly rather than skipped quietly.
    """
    plan = PublishPlan(project_id=project.project_id, publish_id=publish_id)
    for task, draft in pairs:
        if task.state is TaskState.ACCEPTED:
            attribution = _attribution(task, draft, "accepted")
            plan.attributions.append(attribution)
            shapes = draft.shapes if draft else []
            if not shapes:
                # Accepted with no shapes is a real decision ("nothing to annotate here"), and it is
                # recorded as one rather than vanishing.
                plan.rows.append(_row(project, task, attribution, None, publish_id=publish_id, published_at=published_at))
            for shape in shapes:
                plan.rows.append(_row(project, task, attribution, shape, publish_id=publish_id, published_at=published_at))
        elif task.state is TaskState.SKIPPED:
            # ONE sentinel row, and NONE of the draft's shapes (§7.1).
            attribution = _attribution(task, draft, "skipped")
            plan.attributions.append(attribution)
            plan.rows.append(_row(project, task, attribution, None, publish_id=publish_id, published_at=published_at))
        else:
            raise PublishRefusal(f"task {task.task_id} is {task.state}, not terminal — the publish precondition was not met")
    return plan


def project_facet(project: AnnotationProject, plan: PublishPlan, *, frozen_at: datetime | None = None) -> dict[str, Any]:
    """The `annotationProject` run facet (§7.2) — every key a fact the project store already holds.

    Counts rather than name lists: the NAMES are on every row (`annotated_by`, `reviewed_by`), so
    putting them here too would be a second copy that can disagree with the first. Sorted and derived
    so a replayed publish emits an identical facet — two replays producing different payloads would
    read as two provenances for one publish.
    """
    annotators = {a.annotated_by for a in plan.attributions if a.annotated_by}
    reviewers = {a.reviewed_by for a in plan.attributions if a.reviewed_by}
    # Counted per TASK, not per row: a task with ten shapes contributes ten rows and exactly one
    # send. Counting rows would report a project's origins in proportion to how densely each item
    # happened to be annotated, which is not what the number means.
    origins: dict[str, int] = {}
    datasets: dict[str, int] = {}
    for task_id in {r["task_id"] for r in plan.rows}:
        row = next(r for r in plan.rows if r["task_id"] == task_id)
        origins[row["item_source_kind"]] = origins.get(row["item_source_kind"], 0) + 1
        if row["item_dataset"]:
            datasets[row["item_dataset"]] = datasets.get(row["item_dataset"], 0) + 1
    return custom_facet(
        projectId=project.project_id,
        projectSlug=project.slug,
        publishId=plan.publish_id,
        taskCount=len(plan.attributions),
        acceptedCount=plan.accepted_count,
        skippedCount=plan.skipped_count,
        annotatorCount=len(annotators),
        reviewerCount=len(reviewers),
        reviewRequired=project.review_required,
        labelClasses=sorted(c.name for c in project.label_schema.classes),
        shapeCount=plan.shape_count,
        sendOrigins=dict(sorted(origins.items())),
        # §7.2 specifies a per-dataset `version` alongside these counts, as the reproducibility pin.
        # `ItemSource` carries no version field today, so emitting one would mean inventing it — and
        # §7.2 is explicit that a single fabricated pin is a lie. Counts now; the pin lands when the
        # send capture records the version it read.
        sourceDatasets=[{"dataset": name, "items": n} for name, n in sorted(datasets.items())],
        leadTimeSecondsTotal=project.lead_time_seconds_total,
        frozenAt=frozen_at.isoformat() if frozen_at else None,
        # An accepted task with no reviewer is legal (the project can waive review), so it is
        # REPORTED rather than refused — and it is the number a governance reader most wants.
        tasksWithoutReview=sum(1 for a in plan.attributions if a.outcome == "accepted" and not a.reviewed_by),
    )


def table_properties(project: AnnotationProject, plan: PublishPlan) -> dict[str, str]:
    """The properties stamped on the table at create (§7.1). All strings — Lance table properties are
    a string→string map, so the numbers are rendered rather than left to a serializer's discretion."""
    return {
        "annotation.project_id": project.project_id,
        "annotation.publish_id": plan.publish_id,
        "annotation.task_count": str(len(plan.attributions)),
        "annotation.accepted_count": str(plan.accepted_count),
        "annotation.skipped_count": str(plan.skipped_count),
        "annotation.review_required": str(project.review_required).lower(),
        "annotation.label_classes": ",".join(sorted(c.name for c in project.label_schema.classes)),
    }
