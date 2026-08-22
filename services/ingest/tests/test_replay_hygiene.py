"""The DWF-ACT sweep over `ingest` — what must hold because Dapr REPLAYS.

Dapr Workflow's execution model is at-least-once with deterministic replay: an orchestrator body is
re-executed from the top on every resumption and must produce the identical action sequence, and an
activity is re-run whenever its result was not durably recorded. Three properties follow, and each
one was missing:

  F12a  a `finalize` that committed and then lost its pod runs AGAIN with the same input. The
        catalog can recognize its own earlier commit — it stamps `rask.ingest.run_id=<id>` into the
        Lance transaction — but only by scanning versions AFTER the `read_version` presented to it.
        `finalize` re-read that version per attempt, so the retry presented the version its own first
        attempt had just produced, the scan window was empty, and the run appended every row twice.
        Append never conflicts with Append, so nothing refused it.

  F12b  the run ceilings were module-level `os.getenv` reads BRANCHED ON inside the orchestrator. A
        replay lands on whichever pod is free; a rolling deploy that moved either value made the same
        history replay into a different action stream — the durable timer exists in one execution and
        not the other, which wedges the run.

  F12d  the per-unit `errors` map is keyed by UNIT, so a bad source makes it as large as the run.
        The workflow persisted it four times over (the drain's activity result, the child's return,
        the parent's merge, `finalize`'s input).

HOW THESE TESTS WORK — the generator protocol IS the runtime's protocol. A Dapr workflow function is
a generator; the runtime drives it with `.send(result)` for each yielded task. So driving it by hand
is the same protocol minus persistence, and "replay" is expressible directly: send the SAME recorded
results while the process env says something else, and assert the action stream did not move.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from dapr.ext.workflow import DaprWorkflowContext, WorkflowActivityContext
from ingest.workflow import ERRORS_TRUNCATED_KEY, MAX_REPORTED_ERRORS, ChunkSpec, RunLimits, RunSpec, bound_errors, chunk_run, ingest_run, resolve_limits


class _Task:
    """A stand-in for a durabletask task — identity is all `when_any` comparisons use."""


class _Ctx:
    """Records what the workflow ASKS THE RUNTIME FOR, in order: the action stream itself."""

    def __init__(self) -> None:
        self.activities: list[tuple[str, dict[str, Any]]] = []
        self.timers: int = 0
        self.statuses: list[str] = []

    def call_activity(self, fn: Any, *, input: Any = None, retry_policy: Any = None) -> _Task:  # noqa: A002 — the runtime's own keyword
        self.activities.append((getattr(fn, "__name__", str(fn)), input or {}))
        return _Task()

    # `instance_id` is accepted (and unused — this fake returns a completed task) because the REAL
    # `DaprWorkflowContext.call_child_workflow` takes it, and `ingest_run` now passes it so an
    # abandonment path can name the children it must stop (§2.4). A fake that omits a parameter the
    # code under test supplies fails as a TypeError swallowed by the error boundary — which reads as
    # a product bug, not a fixture gap.
    def call_child_workflow(self, fn: Any, *, input: Any = None, instance_id: str | None = None) -> _Task:  # noqa: A002
        return _Task()

    def create_timer(self, _delta: Any) -> _Task:
        self.timers += 1
        return _Task()

    def set_custom_status(self, status: str) -> None:
        self.statuses.append(status)


SPEC = {"run_id": "replay-test", "kind": "s3-prefix", "project": "bind86", "dataset": "pages", "options": {}}
HANDLE = {"location": "s3://wh/pages.lance", "read_version": 5}


@pytest.fixture(autouse=True)
def _plain_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    """`wf.when_all`/`when_any` wrap tasks in runtime bookkeeping these stand-ins do not carry; the
    workflow only ever YIELDS the wrapper, so a bare task is a faithful double at this seam."""
    from ingest import workflow as wf_module

    monkeypatch.setattr(wf_module.wf, "when_all", lambda tasks: _Task())
    monkeypatch.setattr(wf_module.wf, "when_any", lambda tasks: _Task())


def _drive_to_fanout(ctx: _Ctx, *, limits: dict[str, Any], chunks: list[dict[str, Any]] | None = None) -> Any:
    """emit_start -> resolve_limits -> ensure_dataset -> enumerate_chunks -> the fan-in yield."""
    gen = ingest_run(cast(DaprWorkflowContext, ctx), SPEC)
    gen.send(None)
    gen.send(None)  # emit_start done
    gen.send(limits)  # resolve_limits' RECORDED result
    gen.send(HANDLE)  # ensure_dataset's RECORDED result
    gen.send(chunks if chunks is not None else [{"keys": ["a", "b"]}])
    return gen


# ── F12b: the ceilings ride history, not the replaying pod's environment ──────────────


def test_a_replay_creates_the_DEADLINE_TIMER_the_history_records_even_when_this_pod_has_no_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The determinism break, in the direction that wedges a run.

    The history says this run was accepted with a 24h ceiling, so its first execution created a
    durable timer. The pod replaying it has `RASK_INGEST_MAX_RUN_HOURS` unset — a rolling deploy that
    dropped the value. Under a module-level env read the replay would SKIP the timer the history has,
    and the action sequences would diverge.
    """
    monkeypatch.delenv("RASK_INGEST_MAX_RUN_HOURS", raising=False)
    ctx = _Ctx()

    _drive_to_fanout(ctx, limits={"max_run_hours": 24.0, "max_units": 0})

    assert ctx.timers == 1, "the replay skipped the deadline timer its own history records"


