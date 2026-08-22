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

import logging
from typing import Annotated, Any, Final, cast

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from annotator.api.dependencies import ControlEmitterDep
from annotator.api.security import CheckerDep, CurrentSubject
from annotator.projects.machines import FROZEN_PROJECT_STATES, PROJECT_EDGES, IllegalTransition, legal_task_events, project_transition
from annotator.projects.models import ItemSource, MediaRef, ProjectState, Shape, Task, TaskState, new_id
from annotator.projects.ontology import LabelOntology, ShapeLike, membership_violation
from annotator.projects.project_actor import AnnotationProjectActorInterface
from service_kit.control_emit import emit_control
from service_kit.exceptions import ConflictError, ForbiddenError, NotFoundError
from service_kit.governed.audit import FAILURE, SUCCESS, audit
from service_kit.lakehouse.warehouse_registry import namespace_for, namespace_tiers
from service_kit.media.deps import StateDep
from service_kit.media.state import dataset_handle


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/projects", tags=["annotation-projects"])

ProjectId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

#: §6.2 door 2 — the governed plane's rung on the target namespace.
CREATE_TABLE_RELATION: Final[str] = "can_create_table"
#: §6.2 door 3 — only when the target namespace is a validator-gated medallion stage.
PROMOTE_RELATION: Final[str] = "can_promote"

#: The TIER human labels are curated into (§6.2) — the NAME is composed per project, because a bare
#: literal put every tenant's labels in one namespace with one FGA parent and one set of grants.
#: A publish may name another target; the doors are checked wherever it points.
DEFAULT_TARGET_TIER: Final[str] = "silver"

#: Project edges caused by the SYSTEM (the publish saga), never by a principal. Refused on the HTTP
#: surface: `TASK_EDGES`/`PROJECT_EDGES` giving them no permission means "no principal fires this",
#: and exposing them unauthenticated would let anyone mark another operator's publish succeeded.
SYSTEM_ONLY_EVENTS: Final[frozenset[str]] = frozenset(e for (_s, e), (_t, p) in PROJECT_EDGES.items() if p is None)

#: TIERS whose promotion is validator-gated. `silver` and `gold` are medallion stages; a publish into
#: one crosses the same gate a stage promotion does, so door 3 applies. Bronze is deliberately absent:
#: it is the first governed tier rather than a promotion target, and gating it would demand the
#: validator rung for an ordinary ingest write.
#:
#: MATCHED AGAINST THE NAMESPACE'S TIERS, NOT ITS NAME. This was `namespace in _VALIDATOR_GATED`, an
#: exact-string test, and every namespace the estate actually has is project-QUALIFIED
#: (`scripts/seed_estate.py` creates `acme-silver`/`acme-gold`), so it was False for all of them and
#: door 3 never fired. `target_namespace` is caller-supplied, so this was reachable by naming the
#: namespace you would have to name anyway — the normal path, not a crafted input.
_VALIDATOR_GATED: Final[frozenset[str]] = frozenset({"silver", "gold"})


class ProjectEventRequest(BaseModel):
    """One event against a project. `event` must be an edge in `PROJECT_EDGES`.

    Carries no tenant: the authorization object is `annotation_project:<project_id>` from the path,
    so a caller cannot name the object its own permission is checked against.
    """

    event: str = Field(min_length=1)
    #: Empty means "the project's default tier", resolved by the door once it has loaded the project
    #: — the request cannot compose it, because the tenant is on the document rather than in the path.
    target_namespace: str = ""


class PredictionShape(BaseModel):
    """One pre-annotation as a SENDER may state it — `Shape` minus its provenance.

    Declared separately rather than reusing `Shape` for the same reason `SendItem` is not a `Task`:
    `Shape` carries `source`, and `source` is how every later surface tells suggested work from
    drawn work. A sender able to set it could stamp `human` on five hundred items nobody looked at,
    and they would read as annotated for the rest of the corpus's life. Omitting the field is
    stronger than overwriting it — there is no path by which it can arrive.

    Unknown keys are IGNORED (pydantic's default), so a client sending a whole `Shape` is not an
    error; the fields it may not write simply do not land.
    """

    shape_type: str = Field(min_length=1, max_length=32)
    label: str | None = Field(default=None, max_length=128)
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    rotation: float | None = None
    polygon: list[float] = Field(default_factory=list)
    t_start: float | None = None
    t_end: float | None = None
    parent_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    mask: str | None = None
    text: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    group: str | None = None
    #: A producer's OWN confidence. Unlike `source` this is the sender's to state — it describes the
    #: suggestion, not who made it — and the uncertainty selector (`open_browse.md` §4) reads it.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    model_version: str | None = Field(default=None, max_length=128)


