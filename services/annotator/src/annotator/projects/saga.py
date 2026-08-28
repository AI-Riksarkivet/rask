"""The publish saga — project → lakehouse, crash-safe without a workflow engine.

**Why this is not a Dapr Workflow.** `docs/OPERATORS.md` §4 pins a reasoned ruling: Dapr Workflow
stays un-adopted, because every multi-step path in the estate is *token-keyed idempotent*, so
JetStream's redeliver-the-whole-message model already provides the crash-recovery a workflow engine
would sell us — at the cost of a new dependency and a sidecar state-store coupling. This saga is
built to satisfy that criterion rather than to argue with it.

The criterion has one real teeth-bearing consequence: **creating the table is the only step that is
not naturally idempotent**, and it is made idempotent BY KEY. `AnnotationProject.pending_publish_id`
is minted once by the project actor at the `publish` transition and reused by every retry, the table
id is derived from it, so a crashed-and-retried publish finds the table it already created rather
than making a second one. Mint per attempt and you get an orphan table per crash, tracked by nothing.

Every step therefore has a defined answer to "what if this runs twice":

| step | run twice |
| --- | --- |
| read project + tasks | pure read |
| build the plan | pure; a replay is byte-identical (`build_plan` sorts everything derived) |
| create the table | same id ⇒ already-exists, treated as success, NOT an error |
| tag the version | same tag name ⇒ already-exists, treated as success |
| record the publish | actor is set-once; the second call returns the first record |
| fire `publish_succeeded` | already `published` ⇒ `IllegalTransition`, swallowed as converged |

**The saga re-reads tasks from their own actors rather than trusting the index.** The index is
maintained by a best-effort call from the task actor, so it can lag; `reopen` is the one edge where
lagging is the UNSAFE direction. Re-reading closes that hole — it is the reason index drift can only
delay a publish, never let unreviewed work into the lakehouse.

The transport is injected (`Publisher`), so every path here is testable without a catalog, a sidecar
or a cluster.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from annotator.projects.machines import IllegalTransition
from annotator.projects.models import AnnotationProject, Draft, ProjectState, Task, TaskState
from annotator.projects.publish import PROJECT_FACET, PublishPlan, PublishRefusal, build_plan, project_facet, source_pin, table_properties
from service_kit.lakehouse.naming import CATALOG_DELIMITER


logger = logging.getLogger(__name__)


class Publisher(Protocol):
    """The lakehouse side of the saga, injected so the orchestration is testable without a catalog.

    Implementations post through the catalog's REST API — **the annotator never writes Lance
    directly** (§7.1), because the catalog is what seeds FGA ownership on the new table and emits
    the `CREATE` RunEvent with the caller's verified identity.
    """

    async def create_table(
        self,
        table_id: str,
        plan: PublishPlan,
        *,
        properties: dict[str, str],
        run_facet: dict[str, Any],
        source: str | None = None,
        source_version: int | None = None,
    ) -> int:
        """Create the table from the plan's rows; return its version. MUST treat an existing table
        with this id as success — the saga relies on that for retry safety. ``source`` +
        ``source_version`` are the §7.2 reproducibility pin (the lineage READ edge) — present only
        when the plan pins unambiguously, and the transport passes them through verbatim."""
        ...

    async def tag_version(self, table_id: str, version: int, tag: str) -> None:
        """Tag the version. MUST treat an existing identical tag as success."""
        ...


class ProjectHandle(Protocol):
    """The project actor's surface, narrowed to what the saga uses."""

    async def get(self) -> dict[str, Any] | None: ...
    async def list_tasks(self) -> dict[str, Any]: ...
    async def fire(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def record_publish(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def note_progress(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class TaskHandle(Protocol):
    async def get(self) -> dict[str, Any] | None: ...
    async def get_draft(self) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class PublishOutcome:
    """What the saga did. `already_published` distinguishes a converged retry from fresh work —
    without it a caller cannot tell "I published this" from "this was already published", and a
    notification would fire twice for one publish."""

    project_id: str
    publish_id: str
    table_id: str
    version: int
    tag: str
    rows: int
    shapes: int
    already_published: bool = False


# The catalog's identifier delimiter is `CATALOG_DELIMITER` (imported): estate ids are `bronze$events`,
# `silver$features`. A `.` here is NOT a separator to the catalog — the id would parse as one root-level
# segment, landing the table at the catalog ROOT while FGA authorized creation in `namespace:<target>`,
# so the authorization object and the created object would diverge.
def table_id_for(project: AnnotationProject, publish_id: str, namespace: str) -> str:
    """The published table's id — DETERMINISTIC in the publish token, which is what makes `create`
    idempotent on retry.

    The token, not the slug alone: two publishes of the same project (a republish after `reopen`)
    are two datasets and must not collide. The slug is carried for human legibility only.
    """
    return f"{namespace}{CATALOG_DELIMITER}{project.slug}_{publish_id[:12]}"


#: Bounded concurrency for the publish-path task reads — different actor ids, so this is real
#: parallelism. Matches the send path's seed fan-out cap (`_ACTOR_FANOUT`).
_COLLECT_FANOUT = 16


async def collect(project_handle: ProjectHandle, task_handle: Any, task_ids: list[str]) -> list[tuple[Task, Draft | None]]:
    """Read every task and draft FROM ITS OWN ACTOR — the authoritative source, not the index.

    This is the step that closes the index's one unsafe direction. `reopen` moves a task backwards
    out of `accepted`; if the report of that failed, the index still says `accepted` and the
    precondition would pass. Reading the actors makes the publish see the true state, so a stale
    index can only delay a publish.
    """

    async def _one(task_id: str) -> tuple[Task, Draft | None] | None:
        # `get` then `get_draft` stay chained — same actor, and the draft read is cheap after the
        # state read. Different task ids are different actor ids, so the FAN-OUT below is real
        # concurrency, not a turn-lock queue (which is why the send path needed SendMany instead).
        handle = task_handle(task_id)
        raw = await handle.get()
        if raw is None:
            return None  # decided by the caller, in index order — see below
        task = Task.model_validate(raw)
        draft_raw = await handle.get_draft()
        return task, Draft.model_validate(draft_raw) if draft_raw else None

    sem = asyncio.Semaphore(_COLLECT_FANOUT)

    async def _bounded(task_id: str) -> tuple[Task, Draft | None] | None:
        async with sem:
            return await _one(task_id)

    results = await asyncio.gather(*(_bounded(t) for t in task_ids))
    # THE REFUSAL IS DECIDED AFTER THE GATHER, on the LOWEST-INDEX missing task — `gather` preserves
    # input order in its RESULT list even though the coroutines finish out of order, so raising here
    # (rather than from inside `_one`) makes the message name the same task on every run. Raising
    # inside the fan-out would surface whichever get() completed first.
    for task_id, result in zip(task_ids, results, strict=True):
        if result is None:
            raise PublishRefusal(f"task {task_id} is in the project index but its actor holds no state — refusing to publish an incomplete project")
    return [r for r in results if r is not None]


async def run_publish(
    *,
    project_handle: ProjectHandle,
    task_handle: Any,
    publisher: Publisher,
    namespace: str,
    subject: str,
) -> PublishOutcome:
    """Run the saga to completion, or fire `publish_failed` and re-raise.

    Safe to call again after any crash: every step converges on the same `pending_publish_id`.
    """
    raw = await project_handle.get()
    if raw is None:
        raise PublishRefusal("annotation project does not exist")
    project = AnnotationProject.model_validate(raw)

    if project.published is not None:
        # A retry that got all the way through last time. Converged — report it rather than
        # republishing, which would create a second table for one logical publish.
        #
        # But CONVERGE THE STATE FIRST. A crash between `record_publish` and `publish_succeeded`
        # leaves `published` set while the project is still `publishing`, and `PROJECT_EDGES` has no
        # edge out of `publishing` except the two system ones — so returning here without firing
        # would strand the project in `publishing` forever, on the exact crash this saga exists to
        # survive. Returning early WITHOUT this was the bug.
        if project.state is ProjectState.PUBLISHING:
            await _converge(project_handle, {"event": "publish_succeeded", "actor": subject})
        record = project.published
        return PublishOutcome(
            project_id=project.project_id,
            publish_id=record.publish_id,
            table_id=record.table_id,
            version=record.version,
            tag=record.tag or "",
            rows=0,
            shapes=0,
            already_published=True,
        )

    if project.state is not ProjectState.PUBLISHING:
        raise PublishRefusal(f"project {project.project_id} is {project.state}, not publishing — the saga runs only after the publish transition")
    publish_id = project.pending_publish_id
    published_at = project.pending_publish_at

    try:
        # INSIDE the try, unlike the two refusals above, and the difference is which state the
        # project is left in. `publish_failed` is this saga's only exit and it is fired from this
        # try's `except` arm. Raised outside it, this refusal left a project in PUBLISHING with no
        # token, no recorded reason and no transition out — and every later watchdog tick re-entered,
        # re-refused and re-stranded it. `publish_failed` is a RESTING state precisely so a project
        # can be published again.
        #
        # The two siblings above stay outside on purpose: "does not exist" has no project to move,
        # and "is <state>, not publishing" is a state from which `publish_failed` would itself be an
        # illegal transition. Only THIS one describes a project that is genuinely publishing.
        if not publish_id or published_at is None:
            # The actor mints this at the `publish` transition. Its absence means the state machine
            # and this saga disagree about what happened, and guessing a token would defeat the whole
            # idempotency argument — so this stops rather than inventing one.
            raise PublishRefusal(f"project {project.project_id} is publishing but carries no publish token or instant — refusing to mint one here")

        await _note(project_handle, "reading tasks")
        listing = await project_handle.list_tasks()
        pairs = await collect(project_handle, task_handle, sorted(listing["tasks"]))
        _refuse_if_not_terminal(pairs)

        await _note(project_handle, "building plan")
        plan = build_plan(project, pairs, publish_id=publish_id, published_at=published_at)
        table_id = table_id_for(project, publish_id, namespace)
        tag = f"publish-{publish_id}"

        await _note(project_handle, f"creating table {table_id}")
        pin = source_pin(plan)
        version = await publisher.create_table(
            table_id,
            plan,
            properties=table_properties(project, plan),
            # The §7.2 pin — (dataset, version) when every item shares one captured source,
            # else nothing: `source_pin` never fabricates, so neither does the wire.
            source=pin[0] if pin else None,
            source_version=pin[1] if pin else None,
            # KEYED by the facet name. `project_facet` returns the spec-legal PAYLOAD; the catalog's
            # `X-Lance-Run-Facets` is a {name: payload} mapping, so handing it the bare payload would
            # emit a facet with no name — dropped or rejected at the far end, which is provenance
            # that silently does not arrive.
            run_facet={PROJECT_FACET: project_facet(project, plan)},
        )
        await _note(project_handle, f"tagging version {version}")
        await publisher.tag_version(table_id, version, tag)

        await _note(project_handle, "recording publish")
        record = await project_handle.record_publish(
            {
                "table_id": table_id,
                "namespace": namespace,
                "version": version,
                "tag": tag,
                "publish_id": publish_id,
                "published_at": published_at.isoformat(),
                "published_by": subject,
            }
        )
        await _converge(project_handle, {"event": "publish_succeeded", "actor": subject})
        return PublishOutcome(
            project_id=project.project_id,
            publish_id=publish_id,
            table_id=str(record["table_id"]),
            version=int(record["version"]),
            tag=str(record.get("tag") or tag),
            rows=len(plan.rows),
            shapes=plan.shape_count,
        )
    except Exception as exc:
        # `publish_failed` is a RESTING state, not a dead end: the project can be published again and
        # the retry reuses the same token. Recording the reason is what makes that retry informed.
        #
        # The log comes FIRST and the recording is itself guarded, because a failure while recording
        # the failure must not replace the original exception — the operator would then be shown the
        # actor's error instead of the one that actually broke the publish, and the real cause would
        # never be logged at all. That is precisely what an unguarded call here did.
        logger.exception("publish saga failed for project %s (token %s)", project.project_id, publish_id)
        try:
            await _converge(project_handle, {"event": "publish_failed", "actor": subject, "error": str(exc)})
        except Exception:
            logger.exception("could not record the publish failure for project %s — the original error stands", project.project_id)
        raise


def _refuse_if_not_terminal(pairs: list[tuple[Task, Draft | None]]) -> None:
    """The authoritative precondition, re-checked against the tasks' OWN state.

    The actor already checked this against the index before allowing the transition. Checking it
    again here is not redundancy — it is the only check that sees a task whose `reopen` never
    reached the index.
    """
    stragglers = [t.task_id for t, _ in pairs if t.state not in {TaskState.ACCEPTED, TaskState.SKIPPED}]
    if stragglers:
        raise PublishRefusal(f"tasks {sorted(stragglers)} are not terminal — the project index was stale and the publish is refused")


async def _note(handle: ProjectHandle, step: str) -> None:
    """Narrate the saga's current step onto the project document — A4's "not just a spinner".

    Best-effort by contract: progress is telemetry for a watching human, and a narration failure
    must never fail a publish. The actor ignores notes once the project has left PUBLISHING, so a
    stale run cannot scribble on a terminal state."""
    try:
        await handle.note_progress({"step": step})
    except Exception:
        logger.debug("could not record publish progress %r", step, exc_info=True)


async def _converge(handle: ProjectHandle, payload: dict[str, Any]) -> None:
    """Fire a terminal transition, tolerating the case where a previous attempt already fired it.

    `IllegalTransition` here means the project is ALREADY in the state we were driving it to, which
    is convergence, not failure. Anything else is a real problem and propagates.
    """
    try:
        await handle.fire(payload)
    except IllegalTransition:
        logger.info("project already in the target state for %s — converged", payload["event"])