def test_a_replay_does_NOT_invent_a_timer_the_history_has_no_record_of(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same break in the other direction: the deployment gained a ceiling mid-run."""
    monkeypatch.setenv("RASK_INGEST_MAX_RUN_HOURS", "24")
    ctx = _Ctx()

    _drive_to_fanout(ctx, limits={"max_run_hours": 0.0, "max_units": 0})

    assert ctx.timers == 0, "the replay created a durable timer the history has no record of"


def test_the_unit_ceiling_refusal_also_comes_from_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """`max_units` has the milder version of the same defect: the same run reports FAILED-by-ceiling
    or COMPLETE depending on which pod replayed it."""
    monkeypatch.delenv("RASK_INGEST_MAX_UNITS", raising=False)
    ctx = _Ctx()

    gen = _drive_to_fanout(ctx, limits={"max_run_hours": 0.0, "max_units": 1})

    assert ctx.activities[-1][0] == "emit_terminal", "the ceiling refusal must still leave a terminal record"
    with pytest.raises(StopIteration) as stop:
        gen.send(None)
    assert stop.value.value["status"] == "FAILED"
    assert "over the 1 ceiling" in stop.value.value["errors"]["run"]


def test_the_ceilings_are_read_in_ACTIVITY_scope_and_default_to_unbounded(activity_ctx: WorkflowActivityContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """The env read still happens — it just happens where an env read is legal, and its answer is
    then pinned in history. Zero stays the CODE default: this plane advertises million-unit harvests,
    so a live default would kill the legitimate long run the ceilings exist to protect."""
    monkeypatch.delenv("RASK_INGEST_MAX_RUN_HOURS", raising=False)
    monkeypatch.delenv("RASK_INGEST_MAX_UNITS", raising=False)
    monkeypatch.delenv("RASK_INGEST_INCREMENTAL_MAX_ROWS", raising=False)
    assert resolve_limits(activity_ctx, SPEC) == {"max_run_hours": 0.0, "max_units": 0, "incremental_max_rows": 0}

    monkeypatch.setenv("RASK_INGEST_MAX_RUN_HOURS", "24")
    monkeypatch.setenv("RASK_INGEST_MAX_UNITS", "500")
    monkeypatch.setenv("RASK_INGEST_INCREMENTAL_MAX_ROWS", "1000")
    assert resolve_limits(activity_ctx, SPEC) == {"max_run_hours": 24.0, "max_units": 500, "incremental_max_rows": 1000}


def test_an_EMPTY_env_value_is_unbounded_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """`kubectl set env FOO=` leaves an empty string and `float("")` raises. A ceiling that crashes
    for a config typo takes its own activity — and with it every run — down."""
    monkeypatch.setenv("RASK_INGEST_MAX_RUN_HOURS", "")
    monkeypatch.setenv("RASK_INGEST_MAX_UNITS", "")
    monkeypatch.setenv("RASK_INGEST_INCREMENTAL_MAX_ROWS", "")

    assert RunLimits.from_env() == RunLimits()


def test_ACCEPT_TIME_limits_on_the_spec_win_over_the_deployment(activity_ctx: WorkflowActivityContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """The seam that lets a run be pinned to what it was ACCEPTED with rather than to whatever the
    pod that first executed it happened to hold."""
    monkeypatch.setenv("RASK_INGEST_MAX_UNITS", "500")
    monkeypatch.setenv("RASK_INGEST_INCREMENTAL_MAX_ROWS", "999")

    resolved = resolve_limits(activity_ctx, {**SPEC, "limits": {"max_run_hours": 1.0, "max_units": 9, "incremental_max_rows": 7}})

    assert resolved == {"max_run_hours": 1.0, "max_units": 9, "incremental_max_rows": 7}


def test_validating_a_workflow_model_reads_NO_env() -> None:
    """The same hazard one field away, and the reason `sizing` no longer defaults to `resolve()`.

    `RunSpec`/`ChunkSpec` are validated at the TOP of both generator bodies, so a `default_factory`
    that reads env is a workflow env read wearing a Pydantic disguise — `resolve()` reads four
    `RASK_INGEST_FRAGMENT_*`/`FETCH_*` variables at call time. `None` means the deployment default is
    resolved by the drain, in ACTIVITY scope, which is where an env read belongs.
    """
    for model in (RunSpec, ChunkSpec):
        field = model.model_fields["sizing"]
        assert field.default_factory is None, f"{model.__name__}.sizing resolves a default at validation time — that runs in workflow scope"
        assert field.default is None

    assert RunSpec.model_validate(SPEC).sizing is None
    assert ChunkSpec.model_validate({"run_id": "r", "chunk_id": "c0"}).sizing is None


def test_the_workflow_module_reads_NO_env_at_import() -> None:
    """Structural, and it is the shape of the defect rather than one instance of it.

    A module-level `os.getenv` looks like a constant and behaves like a clock: it is fixed for a
    PROCESS, not for a RUN, so two pods executing one run's history can disagree. Every env read in
    this module must sit inside a function body — an activity's, where the answer is then pinned in
    workflow history.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "ingest" / "workflow.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    inside_a_function = {
        id(node) for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) for node in ast.walk(fn) if isinstance(node, ast.Call)
    }
    module_level_env_reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"getenv", "environ"} and id(node) not in inside_a_function
    ]

    assert module_level_env_reads == [], "an env read at module scope is a per-POD value the workflow may branch on — it must be an activity's"


