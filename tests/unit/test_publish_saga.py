"""The publish saga — every crash point, replayed.

`docs/OPERATORS.md` §4 permits skipping a workflow engine ONLY for paths that are token-keyed
idempotent. That is a claim about this code, so it is tested as one: the saga is run, killed after
each step in turn, and run again, and the assertion is always the same — one table, one publish
record, one logical publish.

The second theme is the index's blind spot. `reopen` moves a task out of `accepted`, and if the
report of that never reached the project index the precondition passes on stale data. The saga
re-reads tasks from their own actors precisely to catch that, so it is tested directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from annotator.projects.models import AnnotationProject, ProjectState, Task, TaskState
from annotator.projects.publish import PublishRefusal
from annotator.projects.saga import run_publish, table_id_for


PUBLISH_TOKEN = "0123456789abcdef0123456789abcdef"
MINTED_AT = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)


class _Project:
    """The project actor, in memory, with the real set-once / converge semantics."""

    def __init__(self, state: ProjectState = ProjectState.PUBLISHING, token: str | None = PUBLISH_TOKEN) -> None:
        self.doc = AnnotationProject(
            project_id="p1",
            tenant="acme",
            slug="vasa",
            state=state,
            pending_publish_id=token,
            # Minted WITH the token by the real actor, so the double must do the same or the
            # replay-determinism tests below would be testing a fiction.
            pending_publish_at=MINTED_AT if token else None,
        ).model_dump(mode="json")
        self.index: dict[str, str] = {}
        self.fired: list[str] = []
        self.progress_steps: list[str] = []

    async def get(self) -> dict[str, Any] | None:
        return dict(self.doc)

    async def list_tasks(self) -> dict[str, Any]:
        return {"tasks": dict(self.index), "counts": {}, "total": len(self.index), "terminal": 0, "may_publish": True}

    async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Mirrors `AnnotationProjectActor.fire` — including the two side effects the saga depends
        on: `publish_failed` records the reason, and `publish` REUSES an existing token rather than
        minting a new one. A double that skipped either would make the retry tests prove nothing."""
        from annotator.projects.machines import project_transition

        target, _ = project_transition(ProjectState(self.doc["state"]), payload["event"])
        self.doc["state"] = str(target)
        if payload["event"] == "publish_failed":
            self.doc["publish_error"] = str(payload.get("error", "publish failed"))
        elif payload["event"] == "publish":
            self.doc["publish_error"] = None
            # Mint token AND instant together, once — mirroring the real actor.
            if not self.doc.get("pending_publish_id"):
                self.doc["pending_publish_id"] = "minted-fresh"
                self.doc["pending_publish_at"] = MINTED_AT.isoformat()
        self.fired.append(payload["event"])
        return dict(self.doc)

    async def record_publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.doc.get("published"):
            return dict(self.doc["published"])
        self.doc["published"] = dict(payload)
        return dict(payload)

    async def note_progress(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Mirrors the real actor: recorded only while PUBLISHING, so a stale run cannot scribble
        on a terminal state."""
        if self.doc["state"] == str(ProjectState.PUBLISHING):
            self.doc["publish_progress"] = str(payload.get("step") or "")
            self.progress_steps.append(str(payload.get("step") or ""))
        return dict(self.doc)


class _Task:
    def __init__(self, task_id: str, state: TaskState, shapes: int = 1) -> None:
        self.doc = Task(
            task_id=task_id,
            project_id="p1",
            state=state,
            submitted_by="gina",
            reviewed_by="carol",
            review_action="accepted",
            source={"kind": "chunks", "keys": [task_id]},  # type: ignore[arg-type]
            media={"kind": "image", "image_url": "s3://b/x.jpg"},  # type: ignore[arg-type]
        ).model_dump(mode="json")
        # Shape ids are minted at SAVE time by the real actor and PERSISTED, so a re-read returns
        # the same ids. Minting them here per read would make the double lie about replay stability
        # — which is exactly what it did until this was fixed, and it is why the annotation_id
        # column, not published_at, was the thing that drifted.
        self.draft_shapes = [{"shape_id": f"{task_id}-shape-{i}", "shape_type": "bbox", "x": 1.0, "y": 1.0, "width": 1.0, "height": 1.0} for i in range(shapes)]

    async def get(self) -> dict[str, Any] | None:
        return dict(self.doc)

    async def get_draft(self) -> dict[str, Any] | None:
        if not self.draft_shapes:
            return None
        return {
            "task_id": self.doc["task_id"],
            "project_id": "p1",
            "author": "gina",
            "revision": 1,
            "shapes": [dict(s) for s in self.draft_shapes],
        }


class _Publisher:
    """A catalog that behaves like the real one must: create is idempotent on table id."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.tables: dict[str, int] = {}
        self.creates = 0
        self.pin: tuple[str, int | None] | None = None
        self.tags: list[tuple[str, str]] = []
        self.fail_on = fail_on

    async def create_table(
        self,
        table_id: str,
        plan: Any,
        *,
        properties: dict[str, str],
        run_facet: dict[str, Any],
        source: str | None = None,
        source_version: int | None = None,
    ) -> int:
        self.pin = (source, source_version) if source is not None else None
        if self.fail_on == "create":
            raise RuntimeError("catalog unreachable")
        self.creates += 1
        # The contract the saga relies on: an existing id is success, not an error.
        self.tables.setdefault(table_id, 7)
        self.rows = list(plan.rows)
        self.properties = properties
        self.run_facet = run_facet
        return self.tables[table_id]

    async def tag_version(self, table_id: str, version: int, tag: str) -> None:
        if self.fail_on == "tag":
            raise RuntimeError("tag service unreachable")
        if (table_id, tag) not in self.tags:
            self.tags.append((table_id, tag))


def _wire(tasks: dict[str, _Task], project: _Project) -> Any:
    project.index = {tid: str(t.doc["state"]) for tid, t in tasks.items()}
    return lambda task_id: tasks[task_id]


async def _run(project: _Project, tasks: dict[str, _Task], publisher: _Publisher) -> Any:
    return await run_publish(
        project_handle=project,
        task_handle=_wire(tasks, project),
        publisher=publisher,
        namespace="silver",
        subject="henry",
    )


# --------------------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_publish_creates_one_table_tags_it_and_records_it() -> None:
    project, publisher = _Project(), _Publisher()
    tasks = {"t0": _Task("t0", TaskState.ACCEPTED, 2), "t1": _Task("t1", TaskState.SKIPPED, 0)}

    out = await _run(project, tasks, publisher)

    assert out.shapes == 2 and out.rows == 3  # 2 shapes + 1 skip sentinel
    assert publisher.creates == 1
    assert publisher.tags == [(out.table_id, f"publish-{PUBLISH_TOKEN}")]
    assert project.fired == ["publish_succeeded"]
    assert project.doc["published"]["publish_id"] == PUBLISH_TOKEN


@pytest.mark.asyncio
async def test_the_saga_narrates_its_steps_so_a_publish_is_not_just_a_spinner() -> None:
    """A4: a watching human sees WHERE the publish is. The narration is best-effort — but when it
    works, the steps must arrive in execution order and name the real work."""
    project, publisher = _Project(), _Publisher()
    tasks = {"t0": _Task("t0", TaskState.ACCEPTED, 1)}

    await _run(project, tasks, publisher)

    assert project.progress_steps[0] == "reading tasks"
    assert project.progress_steps[1] == "building plan"
    assert project.progress_steps[2].startswith("creating table silver$vasa_")
    assert project.progress_steps[3].startswith("tagging version")
    assert project.progress_steps[4] == "recording publish"


@pytest.mark.asyncio
async def test_a_narration_failure_does_not_fail_the_publish() -> None:
    """Progress is telemetry, not a step. A project actor that refuses the note must not turn a
    working publish into a failed one."""

    class _Mute(_Project):
        async def note_progress(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("note refused")

    project, publisher = _Mute(), _Publisher()
    tasks = {"t0": _Task("t0", TaskState.ACCEPTED, 1)}

    out = await _run(project, tasks, publisher)

    assert out.rows == 1
    assert project.doc["state"] == ProjectState.PUBLISHED


@pytest.mark.asyncio
async def test_the_table_id_is_derived_from_the_token_not_the_slug_alone() -> None:
    """Two publishes of one project (a republish after `reopen`) are two datasets. A slug-only id
    would collide and silently overwrite the first."""
    p = AnnotationProject(project_id="p1", tenant="acme", slug="vasa")
    a = table_id_for(p, "aaaaaaaaaaaaaaaa", "silver")
    b = table_id_for(p, "bbbbbbbbbbbbbbbb", "silver")

    assert a != b
    assert a.startswith("silver$vasa_")
    assert table_id_for(p, "aaaaaaaaaaaaaaaa", "silver") == a, "the id must be deterministic in the token"


@pytest.mark.asyncio
async def test_the_table_id_uses_the_catalog_delimiter_so_authz_and_creation_name_the_same_object() -> None:
    """The catalog's identifier delimiter is `$` (`LANCE_NS_DELIMITER`, catalog/core/config.py) —
    estate ids are `bronze$events`, `silver$features`. A dot-joined id posted to
    `/v1/table/{id}/...` parses as ONE root-level segment, so the table would land at the catalog
    ROOT while FGA authorized creation in `namespace:silver`: the authorization object and the
    created object diverge. The delimiter in the id is what keeps them the same object."""
    p = AnnotationProject(project_id="p1", tenant="acme", slug="vasa")
    tid = table_id_for(p, "aaaaaaaaaaaaaaaa", "silver")

    namespace, _, name = tid.partition("$")
    assert namespace == "silver", f"the id must be namespace-qualified with '$', got {tid!r}"
    assert name == "vasa_aaaaaaaaaaaa"
    assert "." not in tid, "a '.' in the id is not a namespace separator to the catalog"


# --------------------------------------------------------------------------------------------------
# Crash and retry — the claim OPERATORS.md §4 requires
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_crash_after_create_retries_onto_the_same_table() -> None:
    """The saga's only non-idempotent step, made idempotent by key. A second attempt must NOT create
    a second table — that is the whole reason the token is minted before the work, not after."""
    project, publisher = _Project(), _Publisher(fail_on="tag")
    tasks = {"t0": _Task("t0", TaskState.ACCEPTED, 1)}

    with pytest.raises(RuntimeError):
        await _run(project, tasks, publisher)
    assert project.doc["state"] == ProjectState.PUBLISH_FAILED
    assert len(publisher.tables) == 1

    # Operator retries: the endpoint fires `publish` again, which REUSES the token.
    await project.fire({"event": "publish"})
    publisher.fail_on = None
    out = await _run(project, tasks, publisher)

    assert len(publisher.tables) == 1, "the retry created a SECOND table — the token was not reused"
    assert out.table_id in publisher.tables
    assert project.doc["state"] == ProjectState.PUBLISHED


@pytest.mark.asyncio
async def test_a_failure_records_the_reason_and_leaves_a_retryable_state() -> None:
    project, publisher = _Project(), _Publisher(fail_on="create")
    tasks = {"t0": _Task("t0", TaskState.ACCEPTED, 1)}

    with pytest.raises(RuntimeError, match="catalog unreachable"):
        await _run(project, tasks, publisher)

    assert project.fired == ["publish_failed"]
    assert project.doc["state"] == ProjectState.PUBLISH_FAILED
    assert "catalog unreachable" in project.doc["publish_error"]


@pytest.mark.asyncio
async def test_running_the_saga_again_after_success_converges_instead_of_republishing() -> None:
    """A redelivered trigger must not produce a second dataset for one logical publish."""
    project, publisher = _Project(), _Publisher()
    tasks = {"t0": _Task("t0", TaskState.ACCEPTED, 1)}
    first = await _run(project, tasks, publisher)

    second = await _run(project, tasks, publisher)

    assert second.already_published is True
    assert second.table_id == first.table_id
    assert publisher.creates == 1, "a replay republished the project"


@pytest.mark.asyncio
async def test_the_publish_record_is_never_overwritten_by_a_replay() -> None:
    project, publisher = _Project(), _Publisher()
    tasks = {"t0": _Task("t0", TaskState.ACCEPTED, 1)}
    await _run(project, tasks, publisher)
    original = dict(project.doc["published"])

    await _run(project, tasks, publisher)

    assert project.doc["published"] == original


# --------------------------------------------------------------------------------------------------
# The index's blind spot
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_index_cannot_let_an_unreviewed_task_into_the_lakehouse() -> None:
    """The scenario the whole re-read exists for: a task was REOPENED, but the report to the project
    index failed, so the index still says `accepted` and the precondition passed. Reading the task's
    own actor is the only thing that sees the truth."""
    project, publisher = _Project(), _Publisher()
    reopened = _Task("t1", TaskState.CHANGES_REQUESTED, 1)
    tasks = {"t0": _Task("t0", TaskState.ACCEPTED, 1), "t1": reopened}
    handle = _wire(tasks, project)
    project.index["t1"] = str(TaskState.ACCEPTED)  # the index LIES — the report never landed

    with pytest.raises(PublishRefusal, match="stale"):
        await run_publish(project_handle=project, task_handle=handle, publisher=publisher, namespace="silver", subject="henry")

    assert publisher.creates == 0, "unreviewed work reached the lakehouse"
    assert project.doc["state"] == ProjectState.PUBLISH_FAILED


@pytest.mark.asyncio
async def test_an_indexed_task_whose_actor_lost_its_state_refuses_the_publish() -> None:
    """A half-completed `send` (indexed, never seeded). Publishing the rest would produce a dataset
    that looks complete and silently omits an item."""

    class _Empty:
        async def get(self) -> None:
            return None

        async def get_draft(self) -> None:
            return None

    project, publisher = _Project(), _Publisher()
    project.index = {"ghost": str(TaskState.ACCEPTED)}

    with pytest.raises(PublishRefusal, match="holds no state"):
        await run_publish(project_handle=project, task_handle=lambda _t: _Empty(), publisher=publisher, namespace="silver", subject="henry")
    assert publisher.creates == 0


# --------------------------------------------------------------------------------------------------
# Preconditions on the saga itself
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_saga_refuses_to_run_outside_publishing() -> None:
    project, publisher = _Project(state=ProjectState.FROZEN), _Publisher()
    with pytest.raises(PublishRefusal, match="not publishing"):
        await _run(project, {"t0": _Task("t0", TaskState.ACCEPTED)}, publisher)


@pytest.mark.asyncio
async def test_a_missing_token_stops_rather_than_minting_one() -> None:
    """Inventing a token here would defeat the entire idempotency argument: a retry would mint a
    different one and create a second table. Disagreement between the machine and the saga is a
    bug to surface, not to paper over."""
    project, publisher = _Project(token=None), _Publisher()
    with pytest.raises(PublishRefusal, match="no publish token"):
        await _run(project, {"t0": _Task("t0", TaskState.ACCEPTED)}, publisher)
    assert publisher.creates == 0


@pytest.mark.asyncio
async def test_a_retry_writes_the_SAME_published_at_as_the_attempt_it_resumes() -> None:
    """`published_at` lands in every row and in the PublishRecord. Stamping it per ATTEMPT would make
    a retry rewrite the dataset with different values than the crashed attempt — falsifying the
    "a replay is byte-identical" property in exactly the case it exists to cover.

    Minted with the token, at the transition, so both survive the crash together.
    """
    project, publisher = _Project(), _Publisher(fail_on="tag")
    tasks = {"t0": _Task("t0", TaskState.ACCEPTED, 1)}

    with pytest.raises(RuntimeError):
        await _run(project, tasks, publisher)
    first_rows = [dict(r) for r in publisher.rows]

    await project.fire({"event": "publish"})
    publisher.fail_on = None
    await _run(project, tasks, publisher)

    assert publisher.rows == first_rows, "the retry rewrote the dataset with a different published_at"
    assert project.doc["published"]["published_at"] == MINTED_AT.isoformat()


@pytest.mark.asyncio
async def test_the_instant_is_reused_not_re_minted_on_a_retry() -> None:
    project = _Project()
    before = project.doc["pending_publish_at"]
    await project.fire({"event": "publish_failed", "error": "x"})
    await project.fire({"event": "publish"})
    assert project.doc["pending_publish_at"] == before, "a retry re-minted the publish instant"


@pytest.mark.asyncio
async def test_a_crash_between_record_and_succeeded_does_not_strand_the_project() -> None:
    """The wedge. `record_publish` lands, then the process dies before `publish_succeeded`. The
    project is now `publishing` WITH a publish record — and `PROJECT_EDGES` has no edge out of
    `publishing` except the two system ones, so a retry that returns early on "already published"
    leaves it stuck there forever, on precisely the crash this saga exists to survive.

    The retry must CONVERGE the state before reporting.
    """
    project, publisher = _Project(), _Publisher()
    tasks = {"t0": _Task("t0", TaskState.ACCEPTED, 1)}

    # Simulate the crash: the record landed, the transition never fired.
    project.doc["published"] = {
        "table_id": "silver$vasa_0123456789ab",
        "namespace": "silver",
        "version": 7,
        "tag": f"publish-{PUBLISH_TOKEN}",
        "publish_id": PUBLISH_TOKEN,
        "published_at": MINTED_AT.isoformat(),
        "published_by": "henry",
    }
    assert project.doc["state"] == ProjectState.PUBLISHING

    out = await _run(project, tasks, publisher)

    assert out.already_published is True
    assert project.doc["state"] == ProjectState.PUBLISHED, "the retry left the project stranded in publishing"
    assert publisher.creates == 0, "the retry republished"


@pytest.mark.asyncio
async def test_the_run_facet_is_keyed_by_its_name() -> None:
    """`X-Lance-Run-Facets` is a {name: payload} mapping. Handing the catalog a bare payload emits a
    facet with no name — dropped or rejected at the far end, which is provenance that silently does
    not arrive. `PROJECT_FACET` existed and was never used to key anything."""
    from annotator.projects.publish import PROJECT_FACET

    project, publisher = _Project(), _Publisher()
    await _run(project, {"t0": _Task("t0", TaskState.ACCEPTED, 1)}, publisher)

    assert set(publisher.run_facet) == {PROJECT_FACET}
    payload = publisher.run_facet[PROJECT_FACET]
    assert payload["_producer"] and payload["_schemaURL"], "the payload lost its spec-legal envelope"
    assert payload["publishId"] == PUBLISH_TOKEN


@pytest.mark.asyncio
async def test_a_failure_while_recording_the_failure_keeps_the_original_error() -> None:
    """Otherwise the operator is shown the actor's error instead of the one that broke the publish,
    and the real cause is never logged at all."""

    class _Wedged(_Project):
        async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
            if payload["event"] == "publish_failed":
                raise RuntimeError("actor unreachable too")
            return await super().fire(payload)

    project, publisher = _Wedged(), _Publisher(fail_on="create")

    with pytest.raises(RuntimeError, match="catalog unreachable"):
        await _run(project, {"t0": _Task("t0", TaskState.ACCEPTED, 1)}, publisher)


@pytest.mark.asyncio
async def test_the_pin_reaches_the_publisher_when_the_plan_pins() -> None:
    """§7.2: single dataset, single captured version → the create carries the pin (the lineage
    READ edge). The saga computes it from the plan — the publisher never guesses."""
    project, publisher = _Project(), _Publisher()
    tasks = {"t0": _Task("t0", TaskState.ACCEPTED, 1), "t1": _Task("t1", TaskState.ACCEPTED, 1)}
    for t in tasks.values():
        t.doc["source"]["where"] = "demo"
        t.doc["source"]["dataset_version"] = 24

    await _run(project, tasks, publisher)

    assert publisher.pin == ("demo", 24)


@pytest.mark.asyncio
async def test_no_pin_travels_when_the_capture_is_ambiguous() -> None:
    project, publisher = _Project(), _Publisher()
    tasks = {"t0": _Task("t0", TaskState.ACCEPTED, 1), "t1": _Task("t1", TaskState.ACCEPTED, 1)}
    tasks["t0"].doc["source"]["where"] = "demo"
    tasks["t0"].doc["source"]["dataset_version"] = 24
    tasks["t1"].doc["source"]["where"] = "other"

    await _run(project, tasks, publisher)

    assert publisher.pin is None
