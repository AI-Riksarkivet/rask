"""Publish → silver: which annotations land, and whose names travel with them.

This module is the *decision* half of publishing. It is deliberately pure — no Dapr, no Lance, no
sidecar — so the two rules that matter can be tested exhaustively without a cluster:

1. **Only `accepted` tasks contribute rows.** `skipped` is terminal, so it satisfies the publish
   precondition, but a skipped task can still hold a draft: an annotator drew shapes, then decided
   the item did not belong. Terminal-therefore-publishable is the trap; `_ROWS_FROM` is the answer.
2. **Attribution comes from the task, never from the payload.** `submitted_by` and `reviewed_by` are
   written by the actor from the OIDC-verified subject. The draft's `author` is verified too, but it
   records who last SAVED — under `fix_and_accept` that can be the reviewer, so the two are carried
   separately rather than collapsed into one "who did this".

Provenance is written in **both** directions, matching R26's existing split:

- **Into the rows** — every silver row carries its own annotator, reviewer and review action, so a
  consumer can answer "who annotated this row" from the data, with no graph query.
- **Into the run event** — one `annotationAuthorship` custom facet naming the distinct contributors
  and the review outcome, so the lineage graph can answer "who contributed to this dataset".

They cannot disagree because both are projected from the same `Attribution` list, in one pass.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lineage_kit.schemas import custom_facet
from pydantic import BaseModel, Field

from annotator.projects.models import Draft, Task, TaskState


#: The only state that contributes rows. `SKIPPED` is terminal — it satisfies the publish
#: precondition — but it contributes NOTHING, and a skipped task may still hold a draft.
_ROWS_FROM = frozenset({TaskState.ACCEPTED})

#: The facet key on the publish run event. Custom, but spec-legal via `custom_facet`.
AUTHORSHIP_FACET = "annotationAuthorship"


class PublishRefusal(ValueError):
    """A publish that must not proceed. Raised rather than returning a partial plan — a publish that
    silently drops rows is worse than one that stops and says why."""


class Attribution(BaseModel):
    """Who produced one accepted annotation, taken entirely from server-written fields."""

    task_id: str
    annotated_by: str
    annotated_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_action: str | None = None
    #: Who last SAVED the shapes. Usually the annotator; under `fix_and_accept` it can be the
    #: reviewer, which is exactly the case that makes collapsing these two fields a lie.
    drafted_by: str = ""
    shape_count: int = 0


class PublishPlan(BaseModel):
    """Everything the publish workflow needs, decided in one pass so nothing can drift between the
    rows it writes and the lineage it emits."""

    project_id: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    attributions: list[Attribution] = Field(default_factory=list)
    skipped_task_ids: list[str] = Field(default_factory=list)

    @property
    def shape_count(self) -> int:
        return len(self.rows)


def attribution_for(task: Task, draft: Draft) -> Attribution:
    """Project one accepted task + its draft into an attribution row.

    `submitted_by` is required: an accepted task with nobody recorded as having submitted it means
    the audit trail is broken, and publishing anonymous provenance is worse than not publishing.
    """
    if not task.submitted_by:
        raise PublishRefusal(f"task {task.task_id} is accepted but records no submitter — refusing to publish anonymous work")
    return Attribution(
        task_id=task.task_id,
        annotated_by=task.submitted_by,
        annotated_at=task.submitted_at,
        reviewed_by=task.reviewed_by,
        reviewed_at=task.reviewed_at,
        review_action=task.review_action,
        drafted_by=draft.author,
        shape_count=len(draft.shapes),
    )


def build_plan(project_id: str, pairs: list[tuple[Task, Draft | None]]) -> PublishPlan:
    """Decide the whole publish in one pass.

    `pairs` is every task in the project with its draft (or `None`). Non-accepted tasks are recorded
    in `skipped_task_ids` rather than silently dropped, so the run event can state how many items the
    project held versus how many it published — a publish that quietly halves a dataset should be
    visible in the lineage, not inferred from a row count.
    """
    plan = PublishPlan(project_id=project_id)
    for task, draft in pairs:
        if task.state not in _ROWS_FROM:
            plan.skipped_task_ids.append(task.task_id)
            continue
        if draft is None:
            # Accepted with no draft is legitimate — a reviewer can accept an empty item ("nothing
            # to annotate here"). It contributes attribution but no rows.
            plan.attributions.append(attribution_for(task, Draft(task_id=task.task_id, project_id=project_id, author=task.submitted_by or "")))
            continue
        attribution = attribution_for(task, draft)
        plan.attributions.append(attribution)
        for shape in draft.shapes:
            plan.rows.append(
                {
                    "project_id": project_id,
                    "task_id": task.task_id,
                    **shape.model_dump(mode="json"),
                    # Row-level provenance: from the TASK, so it survives any client payload.
                    "annotated_by": attribution.annotated_by,
                    "annotated_at": attribution.annotated_at.isoformat() if attribution.annotated_at else None,
                    "reviewed_by": attribution.reviewed_by,
                    "reviewed_at": attribution.reviewed_at.isoformat() if attribution.reviewed_at else None,
                    "review_action": attribution.review_action,
                    "drafted_by": attribution.drafted_by,
                }
            )
    return plan


def authorship_facet(plan: PublishPlan, *, published_by: str) -> dict[str, Any]:
    """The `annotationAuthorship` run facet — who contributed, projected from the same plan.

    Distinct and SORTED: the facet is part of a run event that may be replayed by the workflow after
    a crash, and two replays that produce differently-ordered lists would look like two different
    provenances for one publish.
    """
    annotators = sorted({a.annotated_by for a in plan.attributions})
    reviewers = sorted({a.reviewed_by for a in plan.attributions if a.reviewed_by})
    actions: dict[str, int] = {}
    for a in plan.attributions:
        if a.review_action:
            actions[a.review_action] = actions.get(a.review_action, 0) + 1
    return custom_facet(
        publishedBy=published_by,
        annotators=annotators,
        reviewers=reviewers,
        reviewActions=dict(sorted(actions.items())),
        tasksPublished=len(plan.attributions),
        tasksSkipped=len(plan.skipped_task_ids),
        shapes=plan.shape_count,
        # An unreviewed accepted task is legal — the project can waive review — so this is reported
        # rather than refused. It is the number a governance reader most wants and would otherwise
        # have to derive by diffing two lists.
        tasksWithoutReview=sum(1 for a in plan.attributions if not a.reviewed_by),
    )