def test_the_workflow_BODIES_are_annotated_as_the_GENERATORS_they_are() -> None:
    """`uvx ty check` emitted `error[invalid-return-type]` on both of these, and `ty` runs with
    `error-on-warning = true` — so the annotation was a build failure, not a style note.

    A workflow body is a generator: every `yield` is a durable await point and the runtime drives it
    with `.send()`/`.throw()`. Annotating it `-> dict[str, Any]` names the payload the last `return`
    carries and hides the protocol the whole plane rests on. `flows/workflow.py:46` had already ruled
    this the same way ("Annotating it `-> dict` … is a lie the type checker catches"); this is the
    same rule, applied to the module that was the counter-example.
    """
    import ast

    src = Path(__file__).resolve().parents[1] / "src" / "ingest" / "workflow.py"
    functions = {node.name: node for node in ast.parse(src.read_text(encoding="utf-8")).body if isinstance(node, ast.FunctionDef)}

    for name in ("ingest_run", "chunk_run"):
        fn = functions[name]
        assert any(isinstance(node, ast.Yield | ast.YieldFrom) for node in ast.walk(fn)), f"{name} is no longer a generator — this gate is stale"
        returns = ast.unparse(fn.returns) if fn.returns is not None else None
        assert returns is not None and returns.startswith("Generator["), f"{name} is a generator annotated `-> {returns}` — ty calls that invalid-return-type"


# ── F12a: the commit's base version is carried, so a replay is recognizable ───────────


