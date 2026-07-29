"""job → stage → actor parent/child linkage, including across a simulated process boundary."""

from __future__ import annotations

import asyncio

import pytest
from lineage_kit import (
    LineageActorMixin,
    LineageContext,
    RecordingEmitter,
    RunState,
    coerce_context,
    current_context,
    job_run,
    stage,
)


def test_stage_runs_as_child_of_the_ambient_job(recording: RecordingEmitter) -> None:
    @stage("layout")
    def do_layout(x: int) -> int:
        return x + 1

    with job_run("ingest", namespace="rask", emitter=recording) as jr:
        assert do_layout(1) == 2

    # job START, stage START, stage COMPLETE, job COMPLETE
    states = [(e.job.name, e.event_type) for e in recording.events]
    assert states == [
        ("ingest", RunState.START),
        ("ingest.layout", RunState.START),
        ("ingest.layout", RunState.COMPLETE),
        ("ingest", RunState.COMPLETE),
    ]
    parent = recording.events[1].run.facets.parent
    assert parent is not None
    assert parent.run.run_id == jr.run_id
    assert parent.job.name == "ingest"
    assert parent.root is not None and parent.root.run.run_id == jr.run_id


def test_stage_failure_emits_fail_and_reraises(recording: RecordingEmitter) -> None:
    @stage()
    def transcribe() -> None:
        msg = "cuda oom"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="cuda oom"), job_run("htr", namespace="rask", emitter=recording):
        transcribe()

    stage_fail = recording.events[2]
    assert (stage_fail.job.name, stage_fail.event_type) == ("htr.transcribe", RunState.FAIL)


def test_async_stage_terminates_after_the_await(recording: RecordingEmitter) -> None:
    # A coroutine stage must COMPLETE only once the body has actually run — the naive
    # sync wrapping would emit COMPLETE at coroutine-creation time.
    ran: list[str] = []

    @stage("async-layout", emitter=recording)
    async def do_layout() -> int:
        ran.append("body")
        return 7

    coro = do_layout()
    assert recording.events == []  # nothing emitted at coroutine creation — only at execution
    assert asyncio.run(coro) == 7
    assert ran == ["body"]
    assert [e.event_type for e in recording.events] == [RunState.START, RunState.COMPLETE]


def test_async_stage_failure_emits_fail_not_complete(recording: RecordingEmitter) -> None:
    @stage("async-boom", emitter=recording)
    async def boom() -> None:
        msg = "gpu fell over"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="gpu fell over"):
        asyncio.run(boom())

    assert [e.event_type for e in recording.events] == [RunState.START, RunState.FAIL]


def test_stage_inside_lineage_task_parents_onto_the_task(recording: RecordingEmitter) -> None:
    # The online shape end to end: deployment run → per-request task run → nested stage.
    @stage("postprocess")
    def postprocess() -> None:
        pass

    class Transcriber(LineageActorMixin):
        def __init__(self, emitter: RecordingEmitter) -> None:
            self.init_lineage("transcribe", emitter=emitter)

    dep = Transcriber(recording)
    with dep.lineage_task("request") as task:
        postprocess()

    nested_start = next(e for e in recording.events if e.job.name == "transcribe.request.postprocess")
    parent = nested_start.run.facets.parent
    assert parent is not None
    assert parent.run.run_id == task.run_id  # parent = the TASK run, not detached
    assert parent.root is not None and parent.root.run.run_id == dep.lineage.run_id


def test_actor_links_across_a_process_boundary(recording: RecordingEmitter) -> None:
    carried: dict[str, str] = {}

    @stage("layout")
    def do_layout() -> None:
        ctx = current_context()
        assert ctx is not None
        carried["ctx"] = ctx.model_dump_json()  # what the driver ships to the actor

    with job_run("ingest", namespace="rask", emitter=recording) as jr:
        do_layout()

    # "The other side": no ambient context here (the job block is closed) — only the
    # serialized string crosses, exactly like a Ray actor constructor argument.
    assert current_context() is None

    class LayoutActor(LineageActorMixin):
        def __init__(self, lineage_context: str, emitter: RecordingEmitter) -> None:
            self.init_lineage("worker", context=lineage_context, emitter=emitter)

    actor = LayoutActor(carried["ctx"], recording)
    actor_start = recording.events[-1]
    stage_start = recording.events[1]

    assert actor_start.job.name == "ingest.layout.worker"
    parent = actor_start.run.facets.parent
    assert parent is not None
    assert parent.run.run_id == stage_start.run.run_id  # parent = the STAGE run
    assert parent.job.name == "ingest.layout"
    assert parent.root is not None
    assert parent.root.run.run_id == jr.run_id  # root = the JOB run
    assert parent.root.job.name == "ingest"

    actor.lineage.complete()
    assert recording.events[-1].event_type == RunState.COMPLETE


def test_env_var_carry_rehydrates(recording: RecordingEmitter, monkeypatch: pytest.MonkeyPatch) -> None:
    with job_run("ingest", namespace="rask", emitter=recording) as jr:
        env = jr.context.as_env()  # what the driver puts in runtime_env["env_vars"]

    for key, value in env.items():
        monkeypatch.setenv(key, value)

    class Worker(LineageActorMixin):
        def __init__(self, emitter: RecordingEmitter) -> None:
            self.init_lineage("worker", emitter=emitter)

    Worker(recording)
    actor_start = recording.events[-1]
    assert actor_start.job.name == "ingest.worker"
    parent = actor_start.run.facets.parent
    assert parent is not None and parent.run.run_id == jr.run_id


def test_online_serve_shape_root_actor_with_task_children(recording: RecordingEmitter) -> None:
    class Transcriber(LineageActorMixin):
        def __init__(self, emitter: RecordingEmitter) -> None:
            self.init_lineage("transcribe", emitter=emitter)

        def handle(self) -> None:
            with self.lineage_task("request", inputs=[("serve", "payload")]):
                pass

    dep = Transcriber(recording)
    dep.handle()
    dep.handle()

    actor_start = recording.events[0]
    assert actor_start.job.name == "transcribe"
    assert actor_start.run.facets.parent is None  # online: the deployment run is its own root

    req_start = recording.events[1]
    assert req_start.job.name == "transcribe.request"
    parent = req_start.run.facets.parent
    assert parent is not None
    assert parent.run.run_id == dep.lineage.run_id
    assert parent.root is not None and parent.root.run.run_id == dep.lineage.run_id
    # two requests = two distinct child runs
    assert recording.events[1].run.run_id != recording.events[3].run.run_id


def test_lineage_property_requires_init() -> None:
    class Bare(LineageActorMixin):
        pass

    with pytest.raises(RuntimeError, match="init_lineage"):
        _ = Bare().lineage


def test_coerce_context_accepts_model_dict_and_json() -> None:
    ctx = LineageContext.root(namespace="rask", job_name="ingest", run_id="3f2504e0-4f89-11d3-9a0c-0305e82c3301")
    assert coerce_context(ctx) is ctx
    assert coerce_context(ctx.model_dump()) == ctx
    assert coerce_context(ctx.model_dump_json()) == ctx
    assert coerce_context(None) is None
