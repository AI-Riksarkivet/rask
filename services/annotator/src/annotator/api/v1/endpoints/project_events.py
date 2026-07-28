"""Project lifecycle endpoints — open, send, freeze, publish, archive (§5.1).

The sibling of `tasks.py`, and it reads the permission the same way: from `PROJECT_EDGES`, never from
a ladder written here. Adding an edge to the machine gates it with no route change, and a route
cannot drift from the model.

**Publish is the one operation with more than one door (§6.2), and that is the design's most important
authz consequence.** Three checks, all of which must pass:

1. `can_publish` on `annotation_project:<project_id>` — the annotator's own domain.
2. `can_create_table` on the TARGET NAMESPACE — the governed plane's own rung.
3. `can_promote` on the target namespace, *conditionally*, when it is a validator-gated medallion
   stage — the same rung stage promotion already uses.

Nobody moves labels into the lakehouse by holding annotator rights alone, and nobody is forced to
publish by holding table rights alone. The crossing between the two planes is explicit, which is what
"its own domain, synced only when we choose" means in authz terms. Collapsing these into one check
would let either plane's admin quietly acquire the other's authority.

The fourth precondition — every task terminal — is NOT checked here. It lives in the project actor
(`fire`), so it holds for any caller, including the publish workflow retrying after a crash.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, cast

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, Field

from annotator.api.security import CheckerDep, CurrentSubject
from annotator.projects.machines import IllegalTransition, project_transition
from annotator.projects.models import Task
from annotator.projects.project_actor import AnnotationProjectActorInterface
from service_kit.exceptions import ConflictError, ForbiddenError
from service_kit.governed.audit import FAILURE, SUCCESS, audit


router = APIRouter(prefix="/projects", tags=["annotation-projects"])

ProjectId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

#: §6.2 door 2 — the governed plane's rung on the target namespace.
CREATE_TABLE_RELATION: Final[str] = "can_create_table"
#: §6.2 door 3 — only when the target namespace is a validator-gated medallion stage.
PROMOTE_RELATION: Final[str] = "can_promote"

#: Human labels are curated, not raw, so the default target is the tenant warehouse's `silver`
#: namespace (§6.2). A publish may name another; the doors are checked wherever it points.
DEFAULT_TARGET_NAMESPACE: Final[str] = "silver"

#: Namespaces whose promotion is validator-gated. `silver` and `gold` are medallion stages; a publish
#: into one crosses the same gate a stage promotion does, so door 3 applies.
_VALIDATOR_GATED: Final[frozenset[str]] = frozenset({"silver", "gold"})


class ProjectEventRequest(BaseModel):
    """One event against a project. `event` must be an edge in `PROJECT_EDGES`."""

    event: str = Field(min_length=1)
    #: The FGA parent. Required and never inferred — it is the object every check runs against.
    tenant: str = Field(min_length=1)
    target_namespace: str = DEFAULT_TARGET_NAMESPACE
    error: str | None = None


class SendItemsRequest(BaseModel):
    """Send items into a project as tasks (§5.1: legal in draft/labeling, state unchanged)."""

    tenant: str = Field(min_length=1)
    items: list[Task] = Field(min_length=1, max_length=1000)


def _project_proxy(project_id: str) -> AnnotationProjectActorInterface:
    """A typed proxy to one project's actor. Imported lazily — `ActorProxy` opens a sidecar channel,
    so importing it at module scope would make this module require daprd just to be read."""
    from dapr.actor import ActorId, ActorProxy  # noqa: PLC0415 - deliberate, see docstring

    proxy = ActorProxy.create("AnnotationProjectActor", ActorId(project_id), AnnotationProjectActorInterface)
    return cast(AnnotationProjectActorInterface, proxy)


def _task_proxy(task_id: str) -> Any:
    from dapr.actor import ActorId, ActorProxy  # noqa: PLC0415 - deliberate, see above

    from annotator.projects.actor import AnnotationTaskActorInterface  # noqa: PLC0415 - same reason

    return ActorProxy.create("AnnotationTaskActor", ActorId(task_id), AnnotationTaskActorInterface)


async def _check(checker: Any, subject: str, relation: str, obj: str, what: str) -> None:
    """One relation on one object, fail-closed, audited either way."""
    if not await checker(user=subject, relation=relation, obj=obj):
        audit(what, FAILURE, subject=subject, resource=obj, relation=relation)
        raise ForbiddenError(f"{subject} lacks {relation} on {obj}")


async def _authorize_publish(checker: Any, subject: str, project_id: str, namespace: str) -> None:
    """The two-door crossing of §6.2 (three when the target is validator-gated).

    Checked in order and short-circuiting, so the audit trail names the FIRST door that closed rather
    than a composite verdict nobody can act on.
    """
    await _check(checker, subject, "can_publish", f"annotation_project:{project_id}", "project.publish")
    await _check(checker, subject, CREATE_TABLE_RELATION, f"namespace:{namespace}", "project.publish")
    if namespace in _VALIDATOR_GATED:
        await _check(checker, subject, PROMOTE_RELATION, f"namespace:{namespace}", "project.publish")


@router.post("/{project_id}/events", status_code=status.HTTP_200_OK)
async def fire_project_event(project_id: ProjectId, payload: ProjectEventRequest, checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
    """Drive one project transition.

    The permission comes from `PROJECT_EDGES`; `publish` additionally crosses the lakehouse doors.
    The every-task-terminal precondition is enforced by the actor, not here.
    """
    actor = _project_proxy(project_id)
    current = await actor.get()
    if current is None:
        raise ConflictError(f"annotation project {project_id} does not exist")

    from annotator.projects.models import ProjectState  # noqa: PLC0415 - avoids a cycle at import

    try:
        _target, permission = project_transition(ProjectState(current["state"]), payload.event)
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc

    if payload.event == "publish":
        await _authorize_publish(checker, subject, project_id, payload.target_namespace)
    elif permission is not None:
        await _check(checker, subject, permission, f"project:{payload.tenant}", f"project.{payload.event}")

    try:
        updated = await actor.fire({"event": payload.event, "actor": subject, "error": payload.error})
    except IllegalTransition as exc:
        # The actor's own preconditions — every task terminal, and a non-empty project.
        raise ConflictError(str(exc)) from exc

    audit(f"project.{payload.event}", SUCCESS, subject=subject, resource=project_id)
    return updated


@router.post("/{project_id}/items", status_code=status.HTTP_201_CREATED)
async def send_items(project_id: ProjectId, payload: SendItemsRequest, checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
    """Send items into the project as tasks.

    Two writes per item, and the ORDER is the whole correctness argument: seed the TASK actor first,
    then register it in the project index. A crash between them leaves a task that exists but is not
    indexed — invisible to the publish precondition, which is the safe direction only because `send`
    is idempotent on `task_id` and a re-send repairs it. The reverse order would index a task whose
    actor was never seeded, and the publish precondition would then read a state for a task that
    cannot answer for itself.
    """
    project = await _project_proxy(project_id).get()
    if project is None:
        raise ConflictError(f"annotation project {project_id} does not exist")
    await _check(checker, subject, "can_send_items", f"project:{payload.tenant}", "project.send")

    created: list[str] = []
    for item in payload.items:
        task = item.model_copy(update={"project_id": project_id})
        body = task.model_dump(mode="json")
        await _task_proxy(task.task_id).seed(body)
        result = await _project_proxy(project_id).send(body)
        if result.get("created"):
            created.append(task.task_id)

    audit("project.send", SUCCESS, subject=subject, resource=project_id)
    return {"sent": len(payload.items), "created": len(created), "task_ids": created}


@router.get("/{project_id}/tasks")
async def list_project_tasks(project_id: ProjectId, checker: CheckerDep, subject: CurrentSubject, tenant: str) -> dict[str, Any]:
    """The task index plus the publish precondition, computed from ONE snapshot.

    Returned together deliberately: a caller that read the index and then asked "may I publish?"
    separately could compute the answer from a different snapshot than the one it is showing.
    """
    await _check(checker, subject, "can_view", f"project:{tenant}", "project.list_tasks")
    return await _project_proxy(project_id).list_tasks()