def test_finalize_is_handed_the_read_version_ensure_dataset_resolved() -> None:
    """The workflow half. The version `finalize` commits against must come from the handle recorded
    in history, so a replay presents the same base version its first attempt did."""
    ctx = _Ctx()
    gen = _drive_to_fanout(ctx, limits={"max_run_hours": 0.0, "max_units": 0})

    gen.send([{"chunk_id": "c0", "units_done": 2, "fragments": ["{}"], "errors": {}, "errors_total": 0}])

    name, payload = ctx.activities[-1]
    assert name == "finalize"
    assert payload["read_version"] == HANDLE["read_version"], "finalize would re-derive its base version, and the catalog's replay dedupe needs the carried one"


def test_ensure_dataset_at_returns_the_catalogs_version_with_the_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """One resolution of the location AND one of the base version — never two of either."""
    from ingest import runtime

    class _Catalog:
        def ensure(self, namespace: str, dataset: str) -> str:
            return "s3://wh/pages.lance"

        def describe_version(self, namespace: str, dataset: str) -> int:
            return 11

    monkeypatch.setattr(runtime, "_catalog", lambda: _Catalog())

    assert runtime.ensure_dataset_at(RunSpec.model_validate(SPEC)) == ("s3://wh/pages.lance", 11)


def test_a_RETRIED_finalize_presents_the_SAME_read_version_and_never_re_reads_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """THE REGRESSION, at the seam that produced the duplicate rows.

    Attempt 1 commits and the pod dies before Dapr records the result; Dapr re-executes the activity
    with the same input. The catalog answers a repeat of `(run_id, read_version)` with the version
    that run already committed — but it finds the earlier commit by scanning versions AFTER the
    presented `read_version`, so a `finalize` that re-read the version presented the one its own
    first attempt had produced and the scan window was empty. Both attempts must present 5.
    """
    from ingest import runtime, staging

    class _Catalog:
        """A catalog with a `commit` door — the DEPLOYED branch of `finalize_run`."""

        def __init__(self) -> None:
            self.version = 5
            self.presented: list[int] = []
            self.describe_calls = 0

        def ensure(self, namespace: str, dataset: str) -> str:
            return str(tmp_path / "pages.lance")

        def describe_version(self, namespace: str, dataset: str) -> int:
            self.describe_calls += 1
            return self.version

        def commit(self, namespace: str, dataset: str, fragments: Any, *, read_version: int, run_id: str) -> tuple[int, int]:
            self.presented.append(read_version)
            self.version += 1
            return self.version, 4

    catalog = _Catalog()
    monkeypatch.setattr(runtime, "_catalog", lambda: catalog)
    monkeypatch.setattr(staging, "discover_staged", lambda uri, run_id: ["{}"])

    spec = RunSpec.model_validate(SPEC)
    runtime.finalize_run(spec, ["{}"], {}, read_version=5)
    runtime.finalize_run(spec, ["{}"], {}, read_version=5)

    assert catalog.presented == [5, 5], "the retry presented the version its own first attempt produced — the catalog cannot recognize its own commit"
    assert catalog.describe_calls == 0, "finalize re-read the base version instead of using the carried one"


# ── F12d: the error payload is capped, the COUNT stays exact ──────────────────────────


def test_bound_errors_caps_the_payload_and_keeps_the_count() -> None:
    listed, total = bound_errors({f"s3://b/{i:05d}.tif": "corrupt TIFF" for i in range(250)})

    assert total == 250, "the count must survive the cap — it is the number an operator acts on"
    assert len(listed) == MAX_REPORTED_ERRORS + 1, "one entry over the cap is the overflow marker itself"
    assert ERRORS_TRUNCATED_KEY in listed
    assert "150 further units failed" in listed[ERRORS_TRUNCATED_KEY]


def test_bound_errors_is_DETERMINISTIC_because_it_runs_in_workflow_scope() -> None:
    """Two replays must select the same survivors, so the choice is by sorted key and never by dict
    order — which JSON round-trips do not promise to preserve."""
    errors = {f"key-{i:05d}": "boom" for i in range(250)}
    shuffled = dict(reversed(list(errors.items())))

    assert bound_errors(errors)[0] == bound_errors(shuffled)[0]