class SendItem(BaseModel):
    """One item to send. Deliberately NOT a `Task`.

    Accepting a full `Task` would let the sender supply `state`, `submitted_by`, `reviewed_by` and
    `review_action` — fabricating reviewed work with forged provenance, and defeating the entire
    "attribution comes from the task, not the payload" guarantee, because the client would have
    written the task. Only the fields that describe WHAT to annotate are accepted; everything about
    who did what is server-written — including a prediction's `source`, which is why the shapes
    below are `PredictionShape` and not `Shape`.
    """

    task_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    source: ItemSource
    media: MediaRef
    #: PRE-ANNOTATIONS for this item — the bulk-labeling payload. Empty for an ordinary send. Capped
    #: because a prediction rides the task DOCUMENT: an unbounded list is an unbounded actor state.
    prediction: list[PredictionShape] = Field(default_factory=list, max_length=200)


class SendItemsRequest(BaseModel):
    """Send items into a project as tasks (§5.1: legal in draft/labeling, state unchanged)."""

    items: list[SendItem] = Field(min_length=1, max_length=1000)


def _project_proxy(project_id: str) -> AnnotationProjectActorInterface:
    """A typed proxy to one project's actor. Imported lazily — the proxy opens a sidecar channel,
    so importing it at module scope would make this module require daprd just to be read.
    `typed_proxy` maps Python names onto the interface's wire names (a raw `ActorProxy` dispatches
    only the wire names — the mismatch the first live drive found)."""
    from annotator.projects.proxies import typed_proxy  # noqa: PLC0415 - deliberate, see docstring

    return cast(AnnotationProjectActorInterface, typed_proxy("AnnotationProjectActor", project_id, AnnotationProjectActorInterface))


def _task_proxy(task_id: str) -> Any:
    from annotator.projects.actor import AnnotationTaskActorInterface  # noqa: PLC0415 - deliberate, see above
    from annotator.projects.proxies import typed_proxy  # noqa: PLC0415 - same reason

    return typed_proxy("AnnotationTaskActor", task_id, AnnotationTaskActorInterface)


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
    # An INTERSECTION, so the ambiguous shape fails closed. `namespace_tiers` returns a set rather than
    # "the" tier because a project may itself contain hyphens: `acme-bronze-gold` is either project
    # `acme` with a `bronze-gold` lane or project `acme-bronze` promoting into gold, and picking the
    # leftmost would let the second skip a door it must cross.
    if namespace_tiers(namespace) & _VALIDATOR_GATED:
        await _check(checker, subject, PROMOTE_RELATION, f"namespace:{namespace}", "project.publish")


