"""Task endpoints — the CVAT/Label-Studio loop: send, assign, claim, save, submit, review.

Each route authorizes the ONE `can_*` its event requires, on the **verified** subject, then invokes
the task actor. The permission is not hardcoded here: `annotator.projects.machines` already carries
it on the edge (`TASK_EDGES[(state, event)] -> (target, permission)`), so the op→privilege map has a
single home and a route cannot drift from the model.

Two rules the machine cannot express, enforced here because they need the task's own data:

- **Self-review is forbidden.** A reviewer may not accept their own submission (§5.2). The events are
  named in `SELF_REVIEW_FORBIDDEN` so the rule is discoverable from the machine; the check needs
  `task.submitted_by`, which only the actor holds.
- **The lease holder is the only one who may save or submit.** `can_annotate` says you may annotate
  in this project; holding the claim says you may annotate THIS task right now.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, Field

from annotator.api.security import CheckerDep, CurrentSubject
from annotator.projects.actor import AnnotationTaskActorInterface
from annotator.projects.machines import SELF_REVIEW_FORBIDDEN, IllegalTransition, task_transition
from annotator.projects.models import Shape, TaskState
from service_kit.exceptions import ConflictError, ForbiddenError, NotFoundError
from service_kit.governed.audit import FAILURE, SUCCESS, audit


router = APIRouter(prefix="/tasks", tags=["annotation-tasks"])

TaskId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class FireRequest(BaseModel):
    """One event against a task. `event` must be an edge in `TASK_EDGES`."""

    event: str = Field(min_length=1)
    project: str = Field(min_length=1, description="the tenant — the FGA parent the check runs against")
    lease_seconds: int = Field(default=1800, gt=0)
    review_required: bool = True


class SaveDraftRequest(BaseModel):
    project: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    shapes: list[Shape] = Field(default_factory=list)
    base_revision: int | None = None
    origin: str = "human"


def _proxy(task_id: str) -> AnnotationTaskActorInterface:
    """A typed proxy to one task's actor. Imported lazily: `ActorProxy` opens a sidecar channel, so
    importing it at module scope would make this module require daprd just to be read.

    `cast`, not a suppression: `ActorProxy` dispatches `__getattr__` over the wire, so it satisfies
    the interface structurally but cannot declare it. Naming the interface as the return type is what
    keeps a typo in a method name a type error here rather than a 404 from the sidecar.
    """
    from dapr.actor import ActorId, ActorProxy  # noqa: PLC0415 - deliberate, see docstring

    proxy = ActorProxy.create("AnnotationTaskActor", ActorId(task_id), AnnotationTaskActorInterface)
    return cast(AnnotationTaskActorInterface, proxy)


async def _authorize(checker: Any, subject: str, permission: str | None, tenant: str, what: str) -> None:
    """Check one relation on the tenant, fail closed, and audit either way."""
    if permission is None:  # a system-caused edge — no principal, nothing to authorize
        return
    parent = f"project:{tenant}"
    if not await checker(user=subject, relation=permission, obj=parent):
        audit(what, FAILURE, subject=subject, resource=parent, relation=permission)
        raise ForbiddenError(f"{subject} lacks {permission} on {parent}")


@router.post("/{task_id}/events", status_code=status.HTTP_200_OK)
async def fire_task_event(task_id: TaskId, payload: FireRequest, checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
    """Drive one task transition.

    The permission comes from the transition table, so adding an edge to `TASK_EDGES` automatically
    gates it — there is no per-route ladder here to forget to update.
    """
    actor = _proxy(task_id)
    current = await actor.get()
    if current is None:
        raise ConflictError(f"task {task_id} has not been sent into a project")

    state = TaskState(current["state"])
    try:
        _target, permission = task_transition(state, payload.event)
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc

    await _authorize(checker, subject, permission, payload.project, f"task.{payload.event}")

    # Rules the table cannot express, because they depend on the task's own rows.
    if payload.event in SELF_REVIEW_FORBIDDEN and current.get("submitted_by") == subject:
        audit(f"task.{payload.event}", FAILURE, subject=subject, resource=task_id, reason="self_review")
        raise ForbiddenError("a reviewer may not review their own submission")
    if payload.event in {"save_draft", "submit"} and current.get("assignee") not in (None, subject):
        raise ForbiddenError(f"task {task_id} is held by {current['assignee']}")

    try:
        updated = await actor.fire(
            {
                "event": payload.event,
                "actor": subject,
                "lease_seconds": payload.lease_seconds,
                "review_required": payload.review_required,
            }
        )
    except IllegalTransition as exc:  # lost a race — the state moved between our read and the call
        raise ConflictError(str(exc)) from exc

    audit(f"task.{payload.event}", SUCCESS, subject=subject, resource=task_id)
    return updated


@router.get("/{task_id}")
async def get_task(task_id: TaskId, checker: CheckerDep, subject: CurrentSubject, project: str) -> dict[str, Any]:
    await _authorize(checker, subject, "can_view", project, "task.view")
    task = await _proxy(task_id).get()
    if task is None:
        raise ConflictError(f"task {task_id} has not been sent into a project")
    return task


@router.put("/{task_id}/draft", status_code=status.HTTP_200_OK)
async def save_draft(task_id: TaskId, payload: SaveDraftRequest, checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
    """Replace the whole shape set in one keyed write. `base_revision` is the etag — a mismatch is a
    409, which is how two tabs of one annotator are stopped from silently clobbering each other."""
    await _authorize(checker, subject, "can_annotate", payload.project, "task.save_draft")
    actor = _proxy(task_id)

    current = await actor.get()
    if current is None:
        raise ConflictError(f"task {task_id} has not been sent into a project")
    if current.get("assignee") not in (None, subject):
        raise ForbiddenError(f"task {task_id} is held by {current['assignee']}")

    try:
        draft = await actor.save_draft(
            {
                "task_id": task_id,
                "project_id": payload.project_id,
                "author": subject,
                "shapes": [s.model_dump(mode="json") for s in payload.shapes],
                "base_revision": payload.base_revision,
                "origin": payload.origin,
            }
        )
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc

    audit("task.save_draft", SUCCESS, subject=subject, resource=task_id)
    return draft


@router.get("/{task_id}/draft")
async def get_draft(task_id: TaskId, checker: CheckerDep, subject: CurrentSubject, project: str) -> dict[str, Any]:
    await _authorize(checker, subject, "can_view", project, "task.view_draft")
    draft = await _proxy(task_id).get_draft()
    if draft is None:
        raise NotFoundError(f"task {task_id} has no draft")
    return draft