def test_the_supplied_TOTAL_is_a_FLOOR_and_never_under_reports_what_is_listed() -> None:
    """A caller can only ever under-state the count — a child result carrying no `errors_total` at
    all reads as zero — and `(errors={2 named units}, errors_total=0)` is a pair no operator or API
    can act on. What is listed is known to have failed, so it is the floor the count cannot go under.
    """
    listed, total = bound_errors({"k1": "corrupt TIFF", "k2": "corrupt TIFF"}, 0)

    assert total == 2, "the count went under the failures the map itself names"
    assert ERRORS_TRUNCATED_KEY not in listed, "nothing was dropped, so nothing may claim it was"


def test_an_ALREADY_TRUNCATED_map_does_not_lose_its_count_when_it_is_merged_again() -> None:
    """The parent merges N bounded child results; counting its keys would silently un-count every
    unit a child had to drop."""
    child, _ = bound_errors({f"k{i}": "boom" for i in range(250)})

    listed, total = bound_errors(child, 250)

    assert total == 250
    assert len(listed) == MAX_REPORTED_ERRORS + 1


def test_a_chunk_returns_a_BOUNDED_result_no_matter_how_many_units_failed() -> None:
    """A child workflow's return value is persisted, carried to the parent and re-input to finalize.
    A chunk of 1000 dead units must not put 1000 entries through all three."""
    ctx = _Ctx()
    gen = chunk_run(cast(DaprWorkflowContext, ctx), {"run_id": "r", "chunk_id": "c0", "keys": [f"k{i}" for i in range(1000)]})
    gen.send(None)  # -> publish_units
    gen.send(1000)  # published -> drain_chunk

    drained = {"chunk_id": "c0", "fragments": [], "units_done": 0, "errors": {f"k{i}": "corrupt TIFF" for i in range(1000)}, "errors_total": 1000}
    gen.send(drained)  # -> reconcile_chunk (the drain came up short)
    with pytest.raises(StopIteration) as stop:
        gen.send({"chunk_id": "c0", "fragments": [], "errors": {}})

    result = stop.value.value
    assert result["errors_total"] == 1000, "the child dropped the exact failure count along with the payload"
    assert len(result["errors"]) == MAX_REPORTED_ERRORS + 1


def test_the_PARENT_merge_stays_bounded_across_chunks_and_reports_the_true_total() -> None:
    """Bounding each child is not enough: N children each at the cap is still N * cap in the parent's
    history, and the parent's dict is the one that rides into `finalize` and out again."""
    ctx = _Ctx()
    gen = _drive_to_fanout(ctx, limits={"max_run_hours": 0.0, "max_units": 0}, chunks=[{"keys": ["a"]}, {"keys": ["b"]}])

    chunk_results = [
        {"chunk_id": f"c{n}", "fragments": [], "errors": {f"c{n}-k{i}": "corrupt TIFF" for i in range(MAX_REPORTED_ERRORS)}, "errors_total": 900}
        for n in range(2)
    ]
    gen.send(chunk_results)

    name, payload = ctx.activities[-1]
    assert name == "finalize"
    assert payload["errors_total"] == 1800, "the run's failure count must be the children's totals, not the surviving key count"
    assert len(payload["errors"]) == MAX_REPORTED_ERRORS + 1


# ── F12e: the lineage mirror is a ring buffer, not a leak ─────────────────────────────


def test_the_in_process_lineage_mirror_is_BOUNDED() -> None:
    """`_EVENTS` is module-level state in HANDLER scope (DWF-ACT-007), appended to by every activity
    of every run and cleared by nothing outside this suite. An ingest pod serves runs indefinitely, so
    a plain list grew for the lifetime of the process. Nothing reads more than the tail."""
    from ingest.lineage import _EVENT_HISTORY, LineageEvent, LineageRecorder, recorded_events, reset_events

    reset_events()
    recorder = LineageRecorder()
    for index in range(_EVENT_HISTORY * 3):
        recorder._record(LineageEvent(run_id=f"r{index}", event_type="START"))

    events = recorded_events()
    assert len(events) == _EVENT_HISTORY, "the in-process lineage mirror is unbounded — it leaks for the pod's lifetime"
    assert events[-1].run_id == f"r{_EVENT_HISTORY * 3 - 1}", "the buffer must drop the OLDEST events, not the newest"
    reset_events()