@router.post("/{project_id}/events", status_code=status.HTTP_200_OK)
async def fire_project_event(project_id: ProjectId, payload: ProjectEventRequest, checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
    """Drive one project transition.

    The permission comes from `PROJECT_EDGES`; `publish` additionally crosses the lakehouse doors.
    The every-task-terminal precondition is enforced by the actor, not here.
    """
    if payload.event in SYSTEM_ONLY_EVENTS:
        raise ForbiddenError(f"{payload.event} is fired by the publish saga, not by a principal, and cannot be posted over HTTP")

    actor = _project_proxy(project_id)
    current = await actor.get()
    if current is None:
        raise ConflictError(f"annotation project {project_id} does not exist")

    try:
        _target, permission = project_transition(ProjectState(current["state"]), payload.event)
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc

    # RESOLVED ONCE, BEFORE THE GATE. The door authorizes `namespace:<target>` and the actor pins the
    # table id from the same string; deriving them separately would let the gate check one object
    # while the write lands in another, which is worse than the unqualified write this replaces.
    target = payload.target_namespace or namespace_for(str(current.get("tenant") or ""), DEFAULT_TARGET_TIER)

    if payload.event == "publish":
        await _authorize_publish(checker, subject, project_id, target)
    elif permission is not None:
        # `annotation_project`, not `project:<tenant>` — that is the type on which model.fga defines
        # can_manage / can_send_items / can_publish. Checking them on the tenant asks for a relation
        # the `project` type does not define, which fails closed and makes the plane 403 with FGA on.
        await _check(checker, subject, permission, f"annotation_project:{project_id}", f"project.{payload.event}")

    try:
        # `target_namespace` rides along so the actor can PIN it with the publish token — the saga
        # (which may run after a crash, with no request in sight) reads the authorized target off
        # the project document rather than guessing one. Ignored by every other event.
        updated = await actor.fire({"event": payload.event, "actor": subject, "target_namespace": target})
    except IllegalTransition as exc:
        # The actor's own preconditions — every task terminal, and a non-empty project.
        raise ConflictError(str(exc)) from exc

    audit(f"project.{payload.event}", SUCCESS, subject=subject, resource=project_id)
    return updated


class AdjudicationRequest(BaseModel):
    """The manager's pick for one replica group — just the winning replica's id. The group comes
    from the path and the picker from the verified subject, so neither can be forged in the body."""

    task_id: str = Field(min_length=1, max_length=80)


@router.put("/{project_id}/adjudications/{group_id}", status_code=status.HTTP_200_OK)
async def adjudicate_group(
    project_id: ProjectId,
    group_id: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
    payload: AdjudicationRequest,
    checker: CheckerDep,
    subject: CurrentSubject,
) -> dict[str, Any]:
    """Consensus v1's merge step: name ONE accepted replica of the group canonical (a pick, never a
    blend — every replica's rows still publish; the facet carries the pick with attribution).

    `can_manage`, not `can_review`: adjudication decides which OPINION wins, which is the manager's
    distribution authority, not a review of any single submission. PUT because re-picking while the
    project is adjudicable is the intended idempotent shape; the actor refuses once provenance is
    frozen, and refuses a target that is not an accepted member of the group.
    """
    await _check(checker, subject, "can_manage", f"annotation_project:{project_id}", "project.adjudicate")
    try:
        updated = await _project_proxy(project_id).adjudicate({"group": group_id, "task_id": payload.task_id, "actor": subject})
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc
    audit("project.adjudicate", SUCCESS, subject=subject, resource=project_id)
    return updated


@router.delete("/{project_id}/adjudications/{group_id}", status_code=status.HTTP_200_OK)
async def clear_adjudication(
    project_id: ProjectId,
    group_id: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
    checker: CheckerDep,
    subject: CurrentSubject,
) -> dict[str, Any]:
    """Withdraw the pick for one group. Exists because a pick has no other exit: the publish
    refuses a stale or groupless pick (correctly), so without removal one wrong pick would wedge
    the publish permanently (audit finding). Idempotent — clearing an absent pick is a no-op —
    and refused once provenance is frozen, exactly like setting one."""
    await _check(checker, subject, "can_manage", f"annotation_project:{project_id}", "project.adjudicate")
    try:
        updated = await _project_proxy(project_id).adjudicate({"group": group_id, "task_id": None, "actor": subject})
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc
    audit("project.adjudicate", SUCCESS, subject=subject, resource=project_id)
    return updated


#: A task may be dropped only while the project can still change what it will publish. Past
#: `frozen` the answer set is closed and a publish is being prepared against it — removing an item
#: then would change what the run facet describes after the description was fixed.
DROPPABLE_STATES = frozenset({ProjectState.DRAFT, ProjectState.LABELING})


@router.delete("/{project_id}/tasks/{task_id}", status_code=status.HTTP_200_OK)
async def drop_task(
    project_id: ProjectId,
    task_id: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
    checker: CheckerDep,
    subject: CurrentSubject,
    control: ControlEmitterDep,
) -> dict[str, Any]:
    """Remove one item from a project.

    Exists because a send can put items into a project that can NEVER be completed. The case we hit
    is an item naming a media dataset that has since been renamed or removed: the canvas cannot open
    it, so it cannot be claimed, submitted or skipped — and the publish precondition requires every
    task terminal. One unfinishable item wedges the project permanently, and before this the only way
    past it was to abandon the project and re-send everything.

    `can_manage`, not the annotator's own permission: discarding work is a manager act. Idempotent —
    dropping an absent task is a no-op, so a retry cannot 404 a project that is already how the
    caller wants it.
    """
    await _check(checker, subject, "can_manage", f"annotation_project:{project_id}", "project.drop_task")
    proxy = _project_proxy(project_id)
    doc = await proxy.get()
    if doc is None:
        raise NotFoundError(f"annotation project {project_id} does not exist")
    state = ProjectState(doc["state"])
    if state not in DROPPABLE_STATES:
        raise ConflictError(f"project {project_id} is {state.value} — items can be dropped only in {sorted(s.value for s in DROPPABLE_STATES)}")
    # WHO LOSES WORK, read BEFORE the drop. Afterwards the index entry naming this task is gone, and
    # the task actor — which keeps their draft — is no longer reachable from the project. `assignee`
    # first, then `submitted_by`: a task in review has no assignee (the actor nulls it on submit), so
    # falling back is what covers exactly the tasks with the most work already in them.
    holder = ""
    task = await _task_proxy(task_id).get()
    if isinstance(task, dict):
        holder = str(task.get("assignee") or task.get("submitted_by") or "")

    result = await proxy.drop_task({"task_id": task_id, "actor": subject})
    audit("project.drop_task", SUCCESS, subject=subject, resource=project_id)

    # The sharpest departure edge in the service: the item is DISCARDED, so their draft is not merely
    # stopped, it is orphaned where `saga.collect` will never enumerate it again.
    #
    # Gated on `removed` because the route is idempotent — a retry drops nothing and must not put a
    # second row in anyone's inbox — and on `holder != subject`, the standing exclusion for an outcome
    # the caller already has in the response they are looking at.
    if result.get("removed") and holder and holder != subject:
        await emit_control(
            control,
            action="task_dropped",
            object_type="annotation_task",
            object_id=f"annotation_task:{task_id}",
            actor=f"user:{subject}",
            extra={"subject": f"user:{holder}", "project": project_id, "event": "drop_task"},
        )
    return result


def _refuse_unknown_datasets(state: Any, payload: SendItemsRequest) -> None:
    """Refuse the WHOLE send if any item names a media dataset that does not resolve.

    Removal (`DELETE .../tasks/{id}`) is the escape hatch; this is the thing that stops the trap
    being set. An item naming a dataset that was renamed or removed cannot be opened on the canvas,
    so it can never be claimed, submitted or skipped — and the publish precondition requires EVERY
    task terminal, so one of them wedges the project. Creating it is the mistake; refusing at send
    is where it costs nothing.

    The whole send, not the offending items: a partial send produces exactly the half-populated
    project this is meant to prevent, and it costs ZERO seeded task actors to refuse here — the same
    argument the project-state check above makes.

    An UNVERIFIABLE registry lets the send through. If the dataset plane cannot be consulted at all
    (a degraded start, a mount that is not there yet) then refusing every send would take the whole
    labeling plane down for a check that is a guard, not a gate — the canvas still reports the real
    404 at read, and removal still exists. Not being able to check is not evidence of a problem.
    """
    names = sorted({item.source.where for item in payload.items if item.source.where})
    if not names:
        return  # every item takes the backend default, which resolves by construction
    unknown: list[str] = []
    for name in names:
        try:
            dataset_handle(state, name)
        except NotFoundError:
            unknown.append(name)
        except Exception:  # noqa: BLE001 - see docstring: unverifiable is not the same as wrong
            logger.warning("send could not consult the dataset registry; not refusing on a check that did not run")
            return
    if unknown:
        try:
            known = sorted(state.registry.list_ids())
        except Exception:  # noqa: BLE001 - naming the alternatives is a nicety, never the refusal
            known = []
        detail = f"dataset(s) {unknown} do not exist — an item naming one could never be opened, claimed or completed"
        raise ConflictError(f"{detail}; known datasets are {known}" if known else detail)


#: What `send` stamps onto a pre-annotation's `source`. The `import` precedent (§ `projects.imports`
#: `IMPORT_SOURCE`): a free-form provenance string written by the SERVER, which `publish` carries
#: through verbatim (`shape.source or "human"`). "bulk" is the honest word — a human chose the label,
#: but chose it for a selection rather than for this item.
BULK_SOURCE: Final[str] = "bulk"


def _validated_predictions(project: dict[str, Any], payload: SendItemsRequest) -> list[list[Shape]]:
    """Every item's pre-annotations, checked against the project's taxonomy and stamped.

    Returns one list per item, positionally aligned with ``payload.items``. Raises before returning
    if ANY item violates, so the caller can seed knowing the whole send is legal.

    The taxonomy check is `membership_violation` — the same function import and submit use, shared
    rather than reimplemented so the three stages cannot drift. A bulk action is the worst possible
    place to leak an invented label: one click is five hundred rows, and the closed-set property is
    the entire reason a class list is a first-class object.
    """
    ontology = LabelOntology()
    raw = project.get("ontology")
    if raw:
        try:
            ontology = LabelOntology.model_validate(raw)
        except Exception:  # noqa: BLE001 - an unreadable rule constrains nothing, as everywhere else
            logger.warning("project %s carries an ontology this service cannot parse", project.get("project_id"))

    out: list[list[Shape]] = []
    for index, item in enumerate(payload.items):
        # `source` is set HERE and nowhere else: `PredictionShape` does not declare it, so this is
        # the only path by which a stored prediction can acquire provenance at all.
        shapes = [Shape(**shape.model_dump(), source=BULK_SOURCE) for shape in item.prediction]
        violation = membership_violation(ontology, [ShapeLike.model_validate(s.model_dump()) for s in shapes])
        if violation is not None:
            raise ConflictError(f"item {index + 1} of {len(payload.items)}: {violation}")
        out.append(shapes)
    return out


@router.post("/{project_id}/items", status_code=status.HTTP_201_CREATED)
async def send_items(project_id: ProjectId, payload: SendItemsRequest, state: StateDep, checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
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
    if ProjectState(project["state"]) not in {ProjectState.DRAFT, ProjectState.LABELING}:
        # Checked here as well as in the actor so a refusal costs zero seeded task actors — the
        # actor's own refusal would fire only after the first `seed` had already landed.
        raise ConflictError(f"project {project_id} is {project['state']} — items may only be sent while it is draft or labeling")
    await _check(checker, subject, "can_send_items", f"annotation_project:{project_id}", "project.send")

    # REFUSE an item whose media dataset does not resolve. Deliberately after the FGA check: the
    # refusal names the datasets that DO exist, which is what makes it actionable and also what
    # makes it something an unauthorised caller must not be able to enumerate. Off the loop: the
    # body loops dataset_handle (blocking Lance/S3 under a threading.Lock) per named dataset —
    # inline it froze every in-flight request on a cold miss (open_python-audit ANN-01).
    await run_in_threadpool(_refuse_unknown_datasets, state, payload)

    # Consensus v1: N>1 seeds N independent replica items per source item, deterministic sibling
    # ids (`{gid}-r{k}`) — determinism is what lets the one-replica-per-annotator guard find them.
    consensus_n = int(project.get("consensus_n") or 1)
    if len(payload.items) * consensus_n > 1000:
        raise ConflictError(f"{len(payload.items)} items × consensus_n={consensus_n} exceeds the 1000-task send cap — split the send")

    # Every prediction in the send, validated against the project's taxonomy and stamped, BEFORE the
    # first actor is seeded. Doing it inside the loop would leave the good items queued and the bad
    # ones not — a half-applied bulk action, which is worse than a refused one because nothing says
    # which half landed and `seed` is idempotent, so a retry cannot undo the part that did.
    predictions = _validated_predictions(project, payload)

    created: list[str] = []
    for index, item in enumerate(payload.items):
        # Built HERE from the two client-supplied descriptive fields plus the project's own config.
        # Every provenance and state field takes its model default, so a sender cannot pre-set
        # `state=accepted` or name someone else as the annotator.
        group_id = item.task_id or new_id()
        capture: dict[str, Any] = {
            "review_required": bool(project.get("review_required", True)),
            "lease_seconds": int(project.get("lease_seconds") or 1800),
            # The ONTOLOGY rides every item, like the two captures above: submit enforcement reads
            # the ITEM's copy, so a mid-flight ontology edit cannot retroactively invalidate work.
            # This used to capture the `template` and leave the taxonomy behind on the project —
            # which is precisely why the closed-set label check could not exist: the class list was
            # not in scope where enforcement happens, so `label="asdf"` submitted and published.
            "ontology": project.get("ontology") or {},
            # Every replica of a group gets the SAME suggestion. Consensus asks several people the
            # same question; handing some of them a pre-annotation and not others would make their
            # disagreement an artefact of the send rather than of the images.
            "prediction": predictions[index],
        }
        replicas = (
            [Task(task_id=group_id, project_id=project_id, source=item.source, media=item.media, **capture)]
            if consensus_n == 1
            else [
                Task(
                    task_id=f"{group_id}-r{k}",
                    project_id=project_id,
                    replica_of=group_id,
                    source=item.source,
                    media=item.media,
                    **capture,
                )
                for k in range(1, consensus_n + 1)
            ]
        )
        for task in replicas:
            body = task.model_dump(mode="json")
            # `seed` is idempotent and returns what is ALREADY there. Checking its return is what
            # stops a client-chosen `task_id` that already belongs to another project from being
            # indexed here: the index entry would be written from the payload, the task's own
            # `_report_state` would only ever address its real owner, and this project's entry would
            # freeze at its seeded value — permanently non-terminal, `may_publish` false forever.
            seeded = await _task_proxy(task.task_id).seed(body)
            if str(seeded.get("project_id")) != project_id:
                raise ConflictError(f"task {task.task_id} already belongs to project {seeded.get('project_id')} — refusing to index it into {project_id}")
            result = await _project_proxy(project_id).send(body)
            if result.get("created"):
                created.append(task.task_id)

    audit("project.send", SUCCESS, subject=subject, resource=project_id)
    return {"sent": len(payload.items), "created": len(created), "task_ids": created}


@router.get("/{project_id}/tasks")
async def list_project_tasks(
    project_id: ProjectId,
    checker: CheckerDep,
    subject: CurrentSubject,
    include: str | None = None,
) -> dict[str, Any]:
    """The task index plus the publish precondition, computed from ONE snapshot.

    Returned together deliberately: a caller that read the index and then asked "may I publish?"
    separately could compute the answer from a different snapshot than the one it is showing.

    ``include=details`` additionally fans out to each task's OWN actor (bounded concurrency) for
    the full document — assignee, lease, media, review notes — plus its ``legal_events`` from
    `machines.legal_task_events`, the single source A2/A3 render their actions from. The index
    carries only ``task_id → state`` by design (the publish precondition needs no more); the
    queue UI needs the rest. A task whose actor holds no state is reported in ``missing`` rather
    than silently dropped or a 500 — the index can lead its actors after a half-completed send.
    """
    await _check(checker, subject, "can_view", f"annotation_project:{project_id}", "project.list_tasks")
    listing = await _project_proxy(project_id).list_tasks()
    if include != "details":
        return listing

    import asyncio  # noqa: PLC0415 - stdlib, endpoint-local

    # Rule 5 (§5.2): once the project is publishing/published/archived, EVERY task transition is
    # refused — so the details must not hand the UI actions that can only 409. The tasks' own
    # states still admit edges (accepted → reopen); the PROJECT is the gate.
    project = await _project_proxy(project_id).get()
    project_frozen = project is not None and ProjectState(project["state"]) in FROZEN_PROJECT_STATES

    gate = asyncio.Semaphore(16)

    async def _detail(task_id: str) -> tuple[str, dict[str, Any] | None]:
        async with gate:
            return task_id, await _task_proxy(task_id).get()

    pairs = await asyncio.gather(*(_detail(tid) for tid in sorted(listing["tasks"])))
    details: list[dict[str, Any]] = []
    missing: list[str] = []
    for task_id, doc in pairs:
        if doc is None:
            missing.append(task_id)
            continue
        events = [] if project_frozen else legal_task_events(TaskState(doc["state"]))
        details.append({**doc, "legal_events": events})
    return {**listing, "details": details, "missing": missing}
